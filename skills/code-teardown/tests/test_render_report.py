import ast
import base64
import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from render_report import AXES, Checker, TEMPLATE, render, validate

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_report.py"

CORE = "import os\n\ndef load(path):\n    try:\n        return open(path).read()\n    except:\n        return None\n"


def finding(fid="bare-except", verdict="bad", evidence=None, **extra):
    base = {"id": fid, "title": "Bare except hides failures", "verdict": verdict, "confidence": "high",
            "detail": "The handler swallows every exception.",
            "evidence": evidence or [{"ref": "pkg/core.py:6", "quote": "except:"}]}
    base.update(extra)
    return base


def make_findings():
    axes = []
    for axis in AXES:
        axes.append({"id": axis, "summary": f"Summary for {axis}.", "findings": [
            finding(f"{axis}-ok".replace("_", "-"), "good", title=f"{axis} fine",
                    evidence=[{"ref": "pkg/core.py:3", "quote": "def load(path):"}])]})
    axes[1]["findings"].append(finding())
    return {
        "meta": {"title": "demo teardown", "language": "en", "artifact": {"path": "demo/", "kind": "source_dir"},
                 "context": {"id": "production_service", "label": "Internal API", "asked": True}},
        "confidence": {"overall": "high", "explanation": "Real source with tests.", "degradations": []},
        "summary": {"headline": "Small and readable", "verdict": "Good structure, weak error handling."},
        "architecture": {"system": "One package that loads files.", "components": [
            {"name": "core", "role": "File loading", "modules": ["pkg.core"],
             "evidence": [{"ref": "pkg/core.py:3"}]}],
            "key_functions": [{"name": "pkg.core.load", "role": "Reads a file", "evidence": ["pkg/core.py:3-7"]}]},
        "inventory": [{"label": "Files", "value": "1"}],
        "context_weights": {"rationale": "Production service: failures matter most.",
                            **{a: "medium" for a in AXES}, "error_handling": "high"},
        "axes": axes,
        "learning": {"patterns_to_copy": [{"title": "Small functions", "why": "Easy to test.",
                                           "evidence": ["pkg/core.py:3"]}],
                     "anti_patterns": [{"title": "Bare except", "why": "Hides bugs.", "fix": "Catch OSError.",
                                        "evidence": ["pkg/core.py:6"]}]},
        "limits": ["Static analysis only."],
    }


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(CORE)
    return tmp_path


def check(data, repo=None, docker=None, extraction=None, verify=True):
    c = Checker([repo] if repo else [], docker, extraction, verify)
    validate(data, c)
    return c


def messages(c):
    return "\n".join(c.errors)


def test_valid_report_has_no_errors_and_counts_evidence(repo):
    c = check(make_findings(), repo)
    assert c.errors == []
    assert c.checked == c.total and c.total > 8


def test_render_contains_sections_and_verdicts(repo):
    data = make_findings()
    c = check(data, repo)
    page = render(data, c, TEMPLATE.read_text())
    for anchor in ("summary", "architecture", "weighting", "axes", "learning", "limits"):
        assert f'id="{anchor}"' in page
    assert 'data-verdict="bad"' in page and "Bare except hides failures" in page
    assert page.index("Findings that matter most here") < page.index('id="architecture"')
    assert "of " in page and "all resolved" in page


def test_top_findings_prefers_heavier_axis(repo):
    data = make_findings()
    data["axes"][0]["findings"].append(finding("sep-mild", "improvable", title="Mild thing",
                                              evidence=[{"ref": "pkg/core.py:1"}]))
    page = render(data, check(data, repo), TEMPLATE.read_text())
    top = page.split("<ol>")[1].split("</ol>")[0]
    assert top.index("Bare except hides failures") < top.index("Mild thing")


# --- evidence rules -----------------------------------------------------------------

def test_finding_without_evidence_is_rejected(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = []
    assert "no claim without evidence" in messages(check(data, repo))


def test_architecture_claims_need_evidence(repo):
    data = make_findings()
    data["architecture"]["components"][0]["evidence"] = []
    assert "architecture.components[0].evidence" in messages(check(data, repo))


def test_missing_file_line_out_of_range_and_bad_quote(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = [
        "pkg/nope.py:1", "pkg/core.py:999", {"ref": "pkg/core.py:6", "quote": "except ValueError:"}]
    text = messages(check(data, repo))
    assert "file not found" in text and "has only 7 line(s)" in text and "does not appear" in text


def test_traversal_and_absolute_paths_cannot_escape_root(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("x = 1\n")
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = [f"../{outside.name}/secret.py:1", f"{outside}/secret.py:1"]
    assert messages(check(data, repo)).count("file not found") == 2


def test_quote_matching_ignores_whitespace_but_not_content(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = [{"ref": "pkg/core.py:5", "quote": "return   open(path).read()"}]
    assert check(data, repo).errors == []


def test_unrecognized_reference_format(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = ["somewhere around line 12"]
    assert "unrecognized evidence reference" in messages(check(data, repo))


def test_file_evidence_without_roots_fails_loudly():
    assert "no --root given" in messages(check(make_findings(), None))


def test_no_verify_renders_banner_and_marks_unverified(repo):
    data = make_findings()
    c = check(data, repo, verify=False)
    assert c.errors == []
    page = render(data, c, TEMPLATE.read_text())
    assert 'role="alert"' in page and "NOT machine-verified" in page and 'class="ev unverified"' in page


DOCKER = {"layers": [{}, {}], "history": [{}, {}, {}], "config": {"user": None, "env": [], "exposed_ports": []}}


def test_docker_refs_checked_against_report(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = ["layer 1", "history[2]", "config.User", "config.ExposedPorts", "layer 1: etc/passwd"]
    assert check(data, repo, docker=DOCKER).errors == []
    data["axes"][1]["findings"][1]["evidence"] = ["layer 2", "history[3]", "config.Bogus"]
    text = messages(check(data, repo, docker=DOCKER))
    assert "only 2 layer" in text and "only 3 entrie" in text and "unknown config field" in text


def test_docker_refs_need_the_report():
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = ["layer 0"]
    assert "--docker-report" in messages(check(data, None))


EXTRACTION = {"weakest_confidence": "low", "obfuscation_suspected": False,
              "files": [{"path": "p/mod.pyc", "functions": [{"qualname": "run", "line": 14}]}]}


def test_bytecode_refs_checked_against_extraction(repo):
    data = make_findings()
    for axis in data["axes"]:
        for f in axis["findings"]:
            f["confidence"] = "low"
    data["confidence"]["overall"] = "low"
    data["axes"][1]["findings"][1]["evidence"] = ["p/mod.pyc :: run (line 14)", "p/mod.pyc :: run"]
    assert check(data, repo, extraction=EXTRACTION).errors == []
    data["axes"][1]["findings"][1]["evidence"] = ["p/mod.pyc :: run (line 9)", "p/mod.pyc :: missing", "q.pyc :: run"]
    text = messages(check(data, repo, extraction=EXTRACTION))
    assert "source line 14, not 9" in text and "no function or class 'missing'" in text and "no such .pyc" in text


def test_command_evidence_is_accepted_but_counted_unverifiable(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["evidence"] = ["cmd: docker history demo:latest"]
    c = check(data, repo, docker=DOCKER)
    assert c.errors == [] or all("pkg/core" in m for m in c.errors)
    assert c.unverifiable == 1


# --- guard rails on judgment -------------------------------------------------------------

def test_depends_requires_depends_on_and_context_note(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["verdict"] = "depends"
    text = messages(check(data, repo))
    assert "depends_on" in text and "context_note" in text


def test_enums_and_missing_axes(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["verdict"] = "terrible"
    data["axes"][2]["findings"][0]["confidence"] = "certain"
    del data["axes"][5]
    text = messages(check(data, repo))
    assert "terrible" in text and "certain" in text and "axis 'performance' is missing" in text


def test_not_assessed_axis_needs_reason(repo):
    data = make_findings()
    data["axes"][3] = {"id": "testability", "status": "not_assessed"}
    assert "axes[3].reason" in messages(check(data, repo))
    data["axes"][3]["reason"] = "A Docker image has no test suite to assess."
    assert check(data, repo).errors == []


def test_context_assumption_must_be_explained(repo):
    data = make_findings()
    data["meta"]["context"] = {"id": "learning", "label": "Study", "asked": False}
    assert "assumed_reason" in messages(check(data, repo))


def test_confidence_ceiling_from_extraction(repo):
    data = make_findings()
    ext = {"weakest_confidence": "low", "obfuscation_suspected": False, "files": []}
    text = messages(check(data, repo, extraction=ext))
    assert "exceeds the ceiling 'low'" in text and "confidence.overall" in text


def test_header_only_extraction_forbids_quality_findings(repo):
    ext = {"weakest_confidence": "none", "obfuscation_suspected": False, "files": []}
    assert "no quality findings are allowed" in messages(check(make_findings(), repo, extraction=ext))


def test_obfuscation_caps_confidence_and_requires_limits(repo):
    data = make_findings()
    ext = {"weakest_confidence": "medium", "obfuscation_suspected": True, "files": []}
    data["limits"] = []
    text = messages(check(data, repo, extraction=ext))
    assert "ceiling 'low'" in text and "limits section must say" in text


def test_secret_looking_text_is_refused(repo):
    data = make_findings()
    data["axes"][1]["findings"][1]["detail"] = "Token is ghp_abcdefghij1234567890 in the file."
    assert "looks like a secret" in messages(check(data, repo))


def test_malformed_structure_is_a_clean_cli_error(tmp_path):
    bad = tmp_path / "f.json"
    bad.write_text(json.dumps({"meta": [], "axes": "nope"}))
    proc = subprocess.run([sys.executable, str(SCRIPT), str(bad), "--out", str(tmp_path / "r.html")],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr


# --- output safety ---------------------------------------------------------------------------

def hostile(repo):
    data = make_findings()
    data["meta"]["title"] = "</title><script>alert('title')</script>"
    f = data["axes"][1]["findings"][1]
    f["title"] = "<img src=x onerror=alert(1)>"
    f["detail"] = '"><script>alert("detail")</script>'
    f["evidence"] = [{"ref": "pkg/core.py:6", "quote": "except:"}]
    f["id"] = "bare-except"
    data["inventory"] = [{"label": "<b>x</b>", "value": "</td><script>1</script>"}]
    data["limits"] = ["</script><script>alert('limits')</script>"]
    return data


def test_hostile_content_is_escaped_everywhere(repo):
    data = hostile(repo)
    page = render(data, check(data, repo), TEMPLATE.read_text())
    assert page.count("<script>") == 1                       # only the template's own script
    assert "<img src=x" not in page and "onerror=alert(1)>" not in page
    assert "&lt;script&gt;alert(&#x27;title&#x27;)" in page


def test_page_is_self_contained(repo):
    data = make_findings()
    page = render(data, check(data, repo), TEMPLATE.read_text())
    assert not re.search(r"(src|href)=[\"']?(https?:)?//", page)
    assert not re.search(r"url\(|@import|<link|<iframe|<img|<object|<embed|https?://", page)


def test_csp_script_hash_matches_inline_script(repo):
    data = make_findings()
    page = render(data, check(data, repo), TEMPLATE.read_text())
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert f"script-src 'sha256-{digest}'" in csp and "default-src 'none'" in csp
    assert "unsafe-eval" not in csp and "connect-src" not in csp


def test_light_dark_and_responsive_hooks(repo):
    data = make_findings()
    page = render(data, check(data, repo), TEMPLATE.read_text())
    assert "prefers-color-scheme: dark" in page and 'data-theme="dark"' in page
    assert 'name="viewport"' in page and "@media (max-width: 860px)" in page
    assert 'class="toc"' in page and 'href="#axes"' in page


def test_spanish_labels(repo):
    data = make_findings()
    data["meta"]["language"] = "es"
    page = render(data, check(data, repo), TEMPLATE.read_text())
    assert '<html lang="es"' in page and "Ponderación por contexto" in page and "Separación de responsabilidades" in page


def test_cli_success_and_failure_paths(repo, tmp_path):
    good = tmp_path / "f.json"
    good.write_text(json.dumps(make_findings()))
    out = tmp_path / "report.html"
    proc = subprocess.run([sys.executable, str(SCRIPT), str(good), "--out", str(out), "--root", str(repo)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["verified"] is True and summary["evidence_checked"] == summary["evidence_total"]
    assert out.read_text().startswith("<!doctype html>")

    broken = make_findings()
    broken["axes"][1]["findings"][1]["evidence"] = ["pkg/core.py:500"]
    good.write_text(json.dumps(broken))
    out.unlink()
    proc = subprocess.run([sys.executable, str(SCRIPT), str(good), "--out", str(out), "--root", str(repo)],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "has only 7 line(s)" in proc.stderr and not out.exists()


def test_all_scripts_parse_as_python_311():
    for path in (ROOT / "scripts").glob("*.py"):
        ast.parse(path.read_text(), feature_version=(3, 11))
