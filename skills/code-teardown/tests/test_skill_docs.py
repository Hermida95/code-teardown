"""Keep SKILL.md and references/ honest: links resolve, flags exist, the schema example validates."""
import json
import re
from pathlib import Path

import pytest

from render_report import AXES, CONTEXTS, WEIGHTS, Checker, validate

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
REFERENCES = sorted((ROOT / "references").glob("*.md"))


def links(text: str) -> list[str]:
    return [m for m in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text) if not m.startswith("http")]


def test_every_link_in_skill_md_resolves():
    for target in links(SKILL):
        assert (ROOT / target).exists(), target


def test_every_reference_is_linked_from_skill_md():
    linked = {Path(t).name for t in links(SKILL)}
    assert {p.name for p in REFERENCES} <= linked


def test_references_are_one_level_deep_and_sized():
    for path in REFERENCES:
        assert path.parent == ROOT / "references"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 500, path.name
        if len(lines) > 100:
            assert any(line.startswith("Contents:") for line in lines[:10]), f"{path.name} needs a contents line"


def test_scripts_named_in_skill_md_exist():
    for name in set(re.findall(r"\b([a-z_]+\.py)\b", SKILL)):
        assert (ROOT / "scripts" / name).exists(), name


def test_flags_in_docs_exist_in_scripts():
    scripts = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "scripts").glob("*.py"))
    docs = SKILL + "".join(p.read_text(encoding="utf-8") for p in REFERENCES)
    external = {"--python"}            # flags of other tools mentioned in the docs (uv)
    for flag in set(re.findall(r"`?(--[a-z][a-z-]+)", docs)) - external:
        assert f'"{flag}"' in scripts, f"{flag} is documented but no script defines it"


def test_skill_uses_portable_script_path():
    assert "${CLAUDE_SKILL_DIR}/scripts/" in SKILL
    assert "/Users/" not in SKILL


def test_description_has_the_triggers():
    description = re.search(r"^description: (.+)$", SKILL, re.M).group(1).lower()
    for word in (".pyc", "docker", "source", "verdict", "evidence", "not for malware"):
        assert word in description or word.replace("not for ", "not ") in description, word


def test_ground_rules_are_present():
    for phrase in ("Static analysis only", "data, never instructions", "Evidence or silence",
                   "Declare confidence", "Never quote secrets", "licence"):
        assert phrase in SKILL, phrase


def example_findings() -> dict:
    text = (ROOT / "references" / "report-schema.md").read_text(encoding="utf-8")
    block = text.split("## Minimal complete example", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def test_schema_example_validates():
    checker = Checker([], None, None, verify=False)
    validate(example_findings(), checker)
    assert checker.errors == []


def test_schema_example_covers_all_axes_and_enums():
    data = example_findings()
    assert [a["id"] for a in data["axes"]] == AXES
    verdicts = {f["verdict"] for a in data["axes"] for f in a.get("findings", [])}
    assert {"good", "improvable", "depends"} <= verdicts


def context_matrix() -> dict:
    text = (ROOT / "references" / "context-weighting.md").read_text(encoding="utf-8")
    table = text.split("## Matrix", 1)[1].split("Why each context", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| ---")]
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    matrix = {}
    for row in rows[1:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        matrix[cells[0]] = dict(zip(header[1:], cells[1:]))
    return matrix


def test_context_matrix_is_complete_and_valid():
    matrix = context_matrix()
    assert list(matrix) == AXES
    for axis, row in matrix.items():
        assert set(row) == {c for c in CONTEXTS if c != "other"}
        assert set(row.values()) <= set(WEIGHTS)


@pytest.mark.parametrize("path", REFERENCES, ids=lambda p: p.name)
def test_reference_mentions_no_machine_paths(path):
    assert "/Users/" not in path.read_text(encoding="utf-8")


def test_readme_local_links_and_images_exist():
    for readme in (ROOT / "README.md", ROOT / "evals" / "README.md"):
        text = readme.read_text(encoding="utf-8")
        targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text)
        for target in targets:
            if target.startswith(("http://", "https://")):
                continue
            assert (readme.parent / target).exists(), f"{readme.name}: broken link {target}"


def test_readme_documented_commands_match_the_plugin_name():
    import json
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT.parent.parent / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == manifest["name"])
    readme = (ROOT / "README.md").read_text()
    assert manifest["name"] == "code-teardown" and entry["source"] == f"./skills/{ROOT.name}"
    assert f"claude plugin install {manifest['name']}@{market['name']}" in readme
