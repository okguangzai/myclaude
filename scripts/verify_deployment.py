"""Verify HTTPS, UI assets, access controls, persistence and optional live model/RAG.

Credentials JSON is private and must never be committed. No secrets are printed.
"""

import argparse
import json
import re
from pathlib import Path

import httpx


def verify(access, live=False, previous=None):
    url = access["url"].rstrip("/") + "/"
    report = {"url": url}
    with httpx.Client(
        base_url=url, auth=(access["username"], access["password"]), timeout=120, trust_env=False
    ) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response

        report["health"] = request("GET", "api/health").json()
        html = request("GET", "").text
        assets = re.findall(r'(?:src|href)="([^\"]*/assets/[^\"]+)"', html)
        assert len(assets) >= 2
        for asset in assets:
            result = request("GET", asset)
            assert "text/html" not in result.headers.get("Content-Type", "")
        report["frontend_assets"] = len(assets)
        settings = request("GET", "api/settings").json()
        assert "api_key" not in settings and settings["api_key_set"]
        report["configuration_masked"] = True
        report["unauthenticated_status"] = httpx.get(
            url + "api/health", trust_env=False
        ).status_code
        report["foreign_origin_status"] = client.post(
            "api/sessions", headers={"Origin": "https://attacker.invalid"}
        ).status_code
        assert report["unauthenticated_status"] == 401 and report["foreign_origin_status"] == 403
        if previous:
            session = request("GET", "api/sessions/" + previous["cloud_tool"]["session_id"]).json()
            assert session["messages"]
            docs = request("GET", "api/documents").json()
            assert any(doc["id"] == previous["document"]["id"] for doc in docs)
            report.update(session_survived_upgrade=True, document_survived_upgrade=True)
        if live:
            report["qdrant"] = request("POST", "api/connections/qdrant").json()
            report["embedding"] = request("POST", "api/connections/embedding").json()
            document = "MyClaude 0.5 通过 main 自动发布，访问域名为 myclaude.csghome.fun，知识库使用 Qdrant。"
            report["document"] = request(
                "POST",
                "api/documents",
                files={"file": ("deployment-v05-check.md", document.encode(), "text/markdown")},
            ).json()
            report["retrieval"] = request(
                "POST",
                "api/retrieve",
                json={"query": "MyClaude 0.5 的访问域名是什么？", "top_k": 2},
            ).json()
            assert report["retrieval"]["backend"] == "qdrant"
            assert any(
                hit["document_id"] == report["document"]["id"]
                for hit in report["retrieval"]["hits"]
            )
            session = request("POST", "api/sessions", json={"title": "独立域名 0.5 验收"}).json()
            events = []
            with client.stream(
                "POST",
                "api/chat",
                json={
                    "session_id": session["id"],
                    "message": "请调用 read_file 读取工作区 README.txt，然后只回答 MYCLAUDE_V05_SERVER_OK。",
                    "permission": "read-only",
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            final = next(event for event in reversed(events) if event["type"] == "run_end")
            assert final["status"] == "completed", final
            assert any(
                event["type"] == "tool_result"
                and event.get("name") == "read_file"
                and not event.get("is_error")
                for event in events
            )
            assert "MYCLAUDE_V05_SERVER_OK" in "".join(
                event.get("text", "") for event in events if event["type"] == "delta"
            )
            report["cloud_tool"] = {"session_id": session["id"], "final": final, "marker": True}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path(".local/deploy/access.json"))
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".local/server-v05-verification.json"))
    args = parser.parse_args()
    access = json.loads(args.credentials.read_text(encoding="utf-8"))
    previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
    report = verify(access, args.live_model, previous)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "url",
                    "health",
                    "frontend_assets",
                    "unauthenticated_status",
                    "foreign_origin_status",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
