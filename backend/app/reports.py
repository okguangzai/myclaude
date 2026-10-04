"""Canonical report hashes, capability projections and sensitivity provenance."""

import hashlib
import json
from copy import deepcopy

SCHEMA = "claw.report.v1"
POLICY = "claw.report.projection.v1"
SENSITIVITY = {name: i for i, name in enumerate(("public", "internal", "operator_only", "secret"))}


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()[:16]


def canonicalize(report):
    report = deepcopy(report)
    report["schema_version"] = SCHEMA
    for family, key in (("claims", "id"), ("negative_evidence", "id"), ("field_deltas", "field")):
        if report.get(family):
            report[family].sort(key=lambda item: item[key])
        else:
            report.pop(family, None)
    for claim in report.get("claims", []):
        if (
            claim["kind"] not in {"observed_fact", "inference", "hypothesis", "recommendation"}
            or claim["confidence"] not in {"high", "medium", "low", "unknown"}
            or claim["sensitivity"] not in SENSITIVITY
        ):
            raise ValueError("报告 claim 枚举不合法")
        if not claim.get("evidence"):
            claim.pop("evidence", None)
    for item in report.get("negative_evidence", []):
        if (
            item["status"] not in {"not_observed_in_checked_scope", "unknown_not_checked"}
            or item["sensitivity"] not in SENSITIVITY
        ):
            raise ValueError("报告 negative_evidence 枚举不合法")
        if not item.get("checked_surfaces"):
            item.pop("checked_surfaces", None)
    identity = report.get("identity", {}).get("report_id")
    report["identity"] = {"report_id": "", "content_hash": ""}
    digest = stable_hash(report)
    report["identity"] = {"report_id": identity or "report-" + digest, "content_hash": digest}
    return report


def project(report, capabilities, view="default"):
    report = canonicalize(report)
    maximum = SENSITIVITY[capabilities["max_sensitivity"]]
    families = capabilities.get("field_families", [])
    payload = {key: report[key] for key in ("identity", "generated_at", "producer")}
    omitted, redactions = [], []
    for family in ("claims", "negative_evidence", "field_deltas"):
        if families and family not in families:
            omitted.append(family)
            continue
        values = []
        for index, item in enumerate(report.get(family, [])):
            if family == "field_deltas" or SENSITIVITY[item["sensitivity"]] <= maximum:
                values.append(item)
                continue
            # Upstream only filters claims; also protect negative evidence, which can contain secrets.
            secret = item["sensitivity"] == "secret" or family == "negative_evidence"
            redactions.append(
                {
                    "field_path": f"{family}[{index}]" + ("" if secret else ".text"),
                    "reason": ("omitted" if secret else "transformed")
                    + ": sensitivity exceeds consumer policy",
                    "policy_id": POLICY,
                    "original_hash": stable_hash(item),
                }
            )
            if not secret:
                values.append(
                    {
                        k: ("<redacted>" if k == "text" else v)
                        for k, v in item.items()
                        if k != "evidence"
                    }
                )
        payload[family] = values
    provenance = {
        "policy_id": POLICY,
        "source_schema_version": SCHEMA,
        "source_report_id": report["identity"]["report_id"],
        "source_content_hash": report["identity"]["content_hash"],
        "consumer": capabilities["consumer"],
        "downgraded": SCHEMA not in capabilities.get("schema_versions", [])
        or bool(omitted or redactions),
    }
    if omitted:
        provenance["omitted_field_families"] = omitted
    if redactions:
        provenance["redactions"] = redactions
    projection = {"view": view, "provenance": provenance, "payload": payload}
    return {"schema_version": SCHEMA, "projection_id": stable_hash(projection), **projection}
