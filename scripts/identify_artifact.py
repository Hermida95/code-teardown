#!/usr/bin/env python3
"""Step 1 of the pipeline: identify what kind of artifact a path is.

Looks only at magic bytes, file names inside archives and directory layout.
Never extracts to disk, imports or executes anything from the artifact.

Usage: identify_artifact.py PATH
Prints a JSON report on stdout.
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"code-teardown needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import struct
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import available_tools, dump, parse_pyc_magic, running_python  # noqa: E402

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
             ".tox", ".mypy_cache", ".pytest_cache", "dist", "build"}
MAX_TAR_MEMBERS = 20000
MAX_TAR_DECLARED_BYTES = 20_000_000_000   # stop walking a tar that declares more data than this
MAX_DIRS = 100_000
MAX_WALK_FILES = 50000

PIPELINES = {
    "source_dir": ["inventory.py"],
    "source_file": ["inventory.py"],
    "pyc_file": ["extract_pyc.py"],
    "pyc_dir": ["extract_pyc.py", "inventory.py"],
    "python_package_archive": ["extract_pyc.py", "inventory.py"],
    "docker_image_tar": ["inspect_docker_image.py"],
    "docker_image_dir": ["inspect_docker_image.py"],
    "docker_image_ref": ["inspect_docker_image.py"],
}

V1_NOTE = "Out of scope for v0 (planned for v1): declare it and stop instead of faking an analysis."


def result(path: str, kind: str, supported: bool, evidence: list[str], **extra) -> dict:
    report = {
        "path": path,
        "kind": kind,
        "supported_in_v0": supported,
        "evidence": evidence,
        "recommended_scripts": PIPELINES.get(kind, []),
        "limitations": [],
        "tools_available": available_tools(),
    }
    report.update(extra)
    if not supported:
        report["limitations"].append(V1_NOTE)
    return report


# --- native / unsupported binaries -------------------------------------------

def _pe_is_dotnet(head: bytes) -> bool | None:
    """True if a PE has a CLR header, False if not, None if it can't be parsed."""
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None
    (pe_off,) = struct.unpack_from("<I", head, 0x3C)
    if pe_off + 0x18 + 0x70 > len(head) or head[pe_off:pe_off + 4] != b"PE\0\0":
        return None
    opt = pe_off + 0x18
    (magic,) = struct.unpack_from("<H", head, opt)
    base = {0x10B: 96, 0x20B: 112}.get(magic)
    if base is None:
        return None
    clr = opt + base + 14 * 8
    if clr + 8 > len(head):
        return None
    rva, size = struct.unpack_from("<II", head, clr)
    return rva != 0 and size != 0


def classify_binary(head: bytes) -> tuple[str, list[str]] | None:
    if head[:4] == b"\x7fELF":
        return "native_binary", ["ELF magic bytes"]
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return "native_binary", ["Mach-O magic bytes"]
    if head[:4] == b"\xca\xfe\xba\xbe" and len(head) >= 8:
        (value,) = struct.unpack(">I", head[4:8])
        # Java class files carry a major version >= 45 here; a fat Mach-O
        # carries a small architecture count.
        if value >= 45:
            return "java_class", ["Java class magic bytes (CAFEBABE + version)"]
        return "native_binary", ["Mach-O universal (fat) magic bytes"]
    if head[:2] == b"MZ":
        dotnet = _pe_is_dotnet(head)
        if dotnet:
            return "dotnet_assembly", ["PE header with CLR runtime directory (.NET)"]
        return "native_binary", ["PE/MZ magic bytes"]
    if head[:4] == b"\0asm":
        return "native_binary", ["WebAssembly magic bytes"]
    return None


# --- archives ----------------------------------------------------------------

def classify_zip(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return result(str(path), "unknown", False, ["Looks like a zip but cannot be read"])
    lower = [n.lower() for n in names]
    if "androidmanifest.xml" in lower and any(n.endswith(".dex") for n in lower):
        return result(str(path), "apk", False, ["AndroidManifest.xml and classes.dex inside the archive"])
    if any(n.endswith(".class") for n in lower) or "meta-inf/manifest.mf" in lower:
        return result(str(path), "jar", False, ["META-INF/MANIFEST.MF or .class entries inside the archive"])
    if any(n.endswith(".dist-info/wheel") for n in lower):
        return result(str(path), "python_package_archive", True,
                      ["*.dist-info/WHEEL inside the archive"], subtype="wheel",
                      contains_pyc=any(n.endswith(".pyc") for n in lower))
    if "__main__.py" in lower or "__main__.pyc" in lower:
        return result(str(path), "python_package_archive", True,
                      ["__main__ at the archive root (zipapp)"], subtype="zipapp",
                      contains_pyc=any(n.endswith(".pyc") for n in lower))
    if any(n.endswith(".egg-info/pkg-info") or n == "egg-info/pkg-info" for n in lower):
        return result(str(path), "python_package_archive", True,
                      ["EGG-INFO/PKG-INFO inside the archive"], subtype="egg",
                      contains_pyc=any(n.endswith(".pyc") for n in lower))
    if any(n.endswith((".py", ".pyc")) for n in lower):
        return result(str(path), "python_package_archive", True,
                      ["Python files inside a generic zip"], subtype="zip",
                      contains_pyc=any(n.endswith(".pyc") for n in lower))
    return result(str(path), "unknown", False, ["Generic zip without recognizable Python, JVM or Android content"])


def classify_tar(path: Path) -> dict | None:
    """Docker/OCI image tars and Python sdists. None if it is not a tar."""
    try:
        names: list[str] = []
        truncated = False
        with tarfile.open(path, "r:*") as tf:
            declared = 0
            for member in tf:
                names.append(member.name)
                declared += member.size if member.isreg() else 0
                if len(names) >= MAX_TAR_MEMBERS or declared > MAX_TAR_DECLARED_BYTES:
                    truncated = True
                    break
    except (tarfile.TarError, OSError, EOFError):
        return None
    top = {n.lstrip("./") for n in names}
    notes = ["Archive listing truncated (too many entries or too much declared data); classification may be incomplete."] if truncated else []
    if "manifest.json" in top:
        report = result(str(path), "docker_image_tar", True,
                        ["manifest.json at the tar root (docker save, classic layout)"],
                        subtype="docker-classic")
    elif "oci-layout" in top or "index.json" in top:
        report = result(str(path), "docker_image_tar", True,
                        ["oci-layout / index.json at the tar root (OCI image layout)"],
                        subtype="oci")
    elif any(n.endswith("/PKG-INFO") and n.count("/") == 1 for n in top):
        report = result(str(path), "python_package_archive", True,
                        ["PKG-INFO one level down (Python sdist)"], subtype="sdist")
    else:
        report = result(str(path), "unknown", False, ["Tar archive without Docker, OCI or sdist markers"])
    report["limitations"].extend(notes)
    return report


# --- directories -------------------------------------------------------------

def classify_dir(path: Path) -> dict:
    if (path / "oci-layout").is_file() or (path / "manifest.json").is_file() and (path / "repositories").exists():
        return result(str(path), "docker_image_dir", True,
                      ["Extracted docker save / OCI layout in a directory"])
    counts = {"py": 0, "pyc": 0, "other_code": 0}
    other_exts = {".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".rb", ".php", ".cs", ".kt", ".swift"}
    seen = 0
    dirs = 0
    stack = [path]
    while stack and seen < MAX_WALK_FILES and dirs < MAX_DIRS:
        current = stack.pop()
        dirs += 1
        try:
            entries = list(current.iterdir())
        except OSError:                       # unreadable directory: skip it, keep going
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                # __pycache__ is where .pyc files live, so it is not skipped here.
                if entry.name not in SKIP_DIRS or entry.name == "__pycache__":
                    stack.append(entry)
                continue
            seen += 1
            suffix = entry.suffix.lower()
            if suffix == ".py":
                counts["py"] += 1
            elif suffix in (".pyc", ".pyo"):
                counts["pyc"] += 1
            elif suffix in other_exts:
                counts["other_code"] += 1
    evidence = [f"{counts['py']} .py, {counts['pyc']} .pyc, {counts['other_code']} other source files"]
    extra = {"file_counts": counts}
    if counts["pyc"] and not counts["py"]:
        return result(str(path), "pyc_dir", True, evidence, **extra)
    if counts["py"] == 0 and counts["other_code"] > 0:
        report = result(str(path), "source_dir", True, evidence, **extra)
        report["limitations"].append(
            "No Python sources found; inventory still works but the import graph and "
            "function metrics are Python-only in v0.")
        return report
    report = result(str(path), "source_dir", True, evidence, **extra)
    if counts["pyc"]:
        report["limitations"].append(
            "Directory mixes .py sources and .pyc files; analyze compiled files separately.")
    if seen >= MAX_WALK_FILES:
        report["limitations"].append("File walk stopped at the safety cap; counts are partial.")
    return report


# --- file dispatch -----------------------------------------------------------

def classify_file(path: Path) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(4096)
    suffix = path.suffix.lower()

    binary = classify_binary(head)
    if binary:
        kind, evidence = binary
        supported = False
        report = result(str(path), kind, supported, evidence)
        if kind == "native_binary":
            report["limitations"].append(
                "Native binaries need a disassembler; code-teardown does not analyze them.")
        return report

    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return classify_zip(path)

    magic = parse_pyc_magic(head)
    if magic and suffix in (".pyc", ".pyo", ""):
        py = magic["python"]
        report = result(str(path), "pyc_file", True,
                        [f"Header magic number {magic['magic']} (Python {py})"],
                        python_version=py, magic_number=magic["magic"],
                        same_as_running_python=(py == running_python()))
        if py != running_python():
            report["limitations"].append(
                f"Bytecode is Python {py} but this interpreter is {running_python()}: "
                "stdlib dis cannot read it. Only header metadata and strings are available "
                "unless pycdc or decompyle3 is installed.")
        return report

    tar_report = classify_tar(path) if suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz", "") or head[257:262] == b"ustar" else None
    if tar_report:
        return tar_report

    if suffix == ".py":
        return result(str(path), "source_file", True, ["Python source file (.py extension)"])

    return result(str(path), "unknown", False, ["No known magic bytes or extension"])


def identify(path_str: str) -> dict:
    path = Path(path_str).expanduser()
    if path.is_dir():
        return classify_dir(path)
    if path.is_file():
        return classify_file(path)
    if path_str and not any(sep in path_str for sep in ("/", "\\")) and not path.suffix.lower() in (".pyc", ".py", ".tar", ".zip"):
        return result(path_str, "docker_image_ref", True,
                      ["Path does not exist and looks like a Docker image reference (name[:tag])"],
                      limitations_hint="Needs the docker CLI to run 'docker save'; nothing is run.")
    return result(path_str, "unknown", False, ["Path does not exist"])


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    dump(identify(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
