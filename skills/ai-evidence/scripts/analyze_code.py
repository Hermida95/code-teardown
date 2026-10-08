#!/usr/bin/env python3
"""Code pipeline, step 1: look for traces of AI involvement in a source tree.

Static, standard library only. The code is read as text (Python is also parsed with `ast`, which
never imports or runs anything); nothing is executed, installed or sent anywhere. It never runs git:
a repository's own configuration can make git launch programs. Commit history comes only from a log
file that you export yourself (see --git-log) and that deliberately has no author names or e-mails.

Using AI to write code is normal and legitimate. This looks for traces, it does not judge them:

  * RESIDUE: things an assistant leaves behind. Committed assistant configuration (CLAUDE.md,
    .cursorrules...), "Co-Authored-By: <assistant>" commit trailers, "rest of the code remains the
    same" placeholders, Markdown fences pasted at the top of a source file, chat replies in comments.
  * STYLE: comment habits, uniform docstrings, step-numbered comments, emoji in log messages. Every
    one of these is also how plenty of people write, and formatters, templates and tutorials produce
    them too. They weigh very little, and score_evidence.py caps their total weight.

Usage: analyze_code.py PATH [--git-log LOG] [--out evidence.json] [--force]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}).")

import argparse
import ast
import hashlib
import json
import os
import re
import warnings
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import checked_output_path, clean_quote, dump  # noqa: E402
import analyze_text as at  # noqa: E402
from analyze_image import T, ev  # noqa: E402

VERSION = "0.1.0"
MAX_FILES = 3000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_LINE = 2000
MAX_LOG_BYTES = 8 * 1024 * 1024
MIN_CODE_LINES = 200
MIN_FUNCTIONS = 10
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", "vendor", "target", ".tox", ".mypy_cache",
             ".pytest_cache", "site-packages", ".idea", ".vscode", "coverage", ".next", ".gradle", "bower_components", "third_party"}

# Rates are per 100 code lines unless stated. All thresholds are reasoned guesses, not calibrated.
NARRATING_HIGH = 8.0
COMMENT_RATIO_HIGH = 0.35
DOCSTRING_SHARE, SECTION_SHARE = 0.9, 0.6
TRIVIAL_WITH_SECTIONS = 5

LANGS = {".py": ("Python", ("#",), None), ".rb": ("Ruby", ("#",), None), ".sh": ("Shell", ("#",), None), ".pl": ("Perl", ("#",), None),
         ".js": ("JavaScript", ("//",), ("/*", "*/")), ".jsx": ("JavaScript", ("//",), ("/*", "*/")), ".mjs": ("JavaScript", ("//",), ("/*", "*/")),
         ".ts": ("TypeScript", ("//",), ("/*", "*/")), ".tsx": ("TypeScript", ("//",), ("/*", "*/")), ".java": ("Java", ("//",), ("/*", "*/")),
         ".c": ("C", ("//",), ("/*", "*/")), ".h": ("C", ("//",), ("/*", "*/")), ".cc": ("C++", ("//",), ("/*", "*/")), ".cpp": ("C++", ("//",), ("/*", "*/")),
         ".hpp": ("C++", ("//",), ("/*", "*/")), ".cs": ("C#", ("//",), ("/*", "*/")), ".go": ("Go", ("//",), ("/*", "*/")), ".rs": ("Rust", ("//",), ("/*", "*/")),
         ".kt": ("Kotlin", ("//",), ("/*", "*/")), ".swift": ("Swift", ("//",), ("/*", "*/")), ".scala": ("Scala", ("//",), ("/*", "*/")),
         ".php": ("PHP", ("//", "#"), ("/*", "*/")), ".lua": ("Lua", ("--",), None), ".sql": ("SQL", ("--",), None)}

AI_TOOL_FILES = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".clinerules", ".aider.conf.yml", ".aider.chat.history.md",
                 ".aider.input.history", ".github/copilot-instructions.md", ".claude", ".cursor", ".windsurf", ".continue", ".roo", ".kiro", ".codex"]
AI_NAMES = r"(?:chat\s?gpt|gpt-?\d[\w.]*|openai|claude|anthropic|copilot|github copilot|gemini|bard|cursor|codeium|windsurf|llama|an? ai(?: assistant| model)?|ai assistant)"
SOURCE_MARKERS = [re.compile(rf"\bthis (?:file|code|function|class|module|script|implementation|test|snippet) (?:was|is) (?:automatically )?(?:generated|written|created|produced|authored)(?: \w+){{0,3}} (?:by|with|using) {AI_NAMES}", re.I),
                  re.compile(rf"\b(?:generated|written|created|authored|assisted) (?:by|with|using) {AI_NAMES}\b", re.I),
                  re.compile(r"\bai[- ]generated\b", re.I)]
PLACEHOLDERS = [re.compile(r"\.\.\.\s*(?:rest of|existing|previous|remaining|other)\s+(?:the\s+)?(?:code|file|implementation|methods?|functions?|imports?|class)\b", re.I),
                re.compile(r"(?:#|//|/\*)\s*(?:the )?(?:rest of|remaining)\s+(?:the\s+)?(?:code|file|implementation|function|class)\s+(?:remains|stays|is)\s+(?:the same|unchanged)", re.I),
                re.compile(r"(?:#|//|/\*)\s*(?:your|add your|insert your)\s+(?:code|logic|implementation)\s+here\b", re.I),
                re.compile(r"(?:#|//)\s*\(?(?:implementation|code) (?:omitted|truncated|unchanged) for brevity", re.I)]
CHAT_REPLY = re.compile(r"(?:#|//|/\*|\*)\s*(?:here(?:'s| is) the (?:updated|complete|full|revised|fixed|final|corrected) (?:code|version|implementation|file)|certainly!|sure,? here(?:'s| is)|i(?:'ve| have) (?:updated|added|fixed|refactored|rewritten)\b|as an ai\b)", re.I)
STEP_COMMENT = re.compile(r"^\s*(?:#|//)\s*step\s*\d+\s*[:.\-)]", re.I | re.M)
INFORMAL = re.compile(r"(?:#|//|/\*)[^\n]{0,60}\b(?:hack|kludge|wtf|fixme|xxx|ugh|crap|damn|hmm)\b|\b(?:TODO|FIXME)\(\w+\)", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2705\u274C\u2728\u2B50\u26A1]")      # pictographs; not the symbols (check marks, bullets) every CLI uses
EMOJI_HEADING = re.compile(r"^#{1,4}\s*[\U0001F300-\U0001FAFF\u2705\u2728\u26A1]", re.M)
GENERATED_HEADER = re.compile(r"code generated .{0,80}do not edit|@generated\b|automatically generated by (?:protoc|swagger|openapi|grpc|thrift|bison|flex|antlr|sqlc)|generated by the protocol buffer compiler", re.I)
VERBS = {"initialize", "initialise", "create", "set", "get", "loop", "iterate", "return", "check", "define", "import", "add", "update", "calculate", "print", "open", "close",
         "read", "write", "convert", "call", "append", "increment", "declare", "assign", "handle", "validate", "parse", "load", "save", "send", "build", "make", "run", "start", "return"}
TRAILER = re.compile(r"^\s*co-authored-by:[^\n]*?(claude|copilot|chatgpt|openai|gpt|cursor|gemini|codex|aider|devin|anthropic|windsurf|cline)|generated with \[?(claude code|claude|cursor|copilot|codex|gemini)|🤖 generated with|\bgenerated by (claude|chatgpt|copilot|cursor|codex)", re.I | re.M)
CONVENTIONAL = re.compile(r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\([^)]*\))?!?:\s", re.I)
MESSY = re.compile(r"^(?:wip|fix(?:ed)? typo|typo|asdf|misc|stuff|more changes|update$|updates$|oops|temp|test$|\.+$|revert)", re.I)
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9]*")


# --- reading the tree ------------------------------------------------------------------------

def walk_files(root: Path) -> tuple[list[tuple[str, str]], dict]:
    """Return [(relative path, text)] for source files, plus counters. Never follows symlinks; bounded."""
    stats = {"skipped_large": 0, "skipped_binary": 0, "skipped_limit": 0, "bytes": 0}
    out: list[tuple[str, str]] = []
    if root.is_file():
        candidates = [(root.name, root)]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if Path(name).suffix.lower() in LANGS or name.lower().startswith("readme"):
                    candidates.append((str(path.relative_to(root)), path))
    for rel, path in candidates:
        if len(out) >= MAX_FILES or stats["bytes"] >= MAX_TOTAL_BYTES:
            stats["skipped_limit"] += 1
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                stats["skipped_large"] += 1
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            stats["skipped_binary"] += 1
            continue
        stats["bytes"] += len(raw)
        out.append((rel, raw.decode("utf-8", "replace")))
    return out, stats


def ai_tool_files(root: Path) -> list[str]:
    if root.is_file():
        return []
    found = []
    for name in AI_TOOL_FILES:
        path = root / name
        if os.path.lexists(path) and not path.is_symlink():
            found.append(name)
    return found


# --- per-file measurements -------------------------------------------------------------------------

def scan_file(text: str, ext: str) -> dict:
    lang, prefixes, block = LANGS[ext]
    code = comments = 0
    comment_lines: list[tuple[int, str]] = []
    lines = text.split("\n")
    in_block = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or len(line) > MAX_LINE:
            continue
        if in_block:
            comments += 1
            if block and block[1] in line:
                in_block = False
            continue
        if line.startswith(prefixes):
            comments += 1
            if not line.startswith(("#!", "# -*-", "# type:", "# noqa", "# pylint", "# pragma", "# fmt:", "# isort", "# mypy", "//go:", "// eslint", "// @ts-", "// prettier")):
                comment_lines.append((index, line.lstrip("#/- ").strip()))
        elif block and line.startswith(block[0]):
            comments += 1
            if block[1] not in line[len(block[0]):]:
                in_block = True
        else:
            code += 1
    narrating = 0
    for index, content in comment_lines:
        words = IDENT.findall(content)
        if not 2 <= len(words) <= 10 or words[0].lower() not in VERBS or content.endswith((":", "?")):
            continue
        following = next((lines[j].strip() for j in range(index + 1, min(index + 4, len(lines))) if lines[j].strip()), "")
        if not following or following.startswith(prefixes) or len(following) > 90:
            continue
        shared = {w.lower() for w in words[1:]} & {w.lower() for w in IDENT.findall(following)}
        if shared or len(following) <= 40:
            narrating += 1
    return {"lang": lang, "code": code, "comments": comments, "narrating": narrating,
            "steps": len(STEP_COMMENT.findall(text)), "emoji": len(EMOJI.findall(text)), "informal": len(INFORMAL.findall(text))}


def python_docstrings(text: str) -> dict | None:
    """Docstring habits of a Python file, from its syntax tree (never imported or run)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    functions = documented = sections = trivial_sections = 0
    try:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__"):
                functions += 1
                doc = ast.get_docstring(node)
                if doc:
                    documented += 1
                    structured = bool(re.search(r"^\s*(?:Args?|Arguments|Parameters|Returns?|Raises?)\s*:", doc, re.M))
                    sections += structured
                    if structured and len(node.body) <= 3:
                        trivial_sections += 1
    except RecursionError:
        return None
    return {"functions": functions, "documented": documented, "sections": sections, "trivial_sections": trivial_sections}


# --- git log (optional, exported by the user) --------------------------------------------------------

def parse_git_log(text: str) -> list[dict]:
    commits = []
    for record in text.split("\x1e")[1:5001]:
        head, _, tail = record.partition("\x1d")
        fields = head.split("\x1f", 3)
        if len(fields) < 3:
            continue
        added = 0
        for line in tail.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].isdigit():
                added += int(parts[0])
        commits.append({"date": fields[1].strip(), "subject": fields[2].strip(), "body": fields[3] if len(fields) > 3 else "", "added": added})
    return commits


def seconds(stamp: str) -> float | None:
    import datetime
    try:
        return datetime.datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def history_evidence(commits: list[dict]) -> tuple[list[dict], list[dict]]:
    out: list[dict] = []
    n = len(commits)
    measurements = [{"label": T("Commits in the log", "Commits en el registro"), "value": str(n)}]
    if not n:
        return out, measurements
    tools = Counter()
    with_trailer = 0
    for c in commits:
        found = {m.group(m.lastindex).lower() for m in TRAILER.finditer(c["subject"] + "\n" + c["body"]) if m.lastindex}
        if found:
            with_trailer += 1
            tools.update(found)
    share = with_trailer / n
    measurements.append({"label": T("Commits declaring an AI assistant", "Commits que declaran un asistente de IA"), "value": f"{with_trailer}/{n}"})
    if with_trailer:
        names = ", ".join(sorted(tools))
        out.append(ev("cd-commit-trailers", "ai_edited", 9.5, 0.9, T("commit messages", "mensajes de commit"),
                      T(f"{with_trailer} of {n} commits declare an AI assistant ({names})", f"{with_trailer} de {n} commits declaran un asistente de IA ({names})"),
                      T("A \"Co-Authored-By\" trailer or a \"Generated with\" line names an assistant. It is a declaration written by the tool or the person: it shows the assistant took part in those commits, not how much of the code it wrote.",
                        "Una línea «Co-Authored-By» o «Generated with» nombra a un asistente. Es una declaración escrita por la herramienta o por la persona: indica que el asistente participó en esos commits, no cuánto del código escribió."),
                      source="residue"))
        if share >= 0.5 and n >= 5:
            out.append(ev("cd-commit-trailers-share", "generated", 8.0, 0.4, T("commit messages", "mensajes de commit"),
                          T(f"{share * 100:.0f}% of the commits declare an assistant", f"El {share * 100:.0f}% de los commits declara un asistente"),
                          T("When most commits declare an assistant, a large share of the work went through it. It can still have been reviewed and rewritten line by line.",
                            "Cuando la mayoría de los commits declara un asistente, una gran parte del trabajo pasó por él. Aun así pudo revisarse y reescribirse línea a línea."), source="residue"))
    total_added = sum(c["added"] for c in commits)
    first = commits[0]["added"]
    if total_added >= 500 and first / total_added >= 0.7 and n <= 5:
        out.append(ev("cd-history-shape", "generated", 6.0, 0.1, T("commit history", "historial de commits"),
                      T(f"The whole project arrives in {n} commit(s)", f"Todo el proyecto llega en {n} commit(s)"),
                      T("A project that appears almost complete in its first commit, with no iteration visible, is typical of generated scaffolds. It is equally typical of code imported from elsewhere or squashed before publishing.",
                        "Un proyecto que aparece casi completo en su primer commit, sin iteraciones visibles, es típico de los andamios generados. También lo es del código importado de otro sitio o aplastado antes de publicarlo."), source="style"))
    stamps = [seconds(c["date"]) for c in commits]
    bursts = sum(1 for a, b in zip(stamps, stamps[1:]) if a is not None and b is not None and 0 <= b - a <= 120)
    if n >= 15 and bursts / (n - 1) >= 0.4:
        out.append(ev("cd-history-bursts", "generated", 6.0, 0.08, T("commit history", "historial de commits"),
                      T(f"{bursts / (n - 1) * 100:.0f}% of the commits come within two minutes of the previous one", f"El {bursts / (n - 1) * 100:.0f}% de los commits llega a menos de dos minutos del anterior"),
                      T("Agent-driven workflows commit in rapid bursts. So do scripts, rebases and anyone committing small steps.", "Los flujos con agentes hacen commits en ráfagas. También lo hacen los scripts, los rebases y quien hace commits pequeños."), source="style"))
    conventional = sum(bool(CONVENTIONAL.match(c["subject"])) for c in commits) / n
    average_body = sum(len(c["body"].strip()) for c in commits) / n
    measurements.append({"label": T("Conventional-commit share", "Proporción de commits convencionales"), "value": f"{conventional * 100:.0f}%"})
    if n >= 10 and conventional >= 0.9 and average_body >= 200:
        out.append(ev("cd-commit-style", "generated", 6.0, 0.1, T("commit messages", "mensajes de commit"),
                      T("Uniform, long conventional commit messages", "Mensajes de commit convencionales, uniformes y largos"),
                      T("Nearly every commit follows the conventional format and has a long body. Assistants write messages like this; so do disciplined teams with commit templates.",
                        "Casi todos los commits siguen el formato convencional y tienen un cuerpo largo. Los asistentes escriben mensajes así; también los equipos disciplinados con plantillas de commit."), source="style"))
    messy = sum(bool(MESSY.match(c["subject"])) for c in commits) / n
    if n >= 10 and messy >= 0.15:
        out.append(ev("cd-messy-history", "generated", 3.0, 0.1, T("commit history", "historial de commits"),
                      T(f"{messy * 100:.0f}% of the commits are \"wip\", \"typo\", \"update\"...", f"El {messy * 100:.0f}% de los commits son «wip», «typo», «update»..."),
                      T("Untidy, informal messages are what a person working in the open leaves. An assistant can be told to imitate them.", "Los mensajes desordenados e informales son lo que deja una persona trabajando a la vista. Se le puede pedir a un asistente que los imite."), source="style"))
    return out, measurements


# --- evidence from the files -----------------------------------------------------------------------

COMMENT_START = ("#", "//", "/*", "*", "--", "...")


def first_hit(files: list[tuple[str, str]], patterns, *, header_only: bool = False, exts=None):
    """First match of a pattern on a line that is itself a comment (or a bare "..." line). Requiring a whole-line
    comment keeps out the pattern definitions, test fixtures and docs of detectors, linters and this very tool."""
    for rel, text in files:
        if exts is not None and Path(rel).suffix.lower() not in exts:
            continue
        lines = text.split("\n")[: 15 if header_only else 4000]
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if len(line) > MAX_LINE or not stripped.startswith(COMMENT_START):
                continue
            for pattern in patterns if isinstance(patterns, (list, tuple)) else [patterns]:
                match = pattern.search(stripped)
                if match:
                    return rel, number, match.group(0)
    return None


def analyze(path: Path, git_log: str | None = None) -> dict:
    files, walk_stats = walk_files(path)
    code_files = [(rel, text) for rel, text in files if Path(rel).suffix.lower() in LANGS]
    root_name = path.name or str(path)
    evidence: list[dict] = []
    measurements: list[dict] = []

    tool_files = ai_tool_files(path)
    if tool_files:
        evidence.append(ev("cd-ai-tool-files", "ai_edited", 9.0, 0.8, ", ".join(tool_files[:4]),
                           T("Configuration files of AI coding assistants are committed", "Hay archivos de configuración de asistentes de IA para programar"),
                           T("Files such as CLAUDE.md, AGENTS.md, .cursorrules or .github/copilot-instructions.md exist in the project: " + ", ".join(tool_files) + ". They show that an assistant was set up for this project, not how much of the code it wrote, and they can be added without ever using the tool.",
                             "Existen en el proyecto archivos como CLAUDE.md, AGENTS.md, .cursorrules o .github/copilot-instructions.md: " + ", ".join(tool_files) + ". Indican que se configuró un asistente para este proyecto, no cuánto código escribió, y se pueden añadir sin usar nunca la herramienta."),
                           source="residue"))

    generated_files = [rel for rel, text in code_files if GENERATED_HEADER.search("\n".join(text.split("\n")[:12]))]
    analysed = [(rel, text) for rel, text in code_files if rel not in set(generated_files)]
    if generated_files:
        evidence.append(ev("cd-generated-code-headers", "generated", 5.0, 0.0, ", ".join(generated_files[:3]),
                           T(f"{len(generated_files)} file(s) generated by build tools", f"{len(generated_files)} archivo(s) generados por herramientas de compilación"),
                           T("Headers such as \"Code generated ... DO NOT EDIT\" mark output of protoc, OpenAPI generators and similar tools. It is machine-made, not AI-made, and these files are left out of the style statistics.",
                             "Cabeceras como «Code generated ... DO NOT EDIT» marcan la salida de protoc, de generadores OpenAPI y similares. Es código hecho por máquina, no por IA, y estos archivos se excluyen de las estadísticas de estilo."), source="structure"))

    hit = first_hit(analysed, SOURCE_MARKERS[:1]) or first_hit(analysed, SOURCE_MARKERS[1:], header_only=True)
    if hit:
        evidence.append(ev("cd-source-markers", "ai_edited", 8.5, 0.6, f"{hit[0]}:{hit[1]}",
                           T("A comment says an AI wrote or helped with this code", "Un comentario dice que una IA escribió este código o ayudó"),
                           T("A comment declares AI authorship (\"this code was generated by ...\", \"written with Copilot\"). In an application that calls an AI service, comments about the service's output can look the same, so read the line.",
                             "Un comentario declara autoría de una IA («este código fue generado por...», «escrito con Copilot»). En una aplicación que llama a un servicio de IA, los comentarios sobre la salida del servicio pueden parecerse, así que conviene leer la línea."),
                           quote=hit[2], source="residue"))
    hit = first_hit(analysed, PLACEHOLDERS)
    if hit:
        evidence.append(ev("cd-llm-placeholders", "generated", 9.0, 0.6, f"{hit[0]}:{hit[1]}",
                           T("Placeholder left by a code-writing assistant", "Marcador que dejó un asistente al escribir código"),
                           T("A line such as \"... rest of the code remains the same\" is how assistants abbreviate when they rewrite a file. Left in a source file it means a chat answer was pasted. \"Your code here\" also appears in hand-written templates and tutorials.",
                             "Una línea como «... el resto del código sigue igual» es como abrevian los asistentes al reescribir un archivo. Si queda en un archivo de código, significa que se pegó una respuesta de chat. «Tu código aquí» también aparece en plantillas y tutoriales escritos a mano."),
                           quote=hit[2], source="residue"))
    hit = first_hit(analysed, [CHAT_REPLY])
    if hit:
        evidence.append(ev("cd-chat-reply-comments", "ai_edited", 8.0, 0.6, f"{hit[0]}:{hit[1]}",
                           T("A comment reads like a chat reply", "Un comentario suena a respuesta de chat"),
                           T("Comments such as \"Here's the updated code\" or \"Certainly!\" are chat replies pasted into the file.", "Comentarios como «Here's the updated code» o «Certainly!» son respuestas de chat pegadas en el archivo."),
                           quote=hit[2], source="residue"))
    for rel, text in analysed:
        lines = [line for line in text.split("\n") if line.strip()]
        edges = lines[:3] + lines[-3:]
        if Path(rel).suffix.lower() != ".md" and any(line.strip().startswith("```") for line in edges):
            evidence.append(ev("cd-markdown-fences", "generated", 8.5, 0.7, rel,
                               T("Markdown fence at the start or end of a source file", "Valla de Markdown al principio o al final de un archivo de código"),
                               T("A line starting with three backticks at the edge of a source file is what is left when a chat code block is copied with its fence.",
                                 "Una línea que empieza con tres comillas invertidas en el borde de un archivo de código es lo que queda al copiar un bloque de código de un chat con su valla."), source="residue"))
            break

    readme = next(((rel, text) for rel, text in files if Path(rel).name.lower().startswith("readme")), None)
    if readme:
        for item in at.residue_evidence(readme[1][:200_000], at.detect_language(at.WORD.findall(readme[1][:20_000]))):
            item["id"] = item["id"].replace("tx-", "cd-readme-", 1)
            item["claim"] = "ai_edited"
            item["weight"] = min(item["weight"], 0.7)
            item["where"] = readme[0]
            evidence.append(item)
        headings = len(EMOJI_HEADING.findall(readme[1]))
        if headings >= 3:
            evidence.append(ev("cd-readme-emoji-headings", "generated", 6.0, 0.08, readme[0],
                               T(f"{headings} README headings start with an emoji", f"{headings} encabezados del README empiezan con un emoji"),
                               T("Emoji-led headings (\"## 🚀 Features\") are a habit of assistant-written READMEs. Plenty of people write them too.",
                                 "Los encabezados con emoji («## 🚀 Características») son una costumbre de los README escritos por asistentes. Muchas personas también los escriben."), source="style"))

    # ---- style statistics on what is left
    totals = Counter()
    languages = Counter()
    docs = Counter()
    for rel, text in analysed:
        ext = Path(rel).suffix.lower()
        if ext not in LANGS:
            continue
        stats = scan_file(text, ext)
        languages[stats.pop("lang")] += stats["code"]
        totals.update(stats)
        if ext == ".py":
            d = python_docstrings(text)
            if d:
                docs.update(d)
    code, comments = totals["code"], totals["comments"]
    digest = hashlib.sha256()
    for rel, text in sorted(files):
        digest.update(rel.encode("utf-8", "replace") + b"\0" + text.encode("utf-8", "replace"))
    measurements += [{"label": T("Files analysed", "Archivos analizados"), "value": str(len(analysed))},
                     {"label": T("Code lines", "Líneas de código"), "value": str(code)},
                     {"label": T("Languages", "Lenguajes"), "value": ", ".join(f"{n} ({c})" for n, c in languages.most_common(5)) or "-"}]
    if code < MIN_CODE_LINES:
        evidence.append(ev("cd-skipped", "generated", 5.0, 0.0, T("source files", "archivos de código"), T("Style analysis skipped", "Análisis de estilo omitido"),
                           T(f"Only {code} code lines were found; style statistics need at least {MIN_CODE_LINES}.", f"Solo hay {code} líneas de código; las estadísticas de estilo necesitan al menos {MIN_CODE_LINES}."), source="style"))
    else:
        narrating_rate = totals["narrating"] * 100 / code
        ratio = comments / code
        measurements += [{"label": T("Comment lines per code line", "Líneas de comentario por línea de código"), "value": f"{ratio:.2f}"},
                         {"label": T("Narrating comments per 100 code lines", "Comentarios que narran el código por cada 100 líneas"), "value": f"{narrating_rate:.1f}"},
                         {"label": T("Step-numbered comments", "Comentarios numerados por pasos"), "value": str(totals["steps"])},
                         {"label": T("Emoji in code", "Emoji en el código"), "value": str(totals["emoji"])}]
        where = T(f"{len(analysed)} source files", f"{len(analysed)} archivos de código")
        if narrating_rate >= NARRATING_HIGH:
            evidence.append(ev("cd-narrating-comments", "generated", 6.5, 0.12, where, T(f"Comments that restate the next line ({narrating_rate:.1f} per 100 code lines)", f"Comentarios que repiten la línea siguiente ({narrating_rate:.1f} por cada 100 líneas)"),
                               T("Comments like \"# Initialize the list\" above `items = []` are typical of assistant output. Beginners, teaching material and some house styles write them too.",
                                 "Comentarios como «# Inicializa la lista» sobre `items = []` son típicos de la salida de un asistente. Los principiantes, el material didáctico y algunos estilos de casa también los escriben."), source="style"))
        elif narrating_rate < 1.0 and code >= 600:
            evidence.append(ev("cd-narrating-comments", "generated", 4.0, 0.08, where, T("Almost no comments that restate the code", "Casi ningún comentario que repita el código"),
                               T("Code that does not narrate itself leans slightly towards a human. Assistants can be told to comment less.", "El código que no se narra a sí mismo se inclina un poco hacia una persona. Se le puede pedir a un asistente que comente menos."), source="style"))
        if ratio >= COMMENT_RATIO_HIGH:
            evidence.append(ev("cd-comment-density", "generated", 6.0, 0.1, where, T(f"Very heavily commented ({ratio:.2f} comment lines per code line)", f"Muy comentado ({ratio:.2f} líneas de comentario por línea de código)"),
                               T("A high share of comment lines is a habit of assistants. Documented libraries and teaching code have it too.", "Una proporción alta de líneas de comentario es una costumbre de los asistentes. Las bibliotecas documentadas y el código didáctico también la tienen."), source="style"))
        if totals["steps"] >= 3:
            evidence.append(ev("cd-step-comments", "generated", 6.0, 0.1, where, T(f"{totals['steps']} comments numbered \"Step N\"", f"{totals['steps']} comentarios numerados «Step N»"),
                               T("\"# Step 1: ...\" comments walk through a function like a tutorial, which is how assistants explain code. Scripts written as walkthroughs do too.", "Los comentarios «# Step 1: ...» recorren una función como un tutorial, que es como los asistentes explican el código. Los scripts escritos como guías también."), source="style"))
        if totals["emoji"] >= 5:
            evidence.append(ev("cd-emoji-in-code", "generated", 6.0, 0.1, where, T(f"{totals['emoji']} emoji in code and messages", f"{totals['emoji']} emoji en el código y los mensajes"),
                               T("Emoji in log lines and comments (\"✅ Done\", \"❌ Failed\") are a habit of assistant-written scripts. Some people and some CLIs use them deliberately.", "Los emoji en líneas de log y comentarios («✅ Hecho», «❌ Falló») son una costumbre de los scripts escritos por asistentes. Algunas personas y algunas CLI los usan a propósito."), source="style"))
        if totals["informal"] >= 3:
            evidence.append(ev("cd-informal-comments", "generated", 3.0, 0.1, where, T(f"{totals['informal']} informal or personal comments", f"{totals['informal']} comentarios informales o personales"),
                               T("\"hack\", \"wtf\", \"TODO(name)\" and similar leave a human voice in the code. Assistants can be told to imitate it.", "«hack», «wtf», «TODO(nombre)» y parecidos dejan una voz humana en el código. Se le puede pedir a un asistente que la imite."), source="style"))
    if docs["functions"] >= MIN_FUNCTIONS:
        documented, sections = docs["documented"] / docs["functions"], docs["sections"] / docs["functions"]
        measurements += [{"label": T("Python functions with a docstring", "Funciones Python con docstring"), "value": f"{documented * 100:.0f}% of {docs['functions']}"},
                         {"label": T("...with Args/Returns sections", "...con secciones Args/Returns"), "value": f"{sections * 100:.0f}%"}]
        if (documented >= DOCSTRING_SHARE and sections >= SECTION_SHARE) or docs["trivial_sections"] >= TRIVIAL_WITH_SECTIONS:
            evidence.append(ev("cd-docstring-uniformity", "generated", 6.5, 0.12, T("Python functions", "funciones Python"),
                               T(f"Nearly every function has a structured docstring ({documented * 100:.0f}% documented, {sections * 100:.0f}% with Args/Returns)", f"Casi todas las funciones tienen docstring estructurado ({documented * 100:.0f}% documentadas, {sections * 100:.0f}% con Args/Returns)"),
                               T("Complete Args/Returns docstrings, even on tiny functions, are how assistants document. Libraries that enforce a docstring linter look the same.", "Los docstrings completos con Args/Returns, incluso en funciones minúsculas, son como documentan los asistentes. Las bibliotecas que imponen un linter de docstrings se ven igual."), source="style"))

    history_items, history_measurements = (history_evidence(parse_git_log(git_log)) if git_log is not None else ([], []))
    evidence += history_items
    measurements += history_measurements
    seen, unique = set(), []
    for item in evidence:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    notes = []
    if walk_stats["skipped_large"]:
        notes.append(T(f"{walk_stats['skipped_large']} file(s) over {MAX_FILE_BYTES // 1024} KB were skipped.", f"Se omitieron {walk_stats['skipped_large']} archivo(s) de más de {MAX_FILE_BYTES // 1024} KB."))
    if walk_stats["skipped_limit"]:
        notes.append(T(f"The size or file-count limit was reached; {walk_stats['skipped_limit']} file(s) were not read.", f"Se alcanzó el límite de tamaño o de archivos; no se leyeron {walk_stats['skipped_limit']} archivo(s)."))
    limits = [
        T("Using AI to write code is normal and legitimate. This report says which traces exist, not whether the code is good or the work honest; for quality, use code-teardown.",
          "Usar IA para escribir código es normal y legítimo. Este informe dice qué rastros hay, no si el código es bueno ni si el trabajo es honrado; para la calidad, usa code-teardown."),
        T("Style signals are very unreliable here: formatters, linters, templates, scaffolding tools, tutorials and copied snippets all produce uniform, heavily commented code, and an assistant can be told to write in any style.",
          "Las señales de estilo son muy poco fiables aquí: los formateadores, los linters, las plantillas, las herramientas de andamiaje, los tutoriales y los fragmentos copiados producen código uniforme y muy comentado, y se le puede pedir a un asistente que escriba con cualquier estilo."),
        T("Without a git log the history is not examined. With one, no author names or e-mails are read.", "Sin un registro de git no se examina el historial. Con uno, no se leen nombres de autor ni correos."),
        T("Code can be AI-written and show none of this; absence of traces is not evidence of a human author.", "El código puede estar escrito por IA y no mostrar nada de esto; la ausencia de rastros no es prueba de que lo escribiera una persona."),
    ] + notes
    return {"tool": "analyze_code", "version": VERSION,
            "file": {"name": root_name, "format": "code", "modality": "code", "files": len(analysed), "lines": code, "sha256": digest.hexdigest()},
            "evidence": unique, "measurements": measurements, "limits": limits,
            "coverage": {"checked": [T("assistant configuration files", "archivos de configuración de asistentes"), T("residue in source files and the README", "restos en los archivos de código y el README"),
                                     T("comment and docstring habits", "hábitos de comentarios y docstrings")] + ([T("commit trailers and history shape (from the log you supplied)", "marcas de commit y forma del historial (del registro que aportaste)")] if git_log is not None else []),
                         "not_checked": ([] if git_log is not None else [T("commit history (no git log supplied)", "historial de commits (no se aportó registro de git)")])
                         + [T("dependencies that do not exist on the registries (needs the network)", "dependencias que no existen en los registros (necesita la red)"),
                            T("whether the code works or is any good (see code-teardown)", "si el código funciona o es bueno (ver code-teardown)"),
                            T("who wrote it: a comparison with the author's other code", "quién lo escribió: una comparación con otro código del autor")]}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Look for traces of AI involvement in a source tree (static, stdlib only).")
    parser.add_argument("path", help="a project directory or a single source file")
    parser.add_argument("--git-log", help="a log you exported yourself with the command in SKILL.md (no author names); git is never run here")
    parser.add_argument("--out", help="write the JSON to this .json file instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = parser.parse_args()
    path = Path(args.path).expanduser()
    if not path.exists():
        sys.exit(f"error: {path} does not exist")
    log = None
    if args.git_log:
        log_path = Path(args.git_log).expanduser()
        if not log_path.is_file():
            sys.exit(f"error: {log_path} is not a file")
        with log_path.open("rb") as handle:
            log = handle.read(MAX_LOG_BYTES).decode("utf-8", "replace")
    result = analyze(path, log)
    if args.out:
        out = checked_output_path(args.out, ".json", args.force)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(result['evidence'])} evidence items)")
    else:
        dump(result)


if __name__ == "__main__":
    main()
