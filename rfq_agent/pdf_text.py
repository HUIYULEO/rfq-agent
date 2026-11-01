"""Minimal PDF text extraction (stdlib only).

The official Quote.pdf files are generated with Helvetica and an explicit text matrix
per run, so grouping runs by y and sorting by x reconstructs the table reliably.
This is deliberately not a general PDF reader: it handles the subset these documents
use, and raises instead of guessing when it meets something else.
"""
from __future__ import annotations

import re
import zlib

TOKEN = re.compile(
    rb"(?P<str>\((?:[^()\\]|\\.)*\))"
    rb"|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<arr>\[(?:[^\[\]]|\\.)*\]\s*TJ)"
    rb"|(?P<num>[-+]?\d*\.?\d+)"
    rb"|(?P<op>[A-Za-z*'\"]{1,3})",
    re.S,
)
ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
           b"(": b"(", b")": b")", b"\\": b"\\"}


def _unescape(raw: bytes) -> str:
    """Resolve PDF literal-string escapes, including octal and line continuations."""
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i:i + 1]
        if ch != b"\\":
            out += ch
            i += 1
            continue
        nxt = raw[i + 1:i + 2]
        if nxt in ESCAPES:
            out += ESCAPES[nxt]
            i += 2
        elif nxt.isdigit():
            digits = raw[i + 1:i + 4]
            take = 0
            while take < len(digits) and digits[take:take + 1].isdigit():
                take += 1
            out.append(int(digits[:take], 8) & 0xFF)
            i += 1 + take
        elif nxt in (b"\n", b"\r"):
            i += 2
        else:
            out += nxt
            i += 2
    return out.decode("latin-1")


def _streams(data: bytes):
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        body = match.group(1)
        header = data[max(0, match.start() - 300):match.start()]
        if b"/FlateDecode" in header:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        yield body


def extract_runs(data: bytes) -> list[tuple[int, float, float, str]]:
    """(page, x, y, text) for every text run, in stream order."""
    runs = []
    for page, stream in enumerate(_streams(data), start=1):
        x = y = 0.0
        numbers: list[float] = []
        for match in TOKEN.finditer(stream):
            if match.group("num"):
                numbers.append(float(match.group("num")))
                continue
            if match.group("op") == b"Tm" and len(numbers) >= 6:
                x, y = numbers[-2], numbers[-1]
                numbers.clear()
                continue
            if match.group("op") in (b"Td", b"TD") and len(numbers) >= 2:
                x += numbers[-2]
                y += numbers[-1]
                numbers.clear()
                continue
            if match.group("op") == b"T*":
                y -= 12.0
                numbers.clear()
                continue
            if match.group("str"):
                text = _unescape(match.group("str")[1:-1])
                if text.strip():
                    runs.append((page, x, y, text))
                numbers.clear()
                continue
            if match.group("arr"):
                parts = re.findall(rb"\((?:[^()\\]|\\.)*\)", match.group("arr"))
                text = "".join(_unescape(p[1:-1]) for p in parts)
                if text.strip():
                    runs.append((page, x, y, text))
                numbers.clear()
                continue
            numbers.clear()
    return runs


def extract_lines(data: bytes, y_tolerance: float = 3.0) -> list[str]:
    """Rebuild visual lines: same y (within tolerance), ordered left to right."""
    runs = extract_runs(data)
    rows: list[list[tuple[int, float, float, str]]] = []
    for run in runs:
        if rows and rows[-1][0][0] == run[0] and abs(rows[-1][0][2] - run[2]) <= y_tolerance:
            rows[-1].append(run)
        else:
            rows.append([run])
    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda item: item[1])
        text = ""
        last_end = None
        for _, x, _, chunk in row:
            if last_end is not None and x - last_end > 6:
                text += " | "
            text += chunk
            last_end = x + len(chunk) * 5
        lines.append(re.sub(r"\s+", " ", text).strip())
    return lines