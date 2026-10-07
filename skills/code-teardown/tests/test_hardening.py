"""Hardening: hostile inputs must not hang, crash or write where they should not.

Hostile snippets run in a child process with a hard timeout: a catastrophic regex runs in
C and cannot be interrupted by a signal, so an in-process test would just hang the suite.
"""
import ast
import io
import json
import stat
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

import identify_artifact
import inspect_docker_image as docker
import inventory as inv
from builders import make_docker_tar, make_zip
from extract_pyc import extract
from render_report import AXES, TEMPLATE, Checker, render, validate

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def probe(code: str, timeout: float = 15.0):
    """Run code with scripts/ importable. Returns (returncode or None on timeout, stdout, stderr)."""
    prelude = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
    try:
        proc = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out"


def assert_fast_and_clean(code: str):
    rc, out, err = probe(code)
    assert rc is not None, "hung (timed out)"
    assert rc == 0, err[-400:]


# --- catastrophic regexes ----------------------------------------------------------------------

def test_redact_is_linear_on_hostile_image_history():
    assert_fast_and_clean('''
        from inspect_docker_image import redact, normalize_instruction
        redact("a." * 50000)
        redact('password="' * 20000)
        normalize_instruction("|1 " + "a=" * 40 + "x")
    ''')


def test_redact_still_hides_values_after_hardening():
    assert "hunter2hunter2" not in docker.redact("RUN mysql --password=hunter2hunter2 -e 'select 1'")
    assert "abc def" not in docker.redact('ENV API_TOKEN="abc def"')


def test_sql_detection_is_bounded(tmp_path):
    (tmp_path / "big.py").write_text('q = f"' + "SELECT {x} " * 30000 + '"\n')
    assert_fast_and_clean(f'''
        from inventory import SQL_RE, inventory
        SQL_RE.search("SELECT " * 60000)
        inventory({str(tmp_path)!r})
    ''')


def test_todo_scan_is_linear(tmp_path):
    (tmp_path / "t.py").write_text("x = 1  " + "#" * 80000 + "\n")
    assert_fast_and_clean(f"from inventory import inventory; inventory({str(tmp_path)!r})")


def test_todo_comments_are_still_found(tmp_path):
    (tmp_path / "t.py").write_text("x = 1  # TODO: fix\ny = 2\n# fixme later\n")
    kinds = [(s["kind"], s["line"]) for s in inv.inventory(str(tmp_path))["python"]["signals"]]
    assert ("todo_comment", 1) in kinds and ("todo_comment", 3) not in kinds    # FIXME is case-sensitive, as before


# --- deep nesting must be contained, not fatal --------------------------------------------------------

def test_deep_expression_is_reported_not_fatal(tmp_path):
    (tmp_path / "deep.py").write_text("x = a" + ".b" * 4000 + "\n")
    (tmp_path / "ok.py").write_text("def fine():\n    return 1\n")
    result = inv.inventory(str(tmp_path))
    assert [e["path"] for e in result["parse_errors"]] == ["deep.py"]
    assert result["python"]["stats"]["functions"] == 1


@pytest.mark.parametrize("name,content", [("package.json", "[" * 200000), ("pyproject.toml", "a = " + "[" * 100000)])
def test_deep_manifests_do_not_crash_inventory(tmp_path, name, content):
    (tmp_path / name).write_text(content)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert inv.inventory(str(tmp_path))["python"]["modules"]


def test_enormous_numbers_in_evidence_are_validation_errors_not_crashes():
    checker = Checker([], None, None, verify=False)
    for ref in ("a.py:" + "1" * 6000, "layer " + "9" * 6000, "history[" + "9" * 6000 + "]"):
        checker.check_evidence("e", ref)
    assert len(checker.errors) == 3 and all("unrecognized" in m for m in checker.errors)


# --- work and read budgets ---------------------------------------------------------------------------------

def test_inventory_has_a_read_budget(tmp_path, monkeypatch):
    for i in range(5):
        (tmp_path / f"m{i}.py").write_text("x = 1\n" * 1000)
    monkeypatch.setattr(inv, "MAX_TOTAL_READ", 8000)
    result = inv.inventory(str(tmp_path))
    assert any("read budget" in item for item in result["limits"])
    assert 0 < len(result["python"]["modules"]) < 5


def test_layer_declared_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(docker, "MAX_LAYER_DECLARED_BYTES", 1000)
    layers = [{"app/big.bin": "x" * 5000, "app/after.py": "y"}, {"app/ok.py": "z"}]
    report = docker.inspect(docker.TarStore(make_docker_tar(tmp_path / "i.tar", layers=layers)), "i", None)
    assert report["layers"][0].get("truncated") is True
    assert any("layer 0" in item and "declared" in item for item in report["limits"])
    assert report["layers"][1]["files"] == 1              # later layers are still analyzed


def test_identify_stops_counting_a_tar_with_absurd_declared_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(identify_artifact, "MAX_TAR_DECLARED_BYTES", 1000)
    path = tmp_path / "big.tar"
    with tarfile.open(path, "w") as tf:
        for name in ("a", "b", "c"):
            info = tarfile.TarInfo(name)
            info.size = 600
            tf.addfile(info, io.BytesIO(b"x" * 600))
    report = identify_artifact.identify(str(path))
    assert any("truncated" in item for item in report["limitations"])


# --- no writing outside the intended place -----------------------------------------------------------------

def findings_file(tmp_path):
    from test_render_report import make_findings
    path = tmp_path / "f.json"
    path.write_text(json.dumps(make_findings()))
    return path


def run_render(findings, *extra):
    return subprocess.run([sys.executable, str(SCRIPTS / "render_report.py"), str(findings), "--no-verify", *extra],
                          capture_output=True, text=True)


def test_renderer_refuses_non_html_and_existing_outputs(tmp_path):
    findings = findings_file(tmp_path)
    precious = tmp_path / "notes.txt"
    precious.write_text("keep me")
    assert run_render(findings, "--out", str(precious)).returncode != 0
    assert precious.read_text() == "keep me"
    existing = tmp_path / "report.html"
    existing.write_text("old")
    proc = run_render(findings, "--out", str(existing))
    assert proc.returncode != 0 and "--force" in proc.stderr and existing.read_text() == "old"
    assert run_render(findings, "--out", str(existing), "--force").returncode == 0
    assert existing.read_text().startswith("<!doctype html>")


def test_inventory_refuses_non_json_and_existing_outputs(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    script = str(SCRIPTS / "inventory.py")
    target = tmp_path / "rc"
    target.write_text("keep me")
    assert subprocess.run([sys.executable, script, str(tmp_path), "--out", str(target)], capture_output=True).returncode != 0
    assert target.read_text() == "keep me"
    out = tmp_path / "inv.json"
    assert subprocess.run([sys.executable, script, str(tmp_path), "--out", str(out)], capture_output=True).returncode == 0
    assert subprocess.run([sys.executable, script, str(tmp_path), "--out", str(out)], capture_output=True).returncode != 0
    assert subprocess.run([sys.executable, script, str(tmp_path), "--out", str(out), "--force"], capture_output=True).returncode == 0


def test_workdir_that_is_a_file_is_a_clean_error(tmp_path):
    squatter = tmp_path / "work"
    squatter.write_text("x")
    tar_path = make_docker_tar(tmp_path / "i.tar")
    for argv in ([str(SCRIPTS / "inspect_docker_image.py"), str(tar_path), "--out", str(squatter)],
                 [str(SCRIPTS / "extract_pyc.py"), str(tar_path), "--out", str(squatter)]):
        proc = subprocess.run([sys.executable, *argv], capture_output=True, text=True)
        assert proc.returncode != 0 and "Traceback" not in proc.stderr


def test_template_placeholders_in_user_text_are_not_expanded():
    from test_render_report import make_findings
    data = make_findings()
    data["meta"]["title"] = "@@BODY@@ @@NAV@@ @@FOOTER@@"
    checker = Checker([], None, None, verify=False)
    validate(data, checker)
    page = render(data, checker, TEMPLATE.read_text())
    assert page.count('<nav class="toc"') == 1 and page.count("<footer>") == 1
    assert "@@BODY@@ @@NAV@@ @@FOOTER@@" in page


# --- option injection ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("ref", ["-o/tmp/x", "--help", "a b", "x;rm -rf /", "", "a\nb", "$(id)"])
def test_docker_reference_cannot_inject_options_or_shell(tmp_path, monkeypatch, ref):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "called"
    fake = bin_dir / "docker"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    with pytest.raises(docker.ImageError):
        docker.save_image(ref, tmp_path / "out.tar")
    assert not marker.exists()


def test_decompiler_gets_an_absolute_path(tmp_path, monkeypatch):
    import py_compile
    src = tmp_path / "m.py"
    src.write_text("x = 1\n")
    pyc = tmp_path / "-dash.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv"
    tool = bin_dir / "pycdc"
    tool.write_text(f'#!/bin/sh\necho "$1" > {log}\necho "x = 1"\n')
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.chdir(tmp_path)
    extract("./-dash.pyc", str(tmp_path / "w"))
    assert log.read_text().strip().startswith("/")


# --- archives ------------------------------------------------------------------------------------------------

def test_hostile_tar_cannot_write_outside_or_via_links(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        for name, kind in (("../../escape.pyc", "file"), ("/abs/escape.pyc", "file"), ("link.pyc", "symlink"), ("ok/real.py", "file")):
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(victim)
                tf.addfile(info)
            else:
                data = b"x = 1\n"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    report = extract(str(evil), str(tmp_path / "work"))
    assert not (tmp_path / "escape.pyc").exists() and not Path("/abs/escape.pyc").exists()
    assert not any(victim.iterdir())
    assert (tmp_path / "work" / "source" / "ok" / "real.py").is_file()
    reasons = " ".join(s["reason"] for s in report["skipped"])
    assert "unsafe path" in reasons and "link" in reasons


def test_layer_that_turns_a_file_into_a_directory_does_not_crash(tmp_path):
    layers = [{"app/conf.py": "x = 1\n"}, {"app/.wh.conf.py": "", "app/conf.py/inner.py": "y = 2\n"}]
    report = docker.inspect(docker.TarStore(make_docker_tar(tmp_path / "i.tar", layers=layers)), "i", tmp_path / "w")
    assert report["totals"]["layers"] == 2


def test_symlinked_evidence_cannot_escape_the_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret line\n")
    (root / "link.py").symlink_to(outside)
    checker = Checker([root], None, None, verify=True)
    checker.check_evidence("e", {"ref": "link.py:1", "quote": "top secret"})
    assert checker.errors and "file not found" in checker.errors[0]


def test_inventory_ignores_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("PASSWORD = 'this-should-never-be-read'\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "dir_link").symlink_to(outside, target_is_directory=True)
    (repo / "file_link.py").symlink_to(outside / "secret.py")
    result = inv.inventory(str(repo))
    assert [m["path"] for m in result["python"]["modules"]] == ["a.py"]


def test_image_directory_with_symlinked_layer_cannot_read_outside(tmp_path):
    image = tmp_path / "img"
    image.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    (image / "manifest.json").symlink_to(outside)
    with pytest.raises(docker.ImageError):
        docker.load_image(docker.DirStore(image))


# --- escaping, fuzz --------------------------------------------------------------------------------------------

PAYLOAD = "<u9x>&'\"</u9x>"
KEEP = {"id", "ref", "verdict", "confidence", "overall", "language", "kind", "generated", "status"}


def poison(node, key=None):
    """Put the payload into every free-text string; keep ids, enums and evidence refs valid."""
    if key == "evidence" and isinstance(node, list):
        return [({**item, "quote": PAYLOAD} if isinstance(item, dict) else item) for item in node]
    if isinstance(node, str):
        return node if key in KEEP or key in AXES else PAYLOAD
    if isinstance(node, dict):
        return {k: (v if k in ("id", "language") and isinstance(v, str) else
                    v if k == "context_weights" else poison(v, k)) for k, v in node.items()}
    if isinstance(node, list):
        return [poison(v, key) for v in node]
    return node


def test_every_free_text_field_is_escaped():
    from test_render_report import make_findings
    data = make_findings()
    data["context_weights"]["rationale"] = PAYLOAD
    data = poison(data)
    data["meta"]["context"]["id"] = "production_service"
    data["meta"]["language"] = "en"
    data["confidence"]["overall"] = "high"
    checker = Checker([], None, None, verify=False)
    validate(data, checker)
    assert checker.errors == [], checker.errors[:3]
    page = render(data, checker, TEMPLATE.read_text())
    assert "<u9x>" not in page and "</u9x>" not in page
    assert page.count("&lt;u9x&gt;") > 10                       # the payload really reached many places


# --- Python version guard --------------------------------------------------------------------------------------

def test_scripts_parse_on_older_python_and_guard_comes_first():
    for path in SCRIPTS.glob("*.py"):
        text = path.read_text()
        tree = ast.parse(text, feature_version=(3, 9))
        if path.name == "_common.py":
            continue
        seen_guard = False
        for node in tree.body:
            if isinstance(node, ast.If) and "version_info" in ast.unparse(node.test):
                seen_guard = True
                break
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            if isinstance(node, ast.Import) and [a.name for a in node.names] == ["sys"]:
                continue
            if isinstance(node, ast.Expr):            # the module docstring
                continue
            pytest.fail(f"{path.name}: {ast.unparse(node)[:60]!r} comes before the Python version check")
        assert seen_guard, f"{path.name} has no Python version check"


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="system python not found")
def test_old_python_gets_a_friendly_message():
    old = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True)
    if old.returncode != 0 or ast.literal_eval(old.stdout.strip()) >= (3, 11):
        pytest.skip("system python is 3.11 or newer")
    for script in ("identify_artifact.py", "inventory.py", "extract_pyc.py", "inspect_docker_image.py", "render_report.py"):
        proc = subprocess.run(["/usr/bin/python3", str(SCRIPTS / script), "--help"], capture_output=True, text=True)
        assert proc.returncode != 0 and "3.11" in proc.stderr and "Traceback" not in proc.stderr, script


def test_skill_does_not_preapprove_commands():
    """Pre-approved scripts would let injected instructions run them (and their --out writes) unprompted."""
    front = (ROOT / "SKILL.md").read_text().split("\n---\n", 1)[0]
    assert "allowed-tools" not in front


def test_overlong_file_names_do_not_break_extraction(tmp_path):
    import py_compile
    src = tmp_path / "m.py"
    src.write_text("x = 1\n")
    pyc = tmp_path / "m.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    data = pyc.read_bytes()
    wheel = make_zip(tmp_path / "long.whl", {"pkg/" + "a" * 300 + ".pyc": data, "pkg/ok.pyc": data})
    report = extract(str(wheel), str(tmp_path / "w1"))
    assert [f["path"] for f in report["files"]] == ["pkg/ok.pyc"]
    assert any("too long" in s["reason"] for s in report["skipped"])

    folder = tmp_path / "dir"
    folder.mkdir()
    (folder / ("b" * 250 + ".pyc")).write_bytes(data)          # legal on disk, but ".dis.txt" would push it over 255
    report = extract(str(folder), str(tmp_path / "w2"))
    assert len(report["files"]) == 1 and report["files"][0]["method"] in ("dis", "header-only")
