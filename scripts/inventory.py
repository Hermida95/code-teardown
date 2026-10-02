#!/usr/bin/env python3
"""Step 3 of the pipeline: static inventory of a source tree.

Reports languages, size, declared dependencies, entry points, the internal
import graph and per-function metrics. Every finding carries a path and a
line number so the report can cite evidence.

Python files are only parsed with ast: nothing from the analyzed tree is
imported or executed, and setup.py is read as data.

Usage: inventory.py PATH [--out FILE] [--full] [--max-files N]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import statistics
import sys
import tomllib
import warnings
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SECRET_NAME, dump  # noqa: E402

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
             ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
             ".idea", ".vscode", "site-packages", ".eggs"}
CODE_LANGS = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cc": "C++", ".hpp": "C++", ".cs": "C#", ".swift": "Swift", ".sh": "Shell", ".sql": "SQL",
}
CONFIG_LANGS = {
    ".md": "Markdown", ".rst": "reStructuredText", ".html": "HTML", ".css": "CSS",
    ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".toml": "TOML", ".ini": "INI", ".cfg": "INI",
}
MAX_FILE_BYTES = 2_000_000
IMPORT_TO_DIST = {"yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
                  "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
                  "jwt": "pyjwt", "attr": "attrs", "serial": "pyserial", "Crypto": "pycryptodome"}
SQL_RE = re.compile(r"\b(SELECT\s.+?\sFROM|INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.I | re.S)
TODO_RE = re.compile(r"#.*\b(TODO|FIXME|HACK|XXX)\b")


def norm_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def find_line(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return None


# --- Python analysis ---------------------------------------------------------

class FileAnalyzer(ast.NodeVisitor):
    """Collects functions, classes, imports and risk signals from one module."""

    def __init__(self, rel: str, lines: list[str], is_test: bool):
        self.rel, self.lines, self.is_test = rel, lines, is_test
        self.functions: list[dict] = []
        self.classes: list[dict] = []
        self.imports: list[dict] = []
        self.signals: list[dict] = []
        self.main_guard: int | None = None
        self._func_depth = 0
        self._class_stack: list[str] = []

    def signal(self, kind: str, node: ast.AST, snippet: str | None = None) -> None:
        line = getattr(node, "lineno", 0)
        if any(s["kind"] == kind and s["line"] == line for s in self.signals):
            return
        text = snippet if snippet is not None else self.lines[line - 1].strip()[:120] if 0 < line <= len(self.lines) else ""
        self.signals.append({"kind": kind, "path": self.rel, "line": line, "snippet": text})

    # imports
    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({"module": alias.name, "names": [], "level": 0,
                                 "line": node.lineno, "lazy": self._func_depth > 0})

    def visit_ImportFrom(self, node):
        names = [a.name for a in node.names]
        if "*" in names:
            self.signal("wildcard_import", node)
        self.imports.append({"module": node.module or "", "names": names, "level": node.level,
                             "line": node.lineno, "lazy": self._func_depth > 0})

    # structure
    def visit_ClassDef(self, node):
        methods = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in node.body)
        self.classes.append({"path": self.rel, "name": node.name, "line": node.lineno,
                             "end_line": node.end_lineno, "methods": methods,
                             "bases": [ast.unparse(b)[:60] for b in node.bases]})
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node):
        counter = _Complexity()
        for child in node.body:
            counter.visit(child)
        args = node.args
        all_args = args.posonlyargs + args.args + args.kwonlyargs
        annotated = node.returns is not None or any(a.annotation for a in all_args)
        qual = ".".join(self._class_stack + [node.name])
        self.functions.append({
            "path": self.rel, "qualname": qual, "line": node.lineno, "end_line": node.end_lineno,
            "length": (node.end_lineno or node.lineno) - node.lineno + 1,
            "cyclomatic": counter.score, "max_depth": counter.max_depth,
            "args": len(all_args) + bool(args.vararg) + bool(args.kwarg),
            "annotated": annotated, "has_docstring": ast.get_docstring(node) is not None,
            "is_test": self.is_test or node.name.startswith("test_"),
        })
        for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                    isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                    and default.func.id in ("list", "dict", "set")):
                self.signal("mutable_default", default)
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node):
        test = node.test
        if (not self._func_depth and not self._class_stack and isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name) and test.left.id == "__name__"
                and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)):
            self.main_guard = node.lineno
        self.generic_visit(node)

    # risk signals
    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.signal("bare_except", node)
        elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            self.signal("broad_except", node)
        if len(node.body) == 1 and isinstance(node.body[0], (ast.Pass, ast.Continue)):
            self.signal("swallowed_exception", node)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.signal("global_statement", node)

    def visit_Assign(self, node):
        self._secret_check(node.targets, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._secret_check([node.target], node.value, node)
        self.generic_visit(node)

    def _secret_check(self, targets, value, node):
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 8):
            return
        for target in targets:
            name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
            if name and SECRET_NAME.search(name):
                # Never echo the literal: the report must not leak the secret.
                self.signal("hardcoded_secret_candidate", node, f"{name} = <str literal, {len(value.value)} chars>")

    def visit_JoinedStr(self, node):
        literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))
        if any(isinstance(p, ast.FormattedValue) for p in node.values) and SQL_RE.search(literal):
            self.signal("sql_string_building", node)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        left = node.left
        if isinstance(node.op, (ast.Mod, ast.Add)) and isinstance(left, ast.Constant) \
                and isinstance(left.value, str) and SQL_RE.search(left.value):
            self.signal("sql_string_building", node)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format" and isinstance(func.value, ast.Constant) \
                and isinstance(func.value.value, str) and SQL_RE.search(func.value.value):
            self.signal("sql_string_building", node)
        name = func.id if isinstance(func, ast.Name) else None
        dotted = ast.unparse(func) if isinstance(func, (ast.Attribute, ast.Name)) else ""
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        if name in ("eval", "exec"):
            self.signal("eval_exec", node)
        elif dotted == "os.system":
            self.signal("os_system", node)
        # These are string patterns to *detect* in analyzed code; nothing is deserialized here.
        elif dotted in ("pickle.load", "pickle.loads", "marshal.loads", "marshal.load", "shelve.open"):
            self.signal("unsafe_deserialization", node)
        elif dotted in ("yaml.load", "yaml.load_all") and "Loader" not in kwargs:
            self.signal("unsafe_deserialization", node)
        elif dotted in ("hashlib.md5", "hashlib.sha1"):
            self.signal("weak_hash", node)
        if dotted.startswith("subprocess.") and isinstance(kwargs.get("shell"), ast.Constant) and kwargs["shell"].value is True:
            self.signal("shell_true", node)
        if isinstance(kwargs.get("verify"), ast.Constant) and kwargs["verify"].value is False:
            self.signal("tls_verify_disabled", node)
        if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany") and node.args:
            first = node.args[0]
            if (isinstance(first, ast.JoinedStr)
                    or (isinstance(first, ast.BinOp) and isinstance(first.op, (ast.Mod, ast.Add)))
                    or (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute) and first.func.attr == "format")):
                self.signal("sql_string_building", node)
        self.generic_visit(node)


class _Complexity(ast.NodeVisitor):
    """Cyclomatic-complexity approximation for one function body.

    Nested function and class bodies are skipped: they are reported on their own.
    """

    def __init__(self):
        self.score = 1
        self.depth = 0
        self.max_depth = 0

    def _nest(self, node, extra=0):
        self.score += 1 + extra
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.generic_visit(node)
        self.depth -= 1

    visit_If = visit_For = visit_AsyncFor = visit_While = lambda self, n: self._nest(n)
    visit_Try = lambda self, n: self._nest(n, extra=len(n.handlers) - 1 if n.handlers else -1)
    visit_With = visit_AsyncWith = lambda self, n: self._nest(n, extra=-1)

    def visit_BoolOp(self, node):
        self.score += len(node.values) - 1
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self.score += 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.score += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_match_case(self, node):
        self.score += 1
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = visit_FunctionDef


def module_name(rel: Path, src_prefix: bool) -> tuple[str | None, bool]:
    parts = list(rel.with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts.pop()
    if src_prefix and parts and parts[0] == "src":
        parts = parts[1:]
    return (".".join(parts) or None), is_pkg


def tarjan(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan SCC; returns components with more than one node."""
    index, low, on_stack, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in graph:
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    out.append(sorted(component))
    return sorted(out)


# --- manifests ---------------------------------------------------------------

def parse_requirements(rel: str, text: str) -> list[dict]:
    deps = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)(\[[^\]]*\])?\s*(.*)$", line)
        if match:
            spec = match.group(3).strip()
            deps.append({"name": match.group(1), "spec": spec, "pinned": "==" in spec,
                         "path": rel, "line": number, "group": "requirements"})
    return deps


def _spec_name(requirement: str) -> tuple[str, str]:
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)(\[[^\]]*\])?\s*([^;]*)", requirement)
    return (match.group(1), match.group(3).strip()) if match else (requirement, "")


def parse_pyproject(rel: str, text: str) -> tuple[list[dict], list[dict], list[str]]:
    notes: list[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [], [f"{rel}: invalid TOML ({exc})"]
    deps, entries = [], []

    def add(requirement: str, group: str):
        name, spec = _spec_name(requirement)
        deps.append({"name": name, "spec": spec, "pinned": "==" in spec, "path": rel,
                     "line": find_line(text, name), "group": group})

    project = data.get("project", {})
    for requirement in project.get("dependencies", []):
        add(requirement, "dependencies")
    for group, items in project.get("optional-dependencies", {}).items():
        for requirement in items:
            add(requirement, f"optional:{group}")
    for name, target in project.get("scripts", {}).items():
        entries.append({"kind": "console_script", "name": name, "target": target,
                        "path": rel, "line": find_line(text, name)})
    poetry = data.get("tool", {}).get("poetry", {})
    for name, spec in poetry.get("dependencies", {}).items():
        if name.lower() != "python":
            version = spec if isinstance(spec, str) else str(spec.get("version", "")) if isinstance(spec, dict) else ""
            deps.append({"name": name, "spec": version, "pinned": version.startswith("=="),
                         "path": rel, "line": find_line(text, name), "group": "poetry"})
    for name, target in poetry.get("scripts", {}).items():
        entries.append({"kind": "console_script", "name": name, "target": str(target),
                        "path": rel, "line": find_line(text, name)})
    return deps, entries, notes


def parse_setup_py(rel: str, text: str) -> tuple[list[dict], list[dict], list[str]]:
    """Read install_requires / entry_points literals from setup.py without running it."""
    deps, entries, notes = [], [], []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return deps, entries, [f"{rel}: could not be parsed"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) == "setup" or getattr(node.func, "attr", None) == "setup"):
            for kw in node.keywords:
                if kw.arg == "install_requires":
                    try:
                        for requirement in ast.literal_eval(kw.value):
                            name, spec = _spec_name(requirement)
                            deps.append({"name": name, "spec": spec, "pinned": "==" in spec, "path": rel,
                                         "line": kw.value.lineno, "group": "install_requires"})
                    except (ValueError, TypeError):
                        notes.append(f"{rel}:{kw.value.lineno}: install_requires is computed dynamically, not resolved")
                elif kw.arg == "entry_points":
                    try:
                        value = ast.literal_eval(kw.value)
                        for item in (value.get("console_scripts", []) if isinstance(value, dict) else []):
                            entries.append({"kind": "console_script", "name": item.split("=")[0].strip(),
                                            "target": item.split("=", 1)[-1].strip(), "path": rel, "line": kw.value.lineno})
                    except (ValueError, TypeError, AttributeError):
                        notes.append(f"{rel}:{kw.value.lineno}: entry_points is computed dynamically, not resolved")
    return deps, entries, notes


def parse_package_json(rel: str, text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps = []
    for group in ("dependencies", "devDependencies"):
        for name, spec in (data.get(group) or {}).items():
            deps.append({"name": name, "spec": str(spec), "pinned": bool(re.match(r"^\d", str(spec))),
                         "path": rel, "line": find_line(text, f'"{name}"'), "group": group})
    return deps


def parse_dockerfile(rel: str, text: str) -> dict:
    info = {"path": rel, "from": [], "user": None, "entrypoint": None, "cmd": None,
            "healthcheck": False, "expose": []}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        word = line.split(None, 1)[0].upper() if line and not line.startswith("#") else ""
        rest = line.split(None, 1)[1] if " " in line else ""
        if word == "FROM":
            image = rest.split()[0] if rest else ""
            tagless = ":" not in image.split("/")[-1] and "@" not in image and image.lower() != "scratch"
            info["from"].append({"image": image, "line": number,
                                 "unpinned": tagless or image.endswith(":latest")})
        elif word == "USER":
            info["user"] = {"value": rest, "line": number}
        elif word == "ENTRYPOINT":
            info["entrypoint"] = {"value": rest[:200], "line": number}
        elif word == "CMD":
            info["cmd"] = {"value": rest[:200], "line": number}
        elif word == "HEALTHCHECK":
            info["healthcheck"] = True
        elif word == "EXPOSE":
            info["expose"].append({"value": rest, "line": number})
    return info


# --- main walk ---------------------------------------------------------------

def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8000]:
        return None
    return raw.decode("utf-8", errors="replace")


def inventory(root_arg: str, max_files: int = 20000, full: bool = False) -> dict:
    root = Path(root_arg).expanduser().resolve()
    single_file = root.is_file()
    base = root.parent if single_file else root
    files: list[Path] = [root] if single_file else []
    truncated = False
    skipped_dirs: set[str] = set()
    if not single_file:
        for dirpath, dirnames, filenames in os.walk(base):
            kept = []
            for d in sorted(dirnames):
                if d in SKIP_DIRS or (d.startswith(".") and d != ".github"):
                    skipped_dirs.add(d)
                elif not (Path(dirpath) / d).is_symlink():
                    kept.append(d)
            dirnames[:] = kept
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                files.append(path)
                if len(files) >= max_files:
                    truncated = True
                    break
            if truncated:
                break

    languages: dict[str, dict] = {}
    limits: list[str] = []
    deps: list[dict] = []
    entry_points: list[dict] = []
    dep_notes: list[str] = []
    dockerfiles: list[dict] = []
    parse_errors: list[dict] = []
    skipped_files: list[dict] = []
    ci_files, lint_files, lock_files, license_files, readme_files = [], [], [], [], []
    py_files: list[tuple[str, str]] = []
    total_bytes = 0
    largest: list[tuple[int, str]] = []

    for path in files:
        rel = path.relative_to(base).as_posix() if not single_file else path.name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total_bytes += size
        largest.append((size, rel))
        name, suffix = path.name, path.suffix.lower()
        lowered = name.lower()

        if rel.startswith(".github/workflows/") or lowered in (".gitlab-ci.yml", "jenkinsfile", "tox.ini", "noxfile.py", ".travis.yml"):
            ci_files.append(rel)
        if lowered in (".flake8", ".pre-commit-config.yaml", "ruff.toml", ".ruff.toml", "mypy.ini", ".pylintrc", ".eslintrc", ".eslintrc.json"):
            lint_files.append(rel)
        if lowered in ("setup.cfg", "tox.ini"):
            body = read_text(path) or ""
            if re.search(r"^\[(mypy|flake8|pylint|isort)|\b(mypy|black|flake8|ruff|pylint)\b", body, re.M):
                lint_files.append(rel)
        if lowered in ("poetry.lock", "pipfile.lock", "package-lock.json", "yarn.lock", "uv.lock", "pnpm-lock.yaml", "go.sum", "cargo.lock"):
            lock_files.append(rel)
        if lowered.startswith(("license", "copying")):
            license_files.append(rel)
        if lowered.startswith("readme"):
            readme_files.append(rel)

        language = "Dockerfile" if lowered == "dockerfile" or lowered.startswith("dockerfile.") else \
            "Makefile" if lowered == "makefile" else CODE_LANGS.get(suffix) or CONFIG_LANGS.get(suffix)
        if language is None:
            continue
        text = read_text(path)
        if text is None:
            skipped_files.append({"path": rel, "reason": "binary or larger than 2 MB"})
            continue
        loc = sum(1 for line in text.splitlines() if line.strip())
        kind = "code" if suffix in CODE_LANGS else "config_docs"
        entry = languages.setdefault(language, {"kind": kind, "files": 0, "loc": 0})
        entry["files"] += 1
        entry["loc"] += loc

        if language == "Dockerfile":
            dockerfiles.append(parse_dockerfile(rel, text))
        if re.fullmatch(r"requirements.*\.txt", lowered):
            deps.extend(parse_requirements(rel, text))
        elif lowered == "pyproject.toml":
            d, e, n = parse_pyproject(rel, text)
            deps.extend(d); entry_points.extend(e); dep_notes.extend(n)
            if re.search(r"^\[tool\.(ruff|mypy|black|flake8|pylint)", text, re.M):
                lint_files.append(rel)
        elif lowered == "setup.py":
            d, e, n = parse_setup_py(rel, text)
            deps.extend(d); entry_points.extend(e); dep_notes.extend(n)
        elif lowered == "package.json":
            deps.extend(parse_package_json(rel, text))
        if suffix == ".py":
            py_files.append((rel, text))

    python = analyze_python(base, py_files, parse_errors, entry_points)
    for item in dockerfiles:
        for key in ("entrypoint", "cmd"):
            if item[key]:
                entry_points.append({"kind": f"dockerfile_{key}", "name": key.upper(), "target": item[key]["value"],
                                     "path": item["path"], "line": item[key]["line"]})

    python["possibly_undeclared_deps"] = undeclared(python["external_imports"], deps)
    if truncated:
        limits.append(f"File walk stopped at {max_files} files; everything below is partial.")
    if skipped_files:
        limits.append(f"{len(skipped_files)} file(s) skipped (binary or larger than 2 MB).")
    if parse_errors:
        limits.append(f"{len(parse_errors)} Python file(s) failed to parse (Python 2 code or invalid syntax); they are missing from metrics.")
    if single_file:
        limits.append("Single-file input: dependency, entry-point and project-level signals are limited.")

    tests = [m for m in python["modules"] if m["is_test"]]
    top_dirs: dict[str, int] = defaultdict(int)
    for path in files:
        if not single_file:
            parts = path.relative_to(base).parts
            top_dirs[parts[0] + "/" if len(parts) > 1 else "(root)"] += 1
    largest.sort(reverse=True)

    if not full:
        python["functions_total"] = len(python["functions"])
        python["functions"] = []
    return {
        "root": str(root),
        "total_files": len(files),
        "total_bytes": total_bytes,
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1]["loc"])),
        "top_level_layout": dict(sorted(top_dirs.items())),
        "largest_files": [{"path": p, "bytes": s} for s, p in largest[:5]],
        "dependencies": {"declared": deps, "notes": dep_notes,
                         "manifest_found": bool(deps) or any(p.endswith(("pyproject.toml", "setup.py", "package.json")) for _, p in largest)},
        "entry_points": entry_points + [{"kind": "main_guard", "name": m["module"], "path": m["path"], "line": m["main_guard"]}
                                         for m in python["modules"] if m["main_guard"]],
        "dockerfiles": dockerfiles,
        "project_signals": {
            "ci_files": sorted(set(ci_files)), "lint_or_type_config": sorted(set(lint_files)),
            "lockfiles": lock_files, "license_files": license_files, "readme_files": readme_files,
            "test_modules": len(tests), "test_functions": sum(m["test_functions"] for m in tests),
            "python_modules": len(python["modules"]),
        },
        "python": python,
        "parse_errors": parse_errors,
        "skipped_dirs": sorted(skipped_dirs),
        "limits": limits,
    }


def analyze_python(base: Path, py_files: list[tuple[str, str]], parse_errors: list[dict],
                   entry_points: list[dict]) -> dict:
    src_prefix = (base / "src").is_dir() and not (base / "src" / "__init__.py").exists()
    modules: dict[str, dict] = {}
    functions: list[dict] = []
    classes: list[dict] = []
    signals: list[dict] = []
    raw_imports: dict[str, tuple[list[dict], bool]] = {}
    total_funcs = annotated = documented = 0

    for rel, text in py_files:
        lines = text.splitlines()
        is_test = bool(re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$", rel))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(text, filename=rel)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            parse_errors.append({"path": rel, "line": getattr(exc, "lineno", None), "error": type(exc).__name__})
            continue
        analyzer = FileAnalyzer(rel, lines, is_test)
        analyzer.visit(tree)
        for number, line in enumerate(lines, 1):
            if TODO_RE.search(line):
                analyzer.signals.append({"kind": "todo_comment", "path": rel, "line": number, "snippet": line.strip()[:120]})
        name, is_pkg = module_name(Path(rel), src_prefix)
        key = name or rel
        modules[key] = {
            "module": key, "path": rel, "loc": sum(1 for l in lines if l.strip()),
            "functions": len(analyzer.functions), "classes": len(analyzer.classes),
            "main_guard": analyzer.main_guard, "is_test": is_test, "is_package_init": is_pkg,
            "docstring": ast.get_docstring(tree) is not None,
            "test_functions": sum(1 for f in analyzer.functions if f["qualname"].split(".")[-1].startswith("test_")),
        }
        raw_imports[key] = (analyzer.imports, is_pkg)
        functions.extend(analyzer.functions)
        classes.extend(analyzer.classes)
        signals.extend(analyzer.signals)
        for f in analyzer.functions:
            if not f["is_test"]:
                total_funcs += 1
                annotated += f["annotated"]
                documented += f["has_docstring"]

    internal_tops = {m.split(".")[0] for m in modules}
    stdlib = sys.stdlib_module_names
    edges: list[dict] = []
    external: dict[str, list[dict]] = defaultdict(list)
    stdlib_used: set[str] = set()

    def resolve(target: str) -> str | None:
        parts = target.split(".")
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in modules:
                return candidate
        return None

    for mod, (imports, is_pkg) in raw_imports.items():
        package = mod if is_pkg else mod.rpartition(".")[0]
        for imp in imports:
            if imp["level"]:
                parts = package.split(".") if package else []
                parts = parts[:len(parts) - (imp["level"] - 1)] if imp["level"] > 1 else parts
                base_name = ".".join(parts + ([imp["module"]] if imp["module"] else []))
                targets = [f"{base_name}.{n}" for n in imp["names"] if f"{base_name}.{n}" in modules] or [base_name]
            else:
                targets = [f"{imp['module']}.{n}" for n in imp["names"] if f"{imp['module']}.{n}" in modules] or [imp["module"]]
            for target in targets:
                top = target.split(".")[0]
                if imp["level"] or top in internal_tops:
                    resolved = resolve(target)
                    if resolved and resolved != mod:
                        edges.append({"from": mod, "to": resolved, "line": imp["line"], "lazy": imp["lazy"]})
                elif top in stdlib:
                    stdlib_used.add(top)
                elif top:
                    external[top].append({"module": mod, "path": modules[mod]["path"], "line": imp["line"]})

    seen, unique_edges = set(), []
    for edge in edges:
        key = (edge["from"], edge["to"])
        if key not in seen:
            seen.add(key)
            unique_edges.append(edge)
    graph_all: dict[str, set[str]] = {m: set() for m in modules}
    graph_eager: dict[str, set[str]] = {m: set() for m in modules}
    fan_in: dict[str, int] = defaultdict(int)
    fan_out: dict[str, int] = defaultdict(int)
    for edge in unique_edges:
        graph_all[edge["from"]].add(edge["to"])
        if not edge["lazy"]:
            graph_eager[edge["from"]].add(edge["to"])
        fan_out[edge["from"]] += 1
        fan_in[edge["to"]] += 1

    app_funcs = [f for f in functions if not f["is_test"]]
    lengths = [f["length"] for f in app_funcs]
    cyclo = [f["cyclomatic"] for f in app_funcs]

    def top(items, key, n=15):
        return sorted(items, key=lambda x: (-x[key], x["path"], x["line"]))[:n]

    def rank(counter):
        return [{"module": m, "count": c, "path": modules[m]["path"]}
                for m, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]

    return {
        "modules": sorted(modules.values(), key=lambda m: m["path"]),
        "import_graph": {"edges": unique_edges[:3000], "edge_count": len(unique_edges),
                         "most_depended_on": rank(fan_in), "most_dependencies": rank(fan_out),
                         "cycles_all": tarjan(graph_all), "cycles_eager": tarjan(graph_eager)},
        "external_imports": {k: v[:3] for k, v in sorted(external.items())},
        "stdlib_imports": sorted(stdlib_used),
        "functions": functions,
        "stats": {
            "functions": len(app_funcs),
            "mean_length": round(statistics.mean(lengths), 1) if lengths else 0,
            "max_length": max(lengths, default=0),
            "mean_cyclomatic": round(statistics.mean(cyclo), 1) if cyclo else 0,
            "max_cyclomatic": max(cyclo, default=0),
            "annotated_ratio": round(annotated / total_funcs, 2) if total_funcs else None,
            "docstring_ratio": round(documented / total_funcs, 2) if total_funcs else None,
        },
        "longest_functions": top(app_funcs, "length"),
        "most_complex_functions": top(app_funcs, "cyclomatic"),
        "largest_classes": top([c for c in classes if not c["path"].startswith(("tests/", "test/"))], "methods", 10),
        "signals": sorted(signals, key=lambda s: (s["kind"], s["path"], s["line"])),
    }


def undeclared(external: dict[str, list[dict]], deps: list[dict]) -> dict:
    if not deps:
        return {"checked": False, "reason": "No dependency manifest found, so nothing to compare against.", "items": []}
    declared = {norm_dist(d["name"]) for d in deps}
    items = []
    for top, sites in external.items():
        if norm_dist(top) in declared or norm_dist(IMPORT_TO_DIST.get(top, top)) in declared:
            continue
        items.append({"import": top, "first_use": sites[0]})
    return {"checked": True, "items": items,
            "caveat": "Heuristic: an import name can differ from its distribution name, and optional or "
                      "dev dependencies may be declared elsewhere. Treat entries as leads, not facts."}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    parser.add_argument("--full", action="store_true", help="include metrics for every function")
    parser.add_argument("--max-files", type=int, default=20000)
    args = parser.parse_args(argv[1:])
    if not Path(args.path).exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2
    data = inventory(args.path, args.max_files, args.full)
    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.out} ({data['total_files']} files, {data['python']['stats']['functions']} functions)")
    else:
        dump(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
