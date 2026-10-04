"""Frozen upstream PDF text helper: literal text operators and Flate streams.

This is not OCR or a full PDF renderer. It is an explicit helper, never an
automatic file read merely because a prompt mentions a path.
"""

import re
import zlib
from pathlib import Path


def literal(text, start=0):
    index = text.find("(", start)
    if index < 0:
        return None, len(text)
    depth, result = 1, []
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 1
            char = text[index]
            if char in "01234567":
                end = index + 1
                while end < min(index + 3, len(text)) and text[end] in "01234567":
                    end += 1
                result.append(chr(int(text[index:end], 8)))
                index = end
                continue
            result.append({"n": "\n", "r": "\r", "t": "\t"}.get(char, char))
        elif char == "(":
            depth += 1
            result.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(result), index + 1
            result.append(char)
        else:
            result.append(char)
        index += 1
    return None, index


def stream_text(data):
    result, active = "", False
    for line in data.decode("latin-1").splitlines():
        text = line.strip()
        if text == "BT":
            active = True
        elif text == "ET":
            active = False
        elif active:
            pieces, newline = [], False
            if text.endswith("Tj"):
                value, _ = literal(text)
                if value is not None:
                    pieces.append(value)
            elif text.endswith("TJ") and "[" in text and "]" in text:
                inner, index = text[text.find("[") + 1 : text.rfind("]")], 0
                while index < len(inner):
                    value, index = literal(inner, index)
                    if value is None:
                        break
                    pieces.append(value)
            elif text.endswith(("'", '"')) and "(" in text:
                value, _ = literal(text)
                if value is not None:
                    pieces.append(value)
                    newline = True
            if pieces:
                if result:
                    result += "\n" if newline else "" if result.endswith("\n") else " "
                result += "".join(pieces)
    return result


def extract_text_from_bytes(data, max_expanded_bytes=8_000_000):
    if len(data) > 8_000_000:
        raise ValueError("PDF helper input exceeds 8 MB")
    result, offset, remaining = [], 0, max_expanded_bytes
    while (start := data.find(b"stream", offset)) >= 0:
        content_start = start + 6
        if data[content_start : content_start + 2] == b"\r\n":
            content_start += 2
        elif data[content_start : content_start + 1] in {b"\r", b"\n"}:
            content_start += 1
        end = data.find(b"endstream", content_start)
        if end < 0:
            break
        raw = data[content_start:end]
        offset = end + len(b"endstream")
        if b"FlateDecode" in data[max(0, start - 512) : start]:
            try:
                decoder = zlib.decompressobj()
                raw = decoder.decompress(raw, remaining + 1)
                if len(raw) > remaining or decoder.unconsumed_tail:
                    raise ValueError("PDF decompression exceeds budget")
            except zlib.error:
                continue
        remaining -= len(raw)
        if remaining < 0:
            raise ValueError("PDF text exceeds budget")
        text = stream_text(raw)
        if text:
            result.append(text)
    return "\n".join(result)


def extract_text(path):
    path = Path(path)
    if path.stat().st_size > 8_000_000:
        raise ValueError("PDF helper input exceeds 8 MB")
    return extract_text_from_bytes(path.read_bytes())


def looks_like_pdf_path(text):
    for token in text.split():
        cleaned = token.strip("'\"`")
        if re.search(r".+\.pdf$", cleaned, re.I):
            return cleaned
    return None


def maybe_extract_pdf_from_prompt(prompt, workspace):
    from .tools import safe_path

    name = looks_like_pdf_path(prompt)
    if name is None:
        return None
    try:
        text = extract_text(safe_path(workspace, name))
        return (name, text) if text else None
    except (OSError, ValueError):
        return None
