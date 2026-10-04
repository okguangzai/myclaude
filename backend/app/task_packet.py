"""Validation rules ported from runtime/task_packet.rs, independent of execution."""


def validate_packet(packet):
    errors = []
    for field in ("objective", "repo", "branch_policy", "commit_policy"):
        if not isinstance(packet.get(field), str) or not packet[field].strip():
            errors.append(field + " must not be empty")
    for left, right in (
        ("reporting_contract", "reporting_targets"),
        ("escalation_policy", "recovery_policy"),
        ("acceptance_tests", "acceptance_criteria"),
    ):
        if not packet.get(left) and not packet.get(right):
            errors.append(left + " or " + right + " must not be empty")
    scope = packet.get("scope")
    if scope not in {"workspace", "module", "single_file", "single-file", "custom"}:
        errors.append("unknown task scope")
    elif scope != "workspace" and not (packet.get("scope_path") or "").strip():
        errors.append("scope_path is required for scope '" + scope + "'")
    for field in ("model", "provider", "permission_profile", "recovery_policy"):
        if field in packet and (not isinstance(packet[field], str) or not packet[field].strip()):
            errors.append(field + " must not be empty when present")
    for field in ("acceptance_tests", "acceptance_criteria", "verification_plan"):
        for index, value in enumerate(packet.get(field, [])):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{field} contains an empty value at index {index}")
    for index, resource in enumerate(packet.get("resources", [])):
        if not resource.get("kind", "").strip() or not resource.get("value", "").strip():
            errors.append(f"resources contains an incomplete entry at index {index}")
    if errors:
        raise ValueError("; ".join(errors))
    return packet
