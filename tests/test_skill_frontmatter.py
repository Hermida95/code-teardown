"""Validate SKILL.md frontmatter against the Agent Skills spec."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def parse_frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must start with frontmatter"
    block = text.split("\n---\n", 1)[0][4:]
    fields = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def load():
    return parse_frontmatter((ROOT / "SKILL.md").read_text(encoding="utf-8"))


def test_name_valid_and_matches_directory():
    name = load()["name"]
    assert len(name) <= 64
    assert NAME_RE.match(name)
    assert name == ROOT.name


def test_description_within_spec_limit():
    description = load()["description"]
    assert 0 < len(description) <= 1024


def test_compatibility_within_limit():
    assert len(load().get("compatibility", "")) <= 500


def test_skill_body_under_500_lines():
    assert len((ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500
