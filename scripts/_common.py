"""Shared helpers for the code-teardown scripts (stdlib only).

Nothing here executes, imports or evals analyzed content.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

SECRET_NAME = re.compile(r"(password|passwd|secret|token|api_?key|private_?key)", re.I)

MAX_MEMBER_BYTES = 20_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_MEMBERS = 5000

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


# --- safe archive extraction -------------------------------------------------
# Archives under analysis are untrusted. Only regular files are written, names
# are validated against traversal, and size/count caps stop archive bombs.
# Extracted files are data: nothing is ever imported or executed from them.

def safe_join(dest: Path, name: str) -> Path | None:
    """Join an archive member name under dest, or None if it would escape."""
    if "\0" in name or not name:
        return None
    posix = PurePosixPath(name.replace("\\", "/"))
    if posix.is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in posix.parts:
        return None
    parts = [p for p in posix.parts if p not in ("", ".")]
    if not parts:
        return None
    target = dest.joinpath(*parts)
    try:
        target.resolve().relative_to(dest.resolve())
    except ValueError:
        return None
    return target


def extract_members(archive: zipfile.ZipFile | tarfile.TarFile, dest: Path,
                    wanted: Callable[[str], bool],
                    max_member: int = MAX_MEMBER_BYTES, max_total: int = MAX_TOTAL_BYTES,
                    max_count: int = MAX_MEMBERS) -> tuple[list[str], list[dict]]:
    """Extract wanted regular files from a zip or tar. Returns (written, skipped)."""
    written: list[str] = []
    skipped: list[dict] = []
    total = 0
    is_zip = isinstance(archive, zipfile.ZipFile)
    members = archive.infolist() if is_zip else archive
    for member in members:
        name = member.filename if is_zip else member.name
        if (member.is_dir() if is_zip else not member.isreg()):
            if not is_zip and (member.issym() or member.islnk()):
                skipped.append({"name": name, "reason": "link entries are never extracted"})
            continue
        if not wanted(name):
            continue
        target = safe_join(dest, name)
        if target is None:
            skipped.append({"name": name, "reason": "unsafe path (absolute or traversal)"})
            continue
        size = member.file_size if is_zip else member.size
        if size > max_member or total + size > max_total or len(written) >= max_count:
            skipped.append({"name": name, "reason": "size or count cap reached"})
            continue
        source = archive.open(member) if is_zip else archive.extractfile(member)
        if source is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, open(target, "wb") as out:
            out.write(source.read(max_member + 1)[:max_member])
        total += size
        written.append(target.relative_to(dest).as_posix())
    return written, skipped
