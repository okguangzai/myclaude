"""Incremental SSE data framing, including multiline data, comments and EOF frames."""


async def data_frames(response, max_chars=2_000_000):
    lines, size = [], 0
    async for line in response.aiter_lines():
        if line == "":
            if lines:
                yield "\n".join(lines)
                lines, size = [], 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data":
            continue
        value = value.removeprefix(" ") if separator else ""
        size += len(value)
        if size > max_chars:
            raise ValueError("SSE frame exceeds size limit")
        lines.append(value)
    if lines:
        yield "\n".join(lines)
