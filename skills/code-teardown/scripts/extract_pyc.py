#!/usr/bin/env python3
"""Step 2 of the pipeline for compiled Python: extract and decompile into a work dir.

Accepts a .pyc file, a directory of .pyc files, or a wheel/zipapp/egg/zip/sdist
that contains them. Static only: bytecode is read with marshal and dis, and is
never executed or imported. The marshal step runs in a child process because
marshal is not hardened against malformed input; a crash there is contained.

Degradation ladder (declared in the output, never hidden):
  1. external decompiler (pycdc, decompyle3, uncompyle6) if installed
  2. stdlib dis, only when the .pyc matches the running Python version
  3. header + raw strings only (foreign version, no decompiler)

Usage: extract_pyc.py PATH [--out WORKDIR]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"code-teardown needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import dis
import io
import json
import marshal
import re
import shutil
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (SECRET_NAME, available_tools, bounded_name, dump, empty_workdir,  # noqa: E402
                     extract_members, parse_pyc_magic, running_python)

MAX_PYC_FILES = 500
MAX_PYC_BYTES = 50_000_000
MAX_DIS_BYTES = 5_000_000
WORKER_TIMEOUT = 60
DECOMPILER_TIMEOUT = 60
MAX_DECOMPILED_CHARS = 5_000_000
CO_NEWLOCALS = 0x2
CO_ASYNC = 0x80 | 0x200
DECODERS = {"b64decode", "decompress", "a85decode", "unhexlify", "b85decode"}
URL_RE = re.compile(r"https?://[^\s'\"<>]{4,}")


# --- header ------------------------------------------------------------------

def header_info(data: bytes, magic: int) -> dict:
    """Parse the pyc header. Layout depends on the Python version."""
    if magic >= 62000:                      # Python 2.7: magic + mtime
        return {"length": 8, "mtime": int.from_bytes(data[4:8], "little")}
    if magic >= 3390:                       # 3.7+: PEP 552, 16 bytes
        flags = int.from_bytes(data[4:8], "little")
        info = {"length": 16, "flags": flags, "hash_based": bool(flags & 1)}
        if flags & 1:
            info["source_hash"] = data[8:16].hex()
        else:
            info["mtime"] = int.from_bytes(data[8:12], "little")
            info["source_size"] = int.from_bytes(data[12:16], "little")
        return info
    if magic >= 3190:                       # 3.3-3.6: 12 bytes
        return {"length": 12, "mtime": int.from_bytes(data[4:8], "little"),
                "source_size": int.from_bytes(data[8:12], "little")}
    return {"length": 8, "mtime": int.from_bytes(data[4:8], "little")}


def raw_strings(data: bytes, limit: int = 200) -> list[str]:
    seen, out = set(), []
    for match in re.finditer(rb"[\x20-\x7e]{6,}", data):
        text = match.group().decode("ascii")[:120]
        if text not in seen:
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                break
    return out


# --- bytecode analysis (runs in the worker process) ------------------------------

def _walk_code(code: types.CodeType):
    stack = [code]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(c for c in reversed(current.co_consts) if isinstance(c, types.CodeType))


def _kind(code: types.CodeType) -> str:
    if code.co_name == "<module>":
        return "module"
    if code.co_name.startswith("<") and code.co_name.endswith(">"):
        return "inline"
    return "function" if code.co_flags & CO_NEWLOCALS else "class"


def analyze_bytecode(data: bytes) -> dict:
    """Analyze marshalled code. Must only be called on a version-matched payload."""
    # marshal.loads builds code objects without running them (unlike pickle). Its only
    # documented risk is crashing on malformed input, which is why this function is
    # only ever called inside the isolated worker process with a timeout.
    root = marshal.loads(data)
    if not isinstance(root, types.CodeType):
        raise ValueError("payload is not a code object")
    text = io.StringIO()
    functions, imports, signals = [], [], []
    names: set[str] = set()
    strings: list[str] = []
    big_bytes = 0
    dis_len = 0
    secret_values: set[str] = set()
    for code in _walk_code(root):
        qual = getattr(code, "co_qualname", code.co_name)
        kind = _kind(code)
        starts = {off: line for off, line in dis.findlinestarts(code) if line is not None}
        instrs = list(dis.get_instructions(code))
        branches = sum(1 for i in instrs if "JUMP_IF" in i.opname or i.opname == "FOR_ITER")
        section = f"== {qual} [{kind}] (source line {code.co_firstlineno}) =="
        dis_start = text.getvalue().count("\n") + 1
        if dis_len < MAX_DIS_BYTES:
            body = dis.Bytecode(code).dis()
            text.write(section + "\n" + body + "\n")
            dis_len += len(body)
        functions.append({
            "qualname": qual, "kind": kind, "line": code.co_firstlineno,
            "filename": code.co_filename, "args": code.co_argcount + code.co_kwonlyargcount,
            "async": bool(code.co_flags & CO_ASYNC), "instructions": len(instrs),
            "branches": branches, "dis_line": dis_start,
        })
        line = code.co_firstlineno
        recent: list = []
        prev_const = None
        for ins in instrs:
            line = starts.get(ins.offset, line)
            if ins.opname in ("LOAD_CONST", "LOAD_SMALL_INT"):
                recent = (recent + [ins.argval])[-2:]
                prev_const = ins.argval
                if isinstance(ins.argval, str) and len(ins.argval) >= 4:
                    strings.append(ins.argval)
                if isinstance(ins.argval, (bytes, bytearray)):
                    big_bytes = max(big_bytes, len(ins.argval))
                continue
            if ins.opname == "IMPORT_NAME":
                level = recent[0] if len(recent) == 2 and isinstance(recent[0], int) else 0
                imports.append({"module": "." * level + str(ins.argval), "line": line, "in": qual})
            elif ins.opname in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_ATTR", "LOAD_METHOD"):
                name = str(ins.argval)
                names.add(name)
                if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME") and name in ("eval", "exec"):
                    signals.append({"kind": "eval_exec", "line": line, "in": qual, "detail": name})
                elif ins.opname in ("LOAD_GLOBAL", "LOAD_NAME") and name == "__import__":
                    signals.append({"kind": "dynamic_import", "line": line, "in": qual, "detail": name})
                elif ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and name in ("system", "popen"):
                    signals.append({"kind": "os_command", "line": line, "in": qual, "detail": name})
            elif ins.opname.startswith("STORE_") and isinstance(prev_const, str) and len(prev_const) >= 8:
                target = str(ins.argval)
                if SECRET_NAME.search(target):
                    secret_values.add(prev_const)
                    # The literal is never echoed: only the variable name and length.
                    signals.append({"kind": "hardcoded_secret_candidate", "line": line, "in": qual,
                                    "detail": f"{target} = <str literal, {len(prev_const)} chars>"})
            prev_const = None
            recent = []
        names.update(code.co_names)

    # Redact flagged literals everywhere the report or the work dir could echo them.
    strings = [x for x in strings if x not in secret_values]
    dis_out = text.getvalue()
    for value in secret_values:
        dis_out = dis_out.replace(repr(value), "'<redacted secret candidate>'")

    lowered = {n.lower() for n in names} | {s.lower() for s in strings}
    flags = []
    if any("pyarmor" in item or "pytransform" in item for item in lowered):
        flags.append({"kind": "pyarmor_markers", "strength": "strong",
                      "detail": "PyArmor/pytransform names or strings present"})
    if names & {"exec", "eval"} and names & DECODERS:
        flags.append({"kind": "decode_then_exec", "strength": "strong",
                      "detail": "exec/eval combined with a decoder/decompressor"})
    if names & {"exec", "eval"} and "marshal" in {n.lower() for n in names}:
        flags.append({"kind": "marshal_then_exec", "strength": "strong",
                      "detail": "exec/eval combined with marshal"})
    named = [f for f in functions if f["kind"] == "function"]
    if len(named) >= 5 and sum(len(f["qualname"].split(".")[-1]) <= 2 for f in named) / len(named) >= 0.6:
        flags.append({"kind": "minified_identifiers", "strength": "medium",
                      "detail": "most function names are 1-2 characters"})
    if big_bytes >= 2000:
        flags.append({"kind": "large_bytes_const", "strength": "info",
                      "detail": f"a bytes constant of {big_bytes} bytes"})
    if root.co_filename.startswith("<") and root.co_filename.endswith(">"):
        flags.append({"kind": "synthetic_filename", "strength": "info", "detail": root.co_filename})

    urls = []
    for item in strings:
        urls.extend(URL_RE.findall(item))
    return {
        "original_filename": root.co_filename,
        "functions": functions, "imports": imports, "signals": signals,
        "strings_sample": [s[:120] for s in dict.fromkeys(strings)][:60],
        "urls": list(dict.fromkeys(urls))[:20],
        "obfuscation": flags, "dis_text": dis_out,
    }


def run_worker(payload_path: Path) -> dict:
    """Analyze bytecode in a child process; return the result or an error record."""
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(payload_path)],
                              capture_output=True, text=True, timeout=WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": f"bytecode analysis timed out after {WORKER_TIMEOUT}s"}
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["unknown failure"])[-1][:200]
        return {"error": f"bytecode analysis failed (exit {proc.returncode}): {tail}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "bytecode analysis returned invalid output"}


# --- optional decompilers ----------------------------------------------------------

def decompiler_order(label: str) -> list[str]:
    old = label in ("3.6", "3.7", "3.8", "3.0-3.5", "2.7", "2.x or older")
    return ["decompyle3", "uncompyle6", "pycdc"] if old else ["pycdc"]


def try_decompile(pyc: Path, label: str) -> tuple[str, str] | tuple[None, str]:
    """Run an installed decompiler on the file. Returns (tool, source) or (None, reason)."""
    candidates = [t for t in decompiler_order(label) if shutil.which(t)]
    if not candidates:
        return None, "no decompiler on PATH (looked for " + ", ".join(decompiler_order(label)) + ")"
    reasons = []
    for tool in candidates:
        try:
            proc = subprocess.run([tool, str(pyc)], capture_output=True, text=True, timeout=DECOMPILER_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            reasons.append(f"{tool}: {type(exc).__name__}")
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return tool, proc.stdout
        reasons.append(f"{tool}: exit {proc.returncode}")
    return None, "; ".join(reasons)


# --- per-file pipeline -----------------------------------------------------------------

def process_pyc(pyc: Path, rel: str, workdir: Path) -> dict:
    record: dict = {"path": rel}
    try:
        data = pyc.read_bytes()[:MAX_PYC_BYTES]
    except OSError as exc:
        return {**record, "error": f"unreadable: {exc.strerror}", "method": "none", "quality_judgment_confidence": "none"}
    magic = parse_pyc_magic(data[:4])
    if magic is None:
        return {**record, "error": "not a .pyc file (bad header)", "method": "none", "quality_judgment_confidence": "none"}
    label = magic["python"]
    head = header_info(data, magic["magic"])
    record.update({"size": len(data), "magic_number": magic["magic"], "python_version": label, "header": head,
                   "same_as_running_python": label == running_python()})

    result: dict = {}
    notes: list[str] = []
    safe_rel = bounded_name(rel)
    safe_name = safe_rel.replace("/", "__")
    if label == running_python():
        payload = workdir / ".payload" / safe_name
        payload.parent.mkdir(exist_ok=True)
        payload.write_bytes(data[head["length"]:])
        result = run_worker(payload)
        payload.unlink(missing_ok=True)
        if "error" in result:
            notes.append(result.pop("error"))
    else:
        notes.append(f"Bytecode is Python {label}; stdlib dis on {running_python()} cannot read it.")

    dis_text = result.pop("dis_text", "") if result else ""
    if dis_text:
        dis_path = workdir / "dis" / (safe_rel + ".dis.txt")
        dis_path.parent.mkdir(parents=True, exist_ok=True)
        dis_path.write_text(dis_text, encoding="utf-8")
        record["dis_path"] = dis_path.relative_to(workdir).as_posix()
        for fn in result.get("functions", []):
            fn["dis_ref"] = f"{record['dis_path']}:{fn.pop('dis_line')}"

    tool, output = try_decompile(pyc.resolve(), label)
    if tool:
        out_path = workdir / "decompiled" / (safe_rel + ".py")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output[:MAX_DECOMPILED_CHARS], encoding="utf-8")
        record["decompiled_path"] = out_path.relative_to(workdir).as_posix()
        record["method"] = f"decompiler:{tool}"
        record["quality_judgment_confidence"] = "medium"
        notes.append("Decompiled source has lost comments, local naming intent and original formatting; "
                     "treat quality verdicts as medium confidence at best.")
    else:
        notes.append(f"Decompiler unavailable or failed: {output}")
        if dis_text:
            record["method"] = "dis"
            record["quality_judgment_confidence"] = "low"
            notes.append("Only the bytecode disassembly is available; judge structure from it, not style.")
        else:
            record["method"] = "header-only"
            record["quality_judgment_confidence"] = "none"
            record["raw_strings"] = raw_strings(data)
            notes.append("No structure could be recovered. Install pycdc (any version) or decompyle3 "
                         "(Python 3.7-3.8), or run with a matching Python, for a real analysis.")
    record["notes"] = notes
    record.update(result)
    return record


def collect(path: Path, workdir: Path) -> tuple[list[tuple[Path, str]], dict]:
    """Return (pyc files to analyze, extraction info) for any supported input."""
    info = {"source_files_extracted": 0, "native_extensions": [], "skipped": []}
    files: list[tuple[Path, str]] = []

    def is_pyc(name: str) -> bool:
        return name.lower().endswith((".pyc", ".pyo"))

    def is_py(name: str) -> bool:
        return name.lower().endswith(".py")

    def is_native(name: str) -> bool:
        return name.lower().endswith((".so", ".pyd", ".dylib", ".dll"))

    if path.is_file() and not zipfile.is_zipfile(path) and not _is_tar(path):
        return [(path, path.name)], info
    if path.is_dir():
        for found in sorted(path.rglob("*")):
            if found.is_symlink() or not found.is_file():
                continue
            if is_pyc(found.name):
                files.append((found, found.relative_to(path).as_posix()))
            elif is_native(found.name):
                info["native_extensions"].append(found.relative_to(path).as_posix())
        return files, info

    pyc_dir = workdir / "extracted"
    src_dir = workdir / "source"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            info["native_extensions"] = [n for n in archive.namelist() if is_native(n)]
            written, skipped = extract_members(archive, pyc_dir, is_pyc)
            src_written, src_skipped = extract_members(archive, src_dir, is_py)
    else:
        with tarfile.open(path, "r:*") as archive:
            written, skipped = extract_members(archive, pyc_dir, is_pyc)
        with tarfile.open(path, "r:*") as archive:
            src_written, src_skipped = extract_members(archive, src_dir, is_py)
    info["skipped"] = skipped + src_skipped
    info["source_files_extracted"] = len(src_written)
    files = [(pyc_dir / name, name) for name in written]
    return files, info


def _is_tar(path: Path) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except OSError:
        return False


def extract(path_arg: str, out: str | None = None) -> dict:
    path = Path(path_arg).expanduser()
    if not path.exists():
        raise SystemExit(f"error: {path} does not exist")
    workdir = empty_workdir(out)
    (workdir / ".payload").mkdir(exist_ok=True)
    files, info = collect(path, workdir)
    limits: list[str] = []
    if len(files) > MAX_PYC_FILES:
        limits.append(f"{len(files)} .pyc files found; only the first {MAX_PYC_FILES} were analyzed.")
        files = files[:MAX_PYC_FILES]
    records = [process_pyc(p, rel, workdir) for p, rel in files]
    shutil.rmtree(workdir / ".payload", ignore_errors=True)

    obfuscation = [dict(flag, path=r["path"]) for r in records for flag in r.get("obfuscation", [])]
    strong = [f for f in obfuscation if f["strength"] == "strong"]
    medium = [f for f in obfuscation if f["strength"] == "medium"]
    suspected = bool(strong) or len(medium) >= 2
    if suspected:
        limits.append("Obfuscation suspected: names and structure may be deliberately misleading. "
                      "Limit the analysis to what the evidence shows and do not judge design quality.")
    if info["native_extensions"]:
        limits.append(f"{len(info['native_extensions'])} native extension(s) present; they are not analyzed.")
    if info["skipped"]:
        limits.append(f"{len(info['skipped'])} archive member(s) skipped for safety (unsafe path or size cap).")
    if not records:
        limits.append("No .pyc files found in the input.")
    methods = sorted({r["method"] for r in records})
    versions = sorted({r.get("python_version", "?") for r in records})
    report = {
        "input": str(path), "workdir": str(workdir), "python_versions": versions,
        "running_python": running_python(), "tools_available": available_tools(),
        "methods_used": methods,
        "weakest_confidence": min((r["quality_judgment_confidence"] for r in records),
                                  key=["none", "low", "medium"].index, default="none"),
        "obfuscation_suspected": suspected, "source_files_extracted": info["source_files_extracted"],
        "native_extensions": info["native_extensions"][:50], "skipped": info["skipped"][:50],
        "limits": limits, "files": records,
    }
    (workdir / "extraction.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--worker":
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (WORKER_TIMEOUT + 5, WORKER_TIMEOUT + 5))
        except (ImportError, ValueError, OSError):    # not available everywhere; the parent still enforces a timeout
            pass
        json.dump(analyze_bytecode(Path(argv[2]).read_bytes()), sys.stdout)
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path")
    parser.add_argument("--out", help="work directory (must be empty or new); default: a new temp dir")
    args = parser.parse_args(argv[1:])
    dump(extract(args.path, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
