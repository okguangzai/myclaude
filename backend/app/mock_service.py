"""Deterministic Anthropic parity service; no cloud requests and no tool execution."""

import argparse
import json
import socket
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

SCENARIOS = {
    "streaming_text",
    "read_file_roundtrip",
    "grep_chunk_assembly",
    "write_file_allowed",
    "write_file_denied",
    "multi_tool_turn_roundtrip",
    "bash_stdout_roundtrip",
    "bash_permission_prompt_approved",
    "bash_permission_prompt_denied",
    "plugin_tool_roundtrip",
    "auto_compact_triggered",
    "token_cost_reporting",
}


def flatten(value):
    if isinstance(value, str):
        return value
    return "\n".join(block.get("text", json.dumps(block.get("value", ""))) for block in value or [])


def scenario_and_results(body):
    scenario, names, results = None, {}, {}
    for message in body.get("messages", []):
        content = message.get("content", [])
        content = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block in content:
            if block["type"] == "text":
                for token in block["text"].split():
                    if token.startswith("PARITY_SCENARIO:") and token[16:] in SCENARIOS:
                        scenario = token[16:]
            elif block["type"] == "tool_use":
                names[block["id"]] = block["name"]
            elif block["type"] == "tool_result":
                results[names.get(block["tool_use_id"], block["tool_use_id"])] = (
                    flatten(block.get("content")),
                    block.get("is_error", False),
                )
    return scenario, results


def extract(output, *keys, default=None):
    try:
        item = json.loads(output)
        for key in keys:
            item = item[key]
        return item
    except (ValueError, KeyError, TypeError):
        return output.strip() if default is None else default


def response_for(body):
    scenario, results = scenario_and_results(body)
    if not scenario:
        raise ValueError("missing or unsupported PARITY_SCENARIO")
    latest, failed = next(reversed(results.values()), ("", False))
    calls, text = [], ""
    if scenario == "streaming_text":
        text = "Mock streaming says hello from the parity harness."
    elif scenario in {"auto_compact_triggered", "token_cost_reporting"}:
        text = (
            "auto compact parity complete."
            if scenario == "auto_compact_triggered"
            else "token cost reporting parity complete."
        )
    elif scenario == "read_file_roundtrip":
        if results:
            text = "read_file roundtrip complete: " + str(extract(latest, "file", "content"))
        else:
            calls = [("toolu_read_fixture", "read_file", {"path": "fixture.txt"})]
    elif scenario == "grep_chunk_assembly":
        if results:
            text = f"grep_search matched {extract(latest, 'numMatches', default=0)} occurrences"
        else:
            calls = [
                (
                    "toolu_grep_fixture",
                    "grep_search",
                    {"pattern": "parity", "path": "fixture.txt", "output_mode": "count"},
                )
            ]
    elif scenario == "multi_tool_turn_roundtrip":
        if "read_file" in results and "grep_search" in results:
            text = f"multi-tool roundtrip complete: {extract(results['read_file'][0], 'file', 'content')} / {extract(results['grep_search'][0], 'numMatches', default=0)} occurrences"
        else:
            calls = [
                ("toolu_multi_read", "read_file", {"path": "fixture.txt"}),
                (
                    "toolu_multi_grep",
                    "grep_search",
                    {"pattern": "parity", "path": "fixture.txt", "output_mode": "count"},
                ),
            ]
    elif scenario.startswith("write_file_"):
        allowed = scenario == "write_file_allowed"
        if results:
            text = (
                "write_file succeeded: " + str(extract(latest, "filePath"))
                if allowed
                else "write_file denied as expected: " + latest
            )
        else:
            calls = [
                (
                    "toolu_write_allowed" if allowed else "toolu_write_denied",
                    "write_file",
                    {
                        "path": "generated/output.txt" if allowed else "generated/denied.txt",
                        "content": "created by mock service\n" if allowed else "should not exist\n",
                    },
                )
            ]
    elif scenario.startswith("bash_"):
        command = (
            "alpha from bash"
            if scenario == "bash_stdout_roundtrip"
            else "approved via prompt"
            if scenario.endswith("approved")
            else "should not run"
        )
        if results:
            text = (
                "bash completed: " + str(extract(latest, "stdout"))
                if scenario == "bash_stdout_roundtrip"
                else "bash denied as expected: " + latest
                if scenario.endswith("denied")
                else "bash approval unexpectedly failed: " + latest
                if failed
                else "bash approved and executed: " + str(extract(latest, "stdout"))
            )
        else:
            calls = [
                (
                    "toolu_bash_stdout",
                    "bash",
                    {"command": "printf '" + command + "'", "timeout": 1000},
                )
            ]
    elif scenario == "plugin_tool_roundtrip":
        if results:
            text = "plugin tool completed: " + str(extract(latest, "input", "message"))
        else:
            calls = [("toolu_plugin_echo", "plugin_echo", {"message": "hello from plugin parity"})]
    usage = {
        "input_tokens": 50000
        if scenario == "auto_compact_triggered"
        else 1000
        if scenario == "token_cost_reporting"
        else 100,
        "output_tokens": 200
        if scenario == "auto_compact_triggered"
        else 500
        if scenario == "token_cost_reporting"
        else 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    content = [
        {"type": "tool_use", "id": key, "name": name, "input": args} for key, name, args in calls
    ] or [{"type": "text", "text": text}]
    return scenario, {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "claude-sonnet-4-6"),
        "content": content,
        "stop_reason": "tool_use" if calls else "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


def sse_events(response):
    yield {
        "type": "message_start",
        "message": {
            **response,
            "content": [],
            "stop_reason": None,
            "usage": {**response["usage"], "output_tokens": 0},
        },
    }
    for index, block in enumerate(response["content"]):
        is_tool = block["type"] == "tool_use"
        yield {
            "type": "content_block_start",
            "index": index,
            "content_block": {**block, **({"input": {}} if is_tool else {"text": ""})},
        }
        value = json.dumps(block["input"], ensure_ascii=False) if is_tool else block["text"]
        for start in range(0, len(value), 9):
            yield {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta" if is_tool else "text_delta",
                    "partial_json" if is_tool else "text": value[start : start + 9],
                },
            }
        yield {"type": "content_block_stop", "index": index}
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": response["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": response["usage"]["output_tokens"]},
    }
    yield {"type": "message_stop"}


def create_app():
    app = FastAPI(title="MyClaude parity mock")
    app.state.requests = []

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        try:
            scenario, response = response_for(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        # Captures are for in-process tests only; authentication values are never retained.
        app.state.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "scenario": scenario,
                "stream": body.get("stream", False),
                "body": body,
                "headers": {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"authorization", "x-api-key", "cookie"}
                },
            }
        )
        if len(app.state.requests) > 1000:
            del app.state.requests[0]
        headers = {"x-request-id": "req_mock_" + scenario}
        if not body.get("stream"):
            return JSONResponse(response, headers=headers)

        async def stream():
            for event in sse_events(response):
                yield (
                    "event: "
                    + event["type"]
                    + "\ndata: "
                    + json.dumps(event, ensure_ascii=False)
                    + "\n\n"
                )

        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1:0")
    args = parser.parse_args()
    host, port = args.bind.rsplit(":", 1)
    with socket.socket() as sock:
        sock.bind((host, int(port)))
        print(f"MOCK_ANTHROPIC_BASE_URL=http://{host}:{sock.getsockname()[1]}", flush=True)
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="warning"))
        server.run(sockets=[sock])


if __name__ == "__main__":
    main()
