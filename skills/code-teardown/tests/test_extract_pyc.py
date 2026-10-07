import json
import py_compile
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from builders import make_zip
from extract_pyc import extract, header_info

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "extract_pyc.py"

SOURCE = '''import os
import json


API_TOKEN = "abcdef123456"


class Store:
    def load(self, path):
        return json.load(open(path))


def run(cmd):
    def inner():
        return eval(cmd)
    if cmd and os.name:
        return inner()
    return None
'''


def line_of(text, needle):
    return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)


def compile_source(tmp_path, source=SOURCE, name="mod"):
    src = tmp_path / f"{name}.py"
    src.write_text(source)
    out = tmp_path / f"{name}.pyc"
    py_compile.compile(str(src), cfile=str(out), doraise=True)
    src.unlink()
    return out


def foreign_pyc(tmp_path, magic=3413, name="old"):
    path = tmp_path / f"{name}.pyc"
    path.write_bytes(struct.pack("<H", magic) + b"\r\n" + b"\0" * 12 + b"some_readable_identifier\0another_string_value")
    return path


@pytest.fixture
def no_decompilers(monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def fake_tool(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def test_header_layouts():
    modern = header_info(struct.pack("<H", 3495) + b"\r\n" + struct.pack("<I", 1) + b"\xaa" * 8, 3495)
    assert modern["length"] == 16 and modern["hash_based"] and modern["source_hash"] == "aa" * 8
    timestamp = header_info(b"\0\0\r\n" + struct.pack("<III", 0, 1700000000, 42), 3495)
    assert timestamp["mtime"] == 1700000000 and timestamp["source_size"] == 42
    assert header_info(b"x" * 12, 3379)["length"] == 12
    assert header_info(b"x" * 8, 62211)["length"] == 8


def test_same_version_pyc_uses_dis_with_evidence(tmp_path, no_decompilers):
    pyc = compile_source(tmp_path)
    report = extract(str(pyc), str(tmp_path / "work"))
    rec = report["files"][0]
    assert rec["method"] == "dis" and rec["quality_judgment_confidence"] == "low"
    by_name = {f["qualname"]: f for f in rec["functions"]}
    assert by_name["run"]["line"] == line_of(SOURCE, "def run")
    assert by_name["Store"]["kind"] == "class"
    assert by_name["Store.load"]["line"] == line_of(SOURCE, "def load")
    assert "inner" in "".join(by_name) and by_name["run.<locals>.inner"]["kind"] == "function"
    assert {i["module"] for i in rec["imports"]} == {"os", "json"}
    signal_kinds = {s["kind"]: s for s in rec["signals"]}
    assert signal_kinds["eval_exec"]["line"] == line_of(SOURCE, "eval(cmd)")
    assert signal_kinds["hardcoded_secret_candidate"]["line"] == line_of(SOURCE, "API_TOKEN")
    assert "abcdef123456" not in json.dumps(report)
    assert "abcdef123456" not in (tmp_path / "work" / rec["dis_path"]).read_text()


def test_dis_ref_points_at_real_section(tmp_path, no_decompilers):
    report = extract(str(compile_source(tmp_path)), str(tmp_path / "work"))
    rec = report["files"][0]
    fn = next(f for f in rec["functions"] if f["qualname"] == "run")
    path, line = fn["dis_ref"].rsplit(":", 1)
    lines = (tmp_path / "work" / path).read_text().splitlines()
    assert lines[int(line) - 1].startswith("== run [function]")


def test_foreign_version_degrades_to_header_only(tmp_path, no_decompilers):
    report = extract(str(foreign_pyc(tmp_path)), str(tmp_path / "work"))
    rec = report["files"][0]
    assert rec["method"] == "header-only"
    assert rec["python_version"] == "3.8"
    assert rec["quality_judgment_confidence"] == "none"
    assert "some_readable_identifier" in rec["raw_strings"]
    assert report["weakest_confidence"] == "none"
    assert any("pycdc" in n for n in rec["notes"])


def test_corrupt_body_with_matching_version_does_not_crash(tmp_path, no_decompilers):
    good = compile_source(tmp_path)
    data = good.read_bytes()
    bad = tmp_path / "bad.pyc"
    bad.write_bytes(data[:16] + b"\xff" * 64)
    rec = extract(str(bad), str(tmp_path / "work"))["files"][0]
    assert rec["method"] == "header-only"
    assert any("failed" in n or "invalid" in n for n in rec["notes"])


def test_not_a_pyc(tmp_path, no_decompilers):
    junk = tmp_path / "junk.pyc"
    junk.write_bytes(b"hello world, definitely not bytecode")
    rec = extract(str(junk), str(tmp_path / "work"))["files"][0]
    assert rec["error"].startswith("not a .pyc")


def test_decompiler_used_when_available(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    fake_tool(bin_dir, "pycdc", 'echo "def run(cmd):\\n    pass"')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    report = extract(str(compile_source(tmp_path)), str(tmp_path / "work"))
    rec = report["files"][0]
    assert rec["method"] == "decompiler:pycdc"
    assert rec["quality_judgment_confidence"] == "medium"
    assert "def run" in (tmp_path / "work" / rec["decompiled_path"]).read_text()
    assert rec["dis_path"]  # dis is still kept as a second source of evidence


def test_failing_decompiler_falls_back(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    fake_tool(bin_dir, "pycdc", "exit 3")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    rec = extract(str(compile_source(tmp_path)), str(tmp_path / "work"))["files"][0]
    assert rec["method"] == "dis"
    assert any("exit 3" in n for n in rec["notes"])


def test_wheel_extracts_py_and_pyc_and_rejects_zip_slip(tmp_path, no_decompilers):
    pyc = compile_source(tmp_path)
    wheel = make_zip(tmp_path / "p-1.0-py3-none-any.whl", {
        "p/__init__.py": "x = 1\n",
        "p/mod.cpython-x.pyc": pyc.read_bytes(),
        "../escape.pyc": pyc.read_bytes(),
        "/abs.pyc": pyc.read_bytes(),
        "p/native.cpython-x.so": b"\x7fELF",
    })
    report = extract(str(wheel), str(tmp_path / "work"))
    assert [f["path"] for f in report["files"]] == ["p/mod.cpython-x.pyc"]
    assert report["source_files_extracted"] == 1
    assert (tmp_path / "work" / "source" / "p" / "__init__.py").is_file()
    assert len(report["skipped"]) == 2 and all("unsafe path" in s["reason"] for s in report["skipped"])
    assert not (tmp_path / "escape.pyc").exists() and not Path("/abs.pyc").exists()
    assert report["native_extensions"] == ["p/native.cpython-x.so"]
    assert any("native extension" in item for item in report["limits"])


def test_directory_with_pycache(tmp_path, no_decompilers):
    pyc = compile_source(tmp_path)
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "a.cpython.pyc").write_bytes(pyc.read_bytes())
    pyc.unlink()
    report = extract(str(tmp_path / "pkg"), str(tmp_path / "work"))
    assert [f["path"] for f in report["files"]] == ["__pycache__/a.cpython.pyc"]


def test_obfuscation_detected_and_limits_declared(tmp_path, no_decompilers):
    source = "import base64, zlib\nexec(zlib.decompress(base64.b64decode('eJwDAAAAAAE=')))\n"
    report = extract(str(compile_source(tmp_path, source, "obf")), str(tmp_path / "work"))
    assert report["obfuscation_suspected"] is True
    kinds = {f["kind"] for f in report["files"][0]["obfuscation"]}
    assert "decode_then_exec" in kinds
    assert any("Obfuscation suspected" in item for item in report["limits"])


def test_clean_code_is_not_flagged(tmp_path, no_decompilers):
    report = extract(str(compile_source(tmp_path)), str(tmp_path / "work"))
    assert report["obfuscation_suspected"] is False


def test_never_executes_bytecode(tmp_path, no_decompilers):
    marker = tmp_path / "pwned"
    source = f"open({str(marker)!r}, 'w').write('x')\n"
    extract(str(compile_source(tmp_path, source, "evil")), str(tmp_path / "work"))
    assert not marker.exists()


def test_refuses_non_empty_workdir(tmp_path, no_decompilers):
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        extract(str(compile_source(tmp_path)), str(work))


def test_cli_prints_json_and_writes_extraction_file(tmp_path):
    pyc = compile_source(tmp_path)
    work = tmp_path / "work"
    proc = subprocess.run([sys.executable, str(SCRIPT), str(pyc), "--out", str(work)],
                          capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)["files"][0]["python_version"]
    assert (work / "extraction.json").is_file()
