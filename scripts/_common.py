"""Shared helpers for the code-teardown scripts (stdlib only).

Nothing here executes, imports or evals analyzed content.
"""
from __future__ import annotations

import json
import shutil
import sys

# .pyc magic numbers: the first two bytes (little-endian) followed by b"\r\n".
# Entries are (first magic number of the range, label). A magic number belongs
# to the last range whose start is <= it. Ranges are approximate by design:
# CPython bumps the number several times per release during development.
_PYC_RANGES = [
    (0, "2.x or older"),
    (3000, "3.0-3.5"),
    (3379, "3.6"),
    (3390, "3.7"),
    (3400, "3.8"),
    (3420, "3.9"),
    (3430, "3.10"),
    (3450, "3.11"),
    (3500, "3.12"),
    (3550, "3.13"),
    (3600, "3.14"),
    (62000, "2.7"),
]


def parse_pyc_magic(header: bytes) -> dict | None:
    """Return {'magic': int, 'python': str} for a .pyc header, else None."""
    if len(header) < 4 or header[2:4] != b"\r\n":
        return None
    number = int.from_bytes(header[:2], "little")
    label = None
    for start, name in _PYC_RANGES:
        if number >= start:
            label = name
    # Python 3.14 is the newest range this table knows; anything far past it
    # is more likely not a .pyc than a future release.
    if label == "3.14" and number > 3699:
        return None
    return {"magic": number, "python": label}


def running_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def available_tools() -> dict:
    """Which optional external tools are on PATH (reported, never required)."""
    names = ["pycdc", "decompyle3", "uncompyle6", "docker"]
    return {name: bool(shutil.which(name)) for name in names}


def dump(data: dict) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
