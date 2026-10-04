"""Supersede → collapse → cluster, preserving complete tool-call/result groups."""

import copy
import json


def groups(messages):
    result = []
    for message in messages:
        if message["role"] == "tool" and result:
            result[-1].append(message)
        else:
            result.append([message])
    return result


def features(group):
    tools, paths, ops = set(), set(), []
    for message in group:
        for call in message.get("tool_calls", []):
            tool = call["function"]["name"]
            tools.add(tool)
            try:
                args = json.loads(call["function"]["arguments"])
            except (ValueError, TypeError):
                continue
            path = args.get("path", args.get("file_path"))
            op = {
                "read_file": "read",
                "Read": "read",
                "write_file": "write",
                "Write": "write",
                "edit_file": "write",
                "Edit": "write",
            }.get(tool)
            if path and op:
                paths.add(path)
                ops.append((path, op))
    length = sum(
        len((m.get("content") or "").encode())
        + sum(len(c["function"]["arguments"].encode()) for c in m.get("tool_calls", []))
        for m in group
    )
    return {"role": group[0]["role"], "tools": tools, "paths": paths, "ops": ops, "length": length}


def summary(group, label):
    flat = [m for unit in group for m in unit]
    topics = list(dict.fromkeys((m.get("content") or "")[:80] for m in flat if m.get("content")))[
        :5
    ]
    fs = [features(unit) for unit in group]
    tools = sorted(set().union(*(item["tools"] for item in fs)))
    paths = sorted(set().union(*(item["paths"] for item in fs)))[:5]
    text = f"[{label}]\n{len(flat)} earlier messages grouped.\n"
    if tools:
        text += "Tools: " + ", ".join(tools) + ".\n"
    if paths:
        text += "Files: " + ", ".join(paths) + ".\n"
    text += "Topics:\n" + "\n".join("- " + topic for topic in topics)
    return [{"role": "system", "content": text}]


def similarity(a, b):
    if a["role"] != b["role"]:
        return 0

    def overlap(first, second):
        return len(first & second) / len(first | second) if first or second else 1

    length = (
        min(a["length"], b["length"]) / max(a["length"], b["length"])
        if a["length"] or b["length"]
        else 1
    )
    return (
        0.4 * overlap(a["tools"], b["tools"]) + 0.4 * overlap(a["paths"], b["paths"]) + 0.2 * length
    )


def compact(messages, config=None):
    cfg = {
        "supersede_enabled": True,
        "collapse_enabled": True,
        "cluster_enabled": True,
        "collapse_threshold": 4,
        "cluster_min_size": 3,
        "cluster_similarity_threshold": 0.6,
        **(config or {}),
    }
    if (
        cfg["collapse_threshold"] < 2
        or cfg["cluster_min_size"] < 2
        or not 0 <= cfg["cluster_similarity_threshold"] <= 1
    ):
        raise ValueError("invalid Trident thresholds")
    units = groups(copy.deepcopy(messages))
    original = len(messages)
    stats = {
        "superseded_count": 0,
        "collapsed_chains": 0,
        "messages_collapsed": 0,
        "clusters_found": 0,
        "messages_clustered": 0,
    }
    if cfg["supersede_enabled"]:
        fs = [features(unit) for unit in units]
        last_write = {
            path: i for i, item in enumerate(fs) for path, op in item["ops"] if op == "write"
        }
        kept = []
        for index, unit in enumerate(units):
            item = fs[index]
            # Do not drop a mixed batch containing an unrelated tool or preserve only half a pair.
            obsolete = (
                item["ops"]
                and len(item["ops"]) == sum(len(m.get("tool_calls", [])) for m in unit)
                and all(index < last_write.get(path, -1) for path, _ in item["ops"])
            )
            if obsolete:
                stats["superseded_count"] += len(unit)
            else:
                kept.append(unit)
        units = kept
    if cfg["collapse_enabled"]:
        kept, buffer = [], []

        def flush():
            if len(buffer) >= cfg["collapse_threshold"]:
                kept.append(summary(buffer, "Collapsed Conversation"))
                stats["collapsed_chains"] += 1
                stats["messages_collapsed"] += len(buffer)
            else:
                kept.extend(buffer)
            buffer.clear()

        for unit in units:
            if (
                len(unit) == 1
                and not unit[0].get("tool_calls")
                and unit[0]["role"] != "tool"
                and features(unit)["length"] < 200
            ):
                buffer.append(unit)
            else:
                flush()
                kept.append(unit)
        flush()
        units = kept
    if cfg["cluster_enabled"]:
        fs, assigned, clusters = [features(unit) for unit in units], set(), {}
        for index, item in enumerate(fs):
            if index in assigned or item["role"] == "system":
                continue
            members = [index] + [
                j
                for j in range(index + 1, len(fs))
                if j not in assigned
                and fs[j]["role"] != "system"
                and similarity(item, fs[j]) >= cfg["cluster_similarity_threshold"]
            ]
            if len(members) >= cfg["cluster_min_size"]:
                assigned.update(members)
                clusters[index] = members
        kept = []
        for index, unit in enumerate(units):
            if index in clusters:
                selected = [units[i] for i in clusters[index]]
                stats["clusters_found"] += 1
                stats["messages_clustered"] += sum(len(g) for g in selected)
                kept.append(summary(selected, "Clustered Conversation"))
            elif index not in assigned:
                kept.append(unit)
        units = kept
    result = [message for unit in units for message in unit]
    stats.update(
        original_message_count=original,
        final_message_count=len(result),
        tokens_saved_estimate=max(0, (len(json.dumps(messages)) - len(json.dumps(result))) // 4),
    )
    return result, stats
