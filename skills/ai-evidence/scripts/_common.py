"""Shared helpers for the ai-evidence scripts (stdlib only).

Nothing here executes, imports or evals analyzed content.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TOKEN_VALUE = re.compile(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,}|-----BEGIN)")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def dump(data: dict) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def clean_quote(text: str, limit: int = 160) -> str:
    """Make a metadata snippet safe to show: no control characters, bounded, no token-shaped values."""
    text = CONTROL.sub(" ", text).strip()
    text = re.sub(r"\s+", " ", text)
    text = TOKEN_VALUE.sub("[redacted]", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def checked_output_path(arg: str, suffix: str, force: bool) -> Path:
    """Validate a user-supplied output file.

    The scripts can be driven by an agent that read untrusted text, so they must not be
    usable to overwrite arbitrary files: the name has to end in the expected extension and
    an existing file is only replaced with an explicit --force.
    """
    path = Path(arg).expanduser()
    if path.suffix.lower() != suffix:
        raise SystemExit(f"error: output must end with {suffix} (got {arg!r}); refusing to write elsewhere")
    if path.is_dir():
        raise SystemExit(f"error: {path} is a directory")
    if path.exists() and not force:
        raise SystemExit(f"error: {path} already exists; pass --force to overwrite it")
    return path
