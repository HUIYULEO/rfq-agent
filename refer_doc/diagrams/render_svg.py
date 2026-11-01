"""Render every *.mmd in this folder to a *.svg via the kroki.io public API.

Kept next to the diagram sources so the SVGs are reproducible:

    python refer_doc/diagrams/render_svg.py            # render all
    python refer_doc/diagrams/render_svg.py 03 05       # render selected

Offline-first note: the SVGs are committed, so readers never need this script.
It is only used when the architecture changes and diagrams must be re-rendered.
Encoding follows kroki's GET format: zlib-compressed, URL-safe base64.
"""
from __future__ import annotations

import base64
import pathlib
import sys
import urllib.request
import zlib

KROKI = "https://kroki.io/mermaid/svg/"
UA = {"User-Agent": "rfq-agent-docs/1.0", "Accept": "image/svg+xml"}


def encode(source: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(source.encode("utf-8"))).decode().rstrip("=")


def render(mmd: pathlib.Path) -> tuple[bool, str]:
    url = KROKI + encode(mmd.read_text(encoding="utf-8"))
    request = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # noqa: F821 - (imported via urllib.request)
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"network: {exc}"
    if "<svg" not in body[:600]:
        return False, f"kroki returned no SVG: {body[:300]}"
    (mmd.with_suffix(".svg")).write_text(body, encoding="utf-8")
    return True, f"ok ({len(body)} bytes)"


def main(argv: list[str]) -> int:
    here = pathlib.Path(__file__).parent
    sources = sorted(here.glob("*.mmd"))
    if len(argv) > 1:  # filter by prefix, e.g. "03 05"
        wanted = {p.lstrip("0") for p in argv[1:]}
        sources = [p for p in sources if p.name.lstrip("0")[0] in wanted]
    failures = 0
    for mmd in sources:
        ok, note = render(mmd)
        flag = "RENDERED" if ok else "FAILED"
        print(f"{flag}  {mmd.name}  {note}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
