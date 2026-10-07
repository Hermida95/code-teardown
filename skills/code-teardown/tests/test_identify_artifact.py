import json
import py_compile
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from _common import parse_pyc_magic, running_python
from builders import make_docker_tar, make_zip
from identify_artifact import identify

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "identify_artifact.py"


def compile_pyc(tmp_path: Path) -> Path:
    src = tmp_path / "mod.py"
    src.write_text("def f():\n    return 1\n")
    out = tmp_path / "mod.cpython.pyc"
    py_compile.compile(str(src), cfile=str(out))
    return out


def test_pyc_magic_matches_running_interpreter(tmp_path):
    report = identify(str(compile_pyc(tmp_path)))
    assert report["kind"] == "pyc_file"
    assert report["python_version"] == running_python()
    assert report["same_as_running_python"] is True
    assert report["recommended_scripts"] == ["extract_pyc.py"]


def test_foreign_pyc_warns_about_dis(tmp_path):
    foreign = tmp_path / "old.pyc"
    foreign.write_bytes(struct.pack("<H", 3413) + b"\r\n" + b"\0" * 12)  # 3.8
    report = identify(str(foreign))
    assert report["kind"] == "pyc_file"
    assert report["python_version"] == "3.8"
    assert report["same_as_running_python"] is False
    assert any("dis" in item for item in report["limitations"])


@pytest.mark.parametrize("number,expected", [(3390, "3.7"), (3439, "3.10"), (3495, "3.11"),
                                              (3531, "3.12"), (3571, "3.13"), (62211, "2.7")])
def test_magic_table(number, expected):
    assert parse_pyc_magic(struct.pack("<H", number) + b"\r\n")["python"] == expected


def test_not_a_pyc_without_crlf():
    assert parse_pyc_magic(b"\x61\x0d\x00\x00") is None


def test_source_dir(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.js").write_text("//\n")
    report = identify(str(tmp_path))
    assert report["kind"] == "source_dir"
    assert report["file_counts"]["py"] == 1
    assert report["file_counts"]["other_code"] == 0


def test_pyc_only_dir(tmp_path):
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    pyc = compile_pyc(tmp_path)
    (cache / "a.cpython.pyc").write_bytes(pyc.read_bytes())
    pyc.unlink()
    (tmp_path / "mod.py").unlink()
    assert identify(str(tmp_path))["kind"] == "pyc_dir"


def test_single_source_file(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print(1)\n")
    assert identify(str(f))["kind"] == "source_file"


def test_wheel_zip(tmp_path):
    whl = make_zip(tmp_path / "p-1.0-py3-none-any.whl", {"p/__init__.py": "", "p-1.0.dist-info/WHEEL": "x"})
    report = identify(str(whl))
    assert (report["kind"], report["subtype"]) == ("python_package_archive", "wheel")


def test_zipapp(tmp_path):
    app = make_zip(tmp_path / "tool.pyz", {"__main__.py": "print(1)"})
    assert identify(str(app))["subtype"] == "zipapp"


def test_jar_and_apk_are_out_of_scope(tmp_path):
    jar = make_zip(tmp_path / "a.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0", "A.class": b"x"})
    apk = make_zip(tmp_path / "a.apk", {"AndroidManifest.xml": b"x", "classes.dex": b"x"})
    for path, kind in ((jar, "jar"), (apk, "apk")):
        report = identify(str(path))
        assert report["kind"] == kind
        assert report["supported_in_v0"] is False
        assert any("v1" in item for item in report["limitations"])


def test_java_class_vs_macho_fat(tmp_path):
    cls = tmp_path / "A.class"
    cls.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 16)
    fat = tmp_path / "fat"
    fat.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 16)
    assert identify(str(cls))["kind"] == "java_class"
    assert identify(str(fat))["kind"] == "native_binary"


def test_elf_is_native(tmp_path):
    elf = tmp_path / "prog"
    elf.write_bytes(b"\x7fELF" + b"\0" * 60)
    report = identify(str(elf))
    assert report["kind"] == "native_binary"
    assert report["supported_in_v0"] is False


def _pe(dotnet: bool) -> bytes:
    pe_off = 0x80
    data = bytearray(0x200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_off)
    data[pe_off:pe_off + 4] = b"PE\0\0"
    opt = pe_off + 0x18
    struct.pack_into("<H", data, opt, 0x20B)
    if dotnet:
        struct.pack_into("<II", data, opt + 112 + 14 * 8, 0x2000, 0x48)
    return bytes(data)


def test_pe_dotnet_vs_native(tmp_path):
    managed = tmp_path / "m.dll"
    managed.write_bytes(_pe(True))
    native = tmp_path / "n.exe"
    native.write_bytes(_pe(False))
    assert identify(str(managed))["kind"] == "dotnet_assembly"
    assert identify(str(native))["kind"] == "native_binary"


@pytest.mark.parametrize("layout,subtype", [("classic", "docker-classic"), ("oci", "oci")])
def test_docker_tar(tmp_path, layout, subtype):
    report = identify(str(make_docker_tar(tmp_path / "img.tar", layout)))
    assert report["kind"] == "docker_image_tar"
    assert report["subtype"] == subtype
    assert report["recommended_scripts"] == ["inspect_docker_image.py"]


def test_sdist_tar(tmp_path):
    path = tmp_path / "p-1.0.tar.gz"
    pkg = tmp_path / "p-1.0"
    pkg.mkdir()
    (pkg / "PKG-INFO").write_text("Name: p\n")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(pkg, arcname="p-1.0")
        tf.add(pkg / "PKG-INFO", arcname="p-1.0/PKG-INFO")
    assert identify(str(path))["subtype"] == "sdist"


def test_image_reference(tmp_path):
    report = identify("nginx:1.27")
    assert report["kind"] == "docker_image_ref"


def test_missing_path_is_unknown(tmp_path):
    assert identify(str(tmp_path / "nope.bin"))["kind"] == "unknown"


def test_cli_outputs_valid_json(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print(1)\n")
    proc = subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)["kind"] == "source_file"


def test_never_executes_content(tmp_path):
    marker = tmp_path / "pwned"
    evil = tmp_path / "evil.py"
    evil.write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    identify(str(evil))
    identify(str(tmp_path))
    assert not marker.exists()
