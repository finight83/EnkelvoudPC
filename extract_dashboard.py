#!/usr/bin/env python3
"""
tools/extract_dashboard.py - extract the dashboard HTML from EVDCTRL.h.

The ESP32 stores the dashboard as a PROGMEM string literal:

    static const char EVDCTRL_HTML[] PROGMEM = R"HTML(<!DOCTYPE html>
    ...
    )HTML";

This tool extracts everything between R"HTML( and )HTML" and writes it
verbatim to web/dashboard.html. That is exactly the un-gzipped dashboard
the ESP32 serves on GET /.

Usage:
    python tools/extract_dashboard.py EVDCTRL.h web/dashboard.html
"""

import re
import sys
from pathlib import Path

_RAW_STRING = re.compile(r'R"HTML\((.*?)\)HTML"', re.DOTALL)


def extract(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    m = _RAW_STRING.search(text)
    if not m:
        raise SystemExit(
            "No R\"HTML(...)HTML\" literal found. "
            "Is this really EVDCTRL.h?"
        )
    return m.group(1)


def main(argv) -> int:
    if len(argv) != 3:
        print("usage: extract_dashboard.py EVDCTRL.h web/dashboard.html",
              file=sys.stderr)
        return 2
    body = extract(argv[1])
    out = Path(argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"wrote {len(body)} bytes to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))