import json
import re
import subprocess
import sys
from pathlib import Path

import score_evidence as se

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


def frontmatter() -> dict:
    block = SKILL.split("---")[1]
    return dict(re.findall(r"^([a-z-]+):\s*(.+)$", block, re.M))


def test_frontmatter_is_valid():
    meta = frontmatter()
    assert meta["name"] == "ai-evidence" == ROOT.name
    assert 100 < len(meta["description"]) <= 1024
    assert meta["license"] == "MIT"


def test_skill_stays_short_and_every_link_resolves():
    assert len(SKILL.splitlines()) < 500
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", SKILL):
        assert (ROOT / target).exists(), target


def test_flags_in_skill_exist_in_the_scripts():
    for script, flags in {"analyze_image.py": ["--out", "--force"],
                          "score_evidence.py": ["--visual", "--pixels", "--out", "--json-out", "--lang", "--force"]}.items():
        helptext = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], capture_output=True, text=True).stdout
        for flag in flags:
            assert flag in helptext, (script, flag)


def test_visual_example_in_skill_is_accepted_by_the_validator(tmp_path):
    example = re.search(r"```json\n(\{\"observations\".*?)\n```", SKILL, re.S).group(1)
    path = tmp_path / "v.json"
    path.write_text(example, encoding="utf-8")
    items = se.load_visual(str(path))
    assert [i["source"] for i in items] == ["visual", "visual"]


def test_kinds_and_caps_in_docs_match_the_code():
    for kind, cap in se.VISUAL_CAPS.items():
        assert f"`{kind}`" in SKILL + (ROOT / "references/visual-checklist.md").read_text(encoding="utf-8")
    scoring = (ROOT / "references/scoring.md").read_text(encoding="utf-8")
    for kind in ("known_watermark", "anatomy_text_errors", "physics_lighting", "natural_cues", "texture_style"):
        assert f"`{kind}` {se.VISUAL_CAPS[kind]}" in scoring


def test_every_evidence_id_the_analyzer_can_emit_is_documented():
    source = (ROOT / "scripts/analyze_image.py").read_text(encoding="utf-8")
    ids = set(re.findall(r'ev\("([a-z0-9-]+)"', source))
    docs = (ROOT / "references/image-signals.md").read_text(encoding="utf-8")
    missing = {i for i in ids if f"`{i}`" not in docs}
    assert not missing, missing


def test_pixel_module_flags_and_ids_are_documented():
    helptext = subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze_pixels.py"), "--help"], capture_output=True, text=True).stdout
    assert "--out" in helptext and "--force" in helptext
    source = (ROOT / "scripts/analyze_pixels.py").read_text(encoding="utf-8")
    ids = set(re.findall(r'px\("(px-[a-z-]+)"', source))
    assert ids, "no pixel evidence ids found"
    docs = (ROOT / "references/pixel-signals.md").read_text(encoding="utf-8")
    assert not {i for i in ids if f"`{i}`" not in docs}
    assert "analyze_pixels.py" in SKILL and "--pixels" in SKILL and "pixel-signals.md" in SKILL


def test_pixel_cap_is_the_same_in_the_analyzer_and_the_scorer_and_in_the_docs():
    source = (ROOT / "scripts/analyze_pixels.py").read_text(encoding="utf-8")
    assert re.search(r"PIXEL_CAP = 0\.25", source) and se.PIXEL_CAP == 0.25
    assert "capped at weight 0.25" in (ROOT / "references/pixel-signals.md").read_text(encoding="utf-8")


def test_plugin_manifest_matches_the_repo_marketplace_and_readme():
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    market = json.loads((ROOT.parent.parent / ".claude-plugin/marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert plugin["name"] == "ai-evidence" and entry["source"] == f"./skills/{ROOT.name}"
    assert f"/plugin install ai-evidence@{market['name']}" in (ROOT / "README.md").read_text()
