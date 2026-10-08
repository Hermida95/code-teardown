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
        assert f"`{kind}`" in SKILL + (ROOT / "references/visual-checklist.md").read_text(encoding="utf-8") + (ROOT / "references/text-signals.md").read_text(encoding="utf-8") + (ROOT / "references/code-signals.md").read_text(encoding="utf-8")
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


def test_text_module_is_documented_and_its_flags_exist():
    helptext = subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze_text.py"), "--help"], capture_output=True, text=True).stdout
    assert "--out" in helptext and "--force" in helptext
    source = (ROOT / "scripts/analyze_text.py").read_text(encoding="utf-8")
    ids = set(re.findall(r'ev\("(tx-[a-z-]+)"', source))
    docs = (ROOT / "references/text-signals.md").read_text(encoding="utf-8")
    assert len(ids) >= 12 and not {i for i in ids if f"`{i}`" not in docs}
    assert "analyze_text.py" in SKILL and "text-signals.md" in SKILL


def test_pixel_cap_is_the_same_in_the_analyzer_and_the_scorer_and_in_the_docs():
    source = (ROOT / "scripts/analyze_pixels.py").read_text(encoding="utf-8")
    assert re.search(r"PIXEL_CAP = 0\.25", source) and se.PIXEL_CAP == 0.25
    assert "capped at weight 0.25" in (ROOT / "references/pixel-signals.md").read_text(encoding="utf-8")


def test_evals_readme_documents_every_flag_and_its_links_resolve():
    helptext = subprocess.run([sys.executable, str(ROOT / "evals/evaluate_dataset.py"), "--help"], capture_output=True, text=True).stdout
    readme = (ROOT / "evals/README.md").read_text(encoding="utf-8")
    for flag in set(re.findall(r"(--[a-z-]+)", helptext)) - {"--help"}:
        assert flag in readme, flag
    for doc in (ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "references/scoring.md", ROOT / "evals/README.md", ROOT / "evals/COLLECTING.md", ROOT / "evals/GUIA-IMAGENES.es.md", ROOT / "evals/GUIA-TEXTOS.es.md"):
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text(encoding="utf-8")):
            if not target.startswith(("http://", "https://")):
                assert (doc.parent / target).exists(), (doc.name, target)


def test_collecting_guide_matches_the_harness_defaults():
    sys.path.insert(0, str(ROOT / "evals"))
    import evaluate_dataset as ev
    guide = (ROOT / "evals/COLLECTING.md").read_text(encoding="utf-8")
    assert f"at least {ev.MIN_PER_CLASS} images per class" in guide and "--check" in guide and "COLLECTING.md" in (ROOT / "evals/README.md").read_text(encoding="utf-8")
    assert ev.TARGET_PER_CLASS == 100 and "about 100 per class" in guide


def test_the_weight_rule_in_the_evals_readme_matches_the_code():
    sys.path.insert(0, str(ROOT / "evals"))
    import evaluate_dataset as ev
    assert "min(0.97, |ln LR| / ln 100)" in (ROOT / "evals/README.md").read_text(encoding="utf-8")
    assert ev.suggested_weight(10) == 0.5 and ev.MIN_PER_CLASS == 30 and ev.DEV_SHARE == 70


def test_plugin_manifest_matches_the_repo_marketplace_and_readme():
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    market = json.loads((ROOT.parent.parent / ".claude-plugin/marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert plugin["name"] == "ai-evidence" and entry["source"] == f"./skills/{ROOT.name}"
    assert f"/plugin install ai-evidence@{market['name']}" in (ROOT / "README.md").read_text()


def test_every_entry_point_says_the_tool_does_not_accuse():
    for doc in ("SKILL.md", "README.md", "evals/README.md", "CONTRIBUTING.md", "../../README.md"):
        text = (ROOT / doc).read_text(encoding="utf-8").lower()
        assert "accuse" in text, doc
    assert "does not accuse anyone" in SKILL


def test_both_report_languages_carry_the_non_accusation_notice():
    assert "no acusa a nadie" in se.UI["es"]["disclaimer"] and "para ver, comprobar y aprender" in se.UI["es"]["disclaimer"]
    assert "does not accuse anyone" in se.UI["en"]["disclaimer"] and "seeing, checking and learning" in se.UI["en"]["disclaimer"]


def test_code_module_is_documented_and_never_runs_git():
    helptext = subprocess.run([sys.executable, str(ROOT / "scripts" / "analyze_code.py"), "--help"], capture_output=True, text=True).stdout
    for flag in ("--git-log", "--out", "--force"):
        assert flag in helptext
    assert "analyze_code.py" in SKILL and "code-signals.md" in SKILL and "--git-log" in SKILL
    source = (ROOT / "scripts/analyze_code.py").read_text(encoding="utf-8")
    assert "subprocess" not in source and "os.system" not in source and "import ast" in source
    export = "--format='%x1e%H%x1f%aI%x1f%s%x1f%b%x1d'"
    for doc in (SKILL, (ROOT / "references/code-signals.md").read_text(encoding="utf-8")):
        assert export in doc and "%an" not in doc and "%ae" not in doc


def test_the_evals_docs_cover_code():
    readme = (ROOT / "evals/README.md").read_text(encoding="utf-8")
    guide = (ROOT / "evals/COLLECTING.md").read_text(encoding="utf-8")
    assert "git-log.txt" in readme and "## Code" in readme and "--modality image\\|text\\|code" in readme and "code is not covered yet" not in readme
    assert "## Collecting code" in guide and "never runs git" in guide and "tutorial-style" in guide
    source = (ROOT / "evals/evaluate_dataset.py").read_text(encoding="utf-8")
    assert "subprocess" not in source


def test_the_spanish_image_guide_only_uses_flags_that_exist_and_keeps_the_notice():
    helptext = subprocess.run([sys.executable, str(ROOT / "evals/evaluate_dataset.py"), "--help"], capture_output=True, text=True).stdout
    guide = (ROOT / "evals/GUIA-IMAGENES.es.md").read_text(encoding="utf-8")
    for flag in set(re.findall(r"(?<![-\w])(--[a-z][a-z-]*)", guide)) - {"--with"}:          # --with is uv's own flag
        assert flag in helptext, flag
    assert "No acusa a nadie" in guide and "para ver, comprobar y aprender" in guide.lower() or "Esto es para ver, comprobar y aprender" in guide
    assert "HEIC" in guide and "--check" in guide and "GUIA-IMAGENES.es.md" in (ROOT / "evals/COLLECTING.md").read_text(encoding="utf-8")
    assert "/Users/" not in guide


def test_the_spanish_text_guide_uses_real_flags_and_matches_the_harness():
    sys.path.insert(0, str(ROOT / "evals"))
    import evaluate_dataset as ev
    helptext = subprocess.run([sys.executable, str(ROOT / "evals/evaluate_dataset.py"), "--help"], capture_output=True, text=True).stdout
    guide = (ROOT / "evals/GUIA-TEXTOS.es.md").read_text(encoding="utf-8")
    for flag in set(re.findall(r"(?<![-\w])(--[a-z][a-z-]*)", guide)):
        assert flag in helptext, flag
    assert "No acusa a nadie" in guide and "Esto es para ver, comprobar y aprender" in guide
    assert f"{ev.at.MIN_WORDS_STYLE} palabras" in guide and "segundo idioma" in guide and "expreso" in guide
    assert "GUIA-TEXTOS.es.md" in (ROOT / "evals/COLLECTING.md").read_text(encoding="utf-8") and "/Users/" not in guide
    for folder in re.findall(r"~/ai-eval-texts/(real|generated|edited)", guide):
        assert folder in ev.LABELS
