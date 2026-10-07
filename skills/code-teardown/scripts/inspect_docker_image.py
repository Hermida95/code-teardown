#!/usr/bin/env python3
"""Step 2/3 of the pipeline for Docker images: parse an image archive, never run it.

Reads a `docker save` tar (classic or OCI layout), an extracted image directory,
or an image reference (which runs `docker save`, never `docker run`). Reports
config, layers, reconstructed instructions, the merged filesystem and signals.

Evidence is citeable as `layer N`, `history[i]` or `config.<field>`.
Layer contents are only listed; a small allow-list of text files under the
app directories is copied to the work dir as data for inventory.py.

Usage: inspect_docker_image.py TARGET [--out WORKDIR] [--no-extract]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"code-teardown needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import io
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SECRET_NAME, TOKEN_VALUE, dump, empty_workdir, safe_join  # noqa: E402

MAX_JSON_BYTES = 5_000_000
MAX_ENTRIES = 2_000_000
MAX_LAYER_DECLARED_BYTES = 10_000_000_000    # file bytes one layer may declare before we stop walking it
MAX_IMAGE_DECLARED_BYTES = 50_000_000_000
MAX_INSTRUCTION_CHARS = 4000                 # regexes only ever see this much of a history entry
IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
MAX_EXTRACT_FILES = 300
MAX_EXTRACT_BYTES = 20_000_000
MAX_EXTRACT_FILE = 256_000
SAVE_TIMEOUT = 900

APP_ROOTS = ("app/", "srv/", "usr/src/", "opt/", "home/", "workspace/", "code/", "work/", "usr/local/app/")
NOISE_SEGMENTS = {"site-packages", "dist-packages", "node_modules", ".git", "__pycache__", ".venv", "venv",
                  ".cache", ".npm"}
TEXT_EXT = {".py", ".js", ".mjs", ".ts", ".sh", ".json", ".toml", ".txt", ".yml", ".yaml", ".cfg", ".ini",
            ".md", ".go", ".rb", ".php", ".sql", ".conf", ".env.example"}
TEXT_NAMES = {"dockerfile", "makefile", "requirements.txt", "package.json", "entrypoint.sh", "procfile"}
OS_FILES = {"etc/os-release", "usr/lib/os-release", "etc/alpine-release", "etc/debian_version"}
RUNTIME_PATHS = {"usr/local/bin/python3": "python", "usr/bin/python3": "python", "usr/local/bin/node": "node",
                 "usr/bin/node": "node", "usr/bin/java": "java", "usr/local/go/bin/go": "go",
                 "usr/bin/ruby": "ruby", "usr/local/bin/ruby": "ruby", "usr/bin/php": "php",
                 "usr/local/bin/php": "php"}
TOOL_PATHS = {"usr/bin/gcc": "gcc", "usr/bin/cc": "cc", "usr/bin/make": "make", "usr/bin/git": "git",
              "usr/bin/curl": "curl", "usr/bin/wget": "wget", "bin/bash": "bash", "usr/bin/ssh": "ssh",
              "usr/bin/sudo": "sudo"}
CACHE_PREFIXES = {"var/lib/apt/lists/": "apt package lists", "var/cache/apt/": "apt cache",
                  "var/cache/apk/": "apk cache", "root/.cache/": "root user cache", "root/.npm/": "npm cache",
                  "tmp/": "/tmp contents", "usr/share/doc/": "package docs", "usr/share/man/": "man pages",
                  "var/cache/": "other /var/cache"}
SENSITIVE = [
    (re.compile(r"(^|/)\.env(\.[\w.-]+)?$"), ".env file"),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"), "SSH private key"),
    (re.compile(r"\.(pem|key|pfx|p12|jks)$"), "key or certificate material"),
    (re.compile(r"(^|/)\.(npmrc|pypirc|netrc|git-credentials)$"), "credentials file"),
    (re.compile(r"(^|/)\.aws/credentials$"), "AWS credentials"),
    (re.compile(r"(^|/)\.docker/config\.json$"), "Docker registry credentials"),
    (re.compile(r"(^|/)\.git/config$"), ".git metadata shipped in the image"),
    (re.compile(r"(^|/)terraform\.tfstate(\.backup)?$"), "Terraform state"),
    (re.compile(r"(^|/)\.htpasswd$"), "htpasswd file"),
]
SENSITIVE_QUIET = ("etc/ssl/", "usr/share/ca-certificates/", "usr/local/share/ca-certificates/", "etc/pki/",
                   "usr/lib/python", "usr/local/lib/python", "usr/lib/node_modules/", "usr/local/lib/node_modules/")
SENSITIVE_TOKENS = (".env", "id_", ".pem", ".key", ".pfx", ".p12", ".jks", "rc", "netrc", "git-credentials",
                    "credentials", "config", "tfstate", "htpasswd")


class ImageError(Exception):
    pass


def norm(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/").rstrip("/")


# --- image stores (tar file or extracted directory) ---------------------------

class TarStore:
    def __init__(self, path: Path):
        self.tf = tarfile.open(path, "r:*")
        self.index: dict[str, tarfile.TarInfo] = {}
        declared = 0
        for member in self.tf:
            self.index[norm(member.name)] = member
            declared += member.size if member.isreg() else 0
            if len(self.index) > MAX_ENTRIES or declared > MAX_IMAGE_DECLARED_BYTES:
                break

    def has(self, name: str) -> bool:
        return norm(name) in self.index

    def read(self, name: str, cap: int = MAX_JSON_BYTES) -> bytes:
        member = self.index.get(norm(name))
        if member is None or not member.isfile():
            raise ImageError(f"missing member {name}")
        if member.size > cap:
            raise ImageError(f"member {name} is larger than {cap} bytes")
        return self.tf.extractfile(member).read()

    def size(self, name: str) -> int:
        return self.index[norm(name)].size

    def open(self, name: str):
        member = self.index.get(norm(name))
        if member is None or not member.isfile():
            raise ImageError(f"missing member {name}")
        return self.tf.extractfile(member)


class DirStore:
    def __init__(self, path: Path):
        self.root = path.resolve()

    def _path(self, name: str) -> Path:
        target = safe_join(self.root, name)
        if target is None:
            raise ImageError(f"unsafe member path {name}")
        return target

    def has(self, name: str) -> bool:
        try:
            return self._path(name).is_file()
        except ImageError:
            return False

    def read(self, name: str, cap: int = MAX_JSON_BYTES) -> bytes:
        path = self._path(name)
        if not path.is_file() or path.stat().st_size > cap:
            raise ImageError(f"missing or oversized member {name}")
        return path.read_bytes()

    def size(self, name: str) -> int:
        return self._path(name).stat().st_size

    def open(self, name: str):
        path = self._path(name)
        if not path.is_file():
            raise ImageError(f"missing member {name}")
        return open(path, "rb")


# --- manifests --------------------------------------------------------------------

def _blob(digest: str) -> str:
    algo, _, hexdigest = digest.partition(":")
    return f"blobs/{algo}/{hexdigest}"


def _resolve_oci(store, descriptors: list[dict], candidates: list[dict], depth: int = 0) -> None:
    for desc in descriptors:
        annotations = desc.get("annotations") or {}
        platform = desc.get("platform") or {}
        if annotations.get("vnd.docker.reference.type") == "attestation-manifest" or platform.get("os") == "unknown":
            continue
        try:
            body = json.loads(store.read(_blob(desc["digest"])))
        except (ImageError, KeyError, json.JSONDecodeError):
            continue
        if "manifests" in body and depth < 3:
            _resolve_oci(store, body["manifests"], candidates, depth + 1)
        elif "layers" in body:
            candidates.append({"manifest": body, "descriptor": desc})


def load_image(store) -> dict:
    """Return layout, tags, config dict, layer refs and any extra images found."""
    if store.has("manifest.json"):
        manifest = json.loads(store.read("manifest.json"))
        if not manifest:
            raise ImageError("manifest.json is empty")
        entry = manifest[0]
        return {"layout": "classic", "tags": entry.get("RepoTags") or [],
                "config": json.loads(store.read(entry["Config"])),
                "layers": [{"name": name} for name in entry["Layers"]],
                "other_images": [e.get("RepoTags") or [] for e in manifest[1:]], "platforms": []}
    if store.has("index.json"):
        index = json.loads(store.read("index.json"))
        candidates: list[dict] = []
        _resolve_oci(store, index.get("manifests", []), candidates)
        if not candidates:
            raise ImageError("no image manifest found in index.json")
        chosen = candidates[0]
        body, desc = chosen["manifest"], chosen["descriptor"]
        tags = []
        for item in index.get("manifests", []):
            ann = item.get("annotations") or {}
            tag = ann.get("io.containerd.image.name") or ann.get("org.opencontainers.image.ref.name")
            if tag and tag not in tags:
                tags.append(tag)
        platforms = []
        for item in candidates:
            p = item["descriptor"].get("platform") or {}
            platforms.append("/".join(x for x in (p.get("os"), p.get("architecture")) if x) or "unspecified")
        return {"layout": "oci", "tags": tags,
                "config": json.loads(store.read(_blob(body["config"]["digest"]))),
                "layers": [{"name": _blob(layer["digest"]), "media_type": layer.get("mediaType"),
                            "digest": layer["digest"]} for layer in body["layers"]],
                "other_images": [], "platforms": platforms}
    raise ImageError("neither manifest.json nor index.json found: not a docker save archive")


# --- history and instructions -----------------------------------------------------------

SECRET_ASSIGN = re.compile(
    r"(?i)(\b[\w.-]{0,64}(?:password|passwd|secret|token|api_?key|private_?key)[\w.-]{0,64})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
SECRET_FLAG = re.compile(r"(?i)(--(?:password|passwd|token|secret)[= ])\S+")


def redact(text: str) -> str:
    """Hide secret values in build instructions; names stay so the finding is still citeable.

    Input is capped and the name part of the pattern is bounded, so a hostile history entry
    cannot make the regex engine run quadratically.
    """
    text = text[:MAX_INSTRUCTION_CHARS]
    text = SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = SECRET_FLAG.sub(lambda m: f"{m.group(1)}<redacted>", text)
    return TOKEN_VALUE.sub("<redacted>", text)


def normalize_instruction(created_by: str) -> str:
    text = created_by.strip()[:MAX_INSTRUCTION_CHARS]
    text = re.sub(r"\s*#\s*buildkit\s*$", "", text)
    if re.match(r"\|\d+\s", text):           # BuildKit "|N ARG=value /bin/sh -c ..." form, parsed without backtracking
        marker = text.find("/bin/sh -c ")
        if marker != -1:
            text = text[marker:]
    if text.startswith("/bin/sh -c #(nop)"):
        return text[len("/bin/sh -c #(nop)"):].strip()
    if text.startswith("/bin/sh -c "):
        return "RUN " + text[len("/bin/sh -c "):].strip()
    return re.sub(r"^RUN /bin/sh -c ", "RUN ", text)


def build_history(config: dict) -> list[dict]:
    history, layer_index = [], 0
    for index, item in enumerate(config.get("history") or []):
        empty = bool(item.get("empty_layer"))
        raw = item.get("created_by", "")
        history.append({"index": index, "layer_index": None if empty else layer_index, "empty_layer": empty,
                        "created": item.get("created"), "instruction": redact(normalize_instruction(raw))[:400]})
        if not empty:
            layer_index += 1
    return history


# --- layer scanning -----------------------------------------------------------------------

def is_sensitive(path: str) -> str | None:
    base = posixpath.basename(path).lower()
    if not any(token in base for token in SENSITIVE_TOKENS):
        return None
    if path.startswith(SENSITIVE_QUIET) or NOISE_SEGMENTS & set(path.split("/")):
        return None
    for pattern, label in SENSITIVE:
        if pattern.search(path):
            return label
    return None


class Scanner:
    def __init__(self, workdir: Path | None, app_roots: tuple[str, ...]):
        self.merged: dict[str, tuple[int, int, int]] = {}      # path -> (layer, size, mode)
        self.layers: list[dict] = []
        self.signals: list[dict] = []
        self.setuid: list[dict] = []
        self.os_files: dict[str, tuple[int, str]] = {}
        self.workdir = workdir
        self.app_roots = app_roots
        self.extracted: dict[str, Path] = {}
        self.extract_bytes = 0
        self.entries = 0
        self.declared_total = 0
        self.truncated = False

    def shadow(self, path: str, by_layer: int) -> None:
        old = self.merged.pop(path, None)
        if old is None:
            return
        layer, size, _ = old
        self.layers[layer]["wasted_bytes"] += size
        label = is_sensitive(path)
        if label and size:
            # It is no longer in the final filesystem: replace the "present" signal with the
            # more precise "deleted but recoverable" one.
            self.signals = [s for s in self.signals
                            if not (s["kind"] == "sensitive_file" and s["path"] == path and s["ref"] == f"layer {layer}")]
            self.signals.append({
                "kind": "deleted_secret_still_in_layer", "ref": f"layer {layer}", "path": path,
                "detail": f"{label} was removed in layer {by_layer} but its content remains in layer {layer} "
                          "and can be recovered from the image"})
        stale = self.extracted.pop(path, None)
        if stale:
            stale.unlink(missing_ok=True)

    def shadow_tree(self, prefix: str, by_layer: int, keep: set[str] = frozenset()) -> None:
        marker = prefix + "/" if prefix else ""
        for key in [k for k in self.merged if (k == prefix or k.startswith(marker)) and k not in keep]:
            self.shadow(key, by_layer)

    def wants_extract(self, path: str, size: int) -> bool:
        if self.workdir is None or size > MAX_EXTRACT_FILE or size == 0:
            return False
        if len(self.extracted) >= MAX_EXTRACT_FILES or self.extract_bytes + size > MAX_EXTRACT_BYTES:
            return False
        if NOISE_SEGMENTS & set(path.split("/")):
            return False
        if not path.startswith(self.app_roots):
            return False
        base = posixpath.basename(path).lower()
        return base in TEXT_NAMES or any(base.endswith(ext) for ext in TEXT_EXT)

    def scan(self, index: int, fileobj, compressed: int, created_by: str | None, digest: str | None) -> None:
        stats = {"index": index, "digest": digest, "compressed_bytes": compressed, "files": 0, "bytes": 0,
                 "wasted_bytes": 0, "whiteouts": 0, "created_by": created_by}
        self.layers.append(stats)
        added: set[str] = set()
        dir_bytes: dict[str, int] = {}
        declared = 0
        with tarfile.open(fileobj=fileobj, mode="r:*") as tar:
            for member in tar:
                declared += member.size if member.isreg() else 0
                self.declared_total += member.size if member.isreg() else 0
                if declared > MAX_LAYER_DECLARED_BYTES or self.declared_total > MAX_IMAGE_DECLARED_BYTES:
                    stats["truncated"] = True      # walking on would mean unpacking a decompression bomb
                    break
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    self.truncated = True
                    break
                path = norm(member.name)
                if not path:
                    continue
                base, parent = posixpath.basename(path), posixpath.dirname(path)
                if base == ".wh..wh..opq":
                    self.shadow_tree(parent, index, keep=added)
                    stats["whiteouts"] += 1
                    continue
                if base.startswith(".wh."):
                    self.shadow_tree(posixpath.join(parent, base[4:]), index)
                    stats["whiteouts"] += 1
                    continue
                size = member.size if member.isreg() else 0
                if path in self.merged:
                    self.shadow(path, index)
                self.merged[path] = (index, size, member.mode)
                added.add(path)
                if not member.isreg():
                    continue
                stats["files"] += 1
                stats["bytes"] += size
                top = "/".join(path.split("/")[:2])
                dir_bytes[top] = dir_bytes.get(top, 0) + size
                if member.mode & 0o4000:
                    self.setuid.append({"path": path, "layer": index})
                label = is_sensitive(path)
                if label:
                    self.signals.append({"kind": "sensitive_file", "ref": f"layer {index}", "path": path,
                                         "detail": f"{label} (content not read)"})
                if path in OS_FILES and size <= 4096:
                    self.os_files[path] = (index, tar.extractfile(member).read().decode("utf-8", "replace"))
                if self.wants_extract(path, size):
                    target = safe_join(self.workdir, path)
                    data = tar.extractfile(member).read(MAX_EXTRACT_FILE)
                    if target and b"\0" not in data[:4096]:
                        try:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(data)
                        except OSError:        # e.g. a path that is a file in one layer and a directory in another
                            continue
                        self.extracted[path] = target
                        self.extract_bytes += len(data)
        stats["top_dirs"] = [{"path": p, "bytes": b} for p, b in sorted(dir_bytes.items(), key=lambda kv: -kv[1])[:5]]


# --- analysis of config and instructions --------------------------------------------------------

def mask_env(env: list[str]) -> list[dict]:
    out = []
    for item in env or []:
        name, _, value = item.partition("=")
        secret = bool(SECRET_NAME.search(name)) or bool(TOKEN_VALUE.search(value))
        out.append({"name": name, "value": "<masked>" if secret else value[:200], "secret_like": secret})
    return out


def instruction_signals(history: list[dict]) -> list[dict]:
    signals = []
    for item in history:
        text, ref = item["instruction"], f"history[{item['index']}]"
        lowered = text.lower()

        def add(kind, detail):
            signals.append({"kind": kind, "ref": ref, "detail": detail})

        if re.search(r"(curl|wget)[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", lowered):
            add("curl_pipe_shell", "downloads and pipes a script straight into a shell")
        if re.search(r"apt(-get)?\s+install", lowered) and "/var/lib/apt/lists" not in lowered:
            add("apt_no_cleanup", "apt install without removing /var/lib/apt/lists in the same instruction")
        if re.search(r"apt(-get)?\s+update", lowered) and not re.search(r"apt(-get)?\s+install", lowered):
            add("apt_update_alone", "apt update in its own instruction gets cached and goes stale")
        if "apk add" in lowered and "--no-cache" not in lowered and "/var/cache/apk" not in lowered:
            add("apk_no_cache", "apk add without --no-cache")
        if re.search(r"pip3?\s+install", lowered) and "--no-cache-dir" not in lowered:
            add("pip_cache", "pip install without --no-cache-dir leaves the pip cache in the layer")
        if re.search(r"chmod\s+(-r\s+)?0?777", lowered):
            add("chmod_777", "world-writable permissions")
        if re.search(r"^add\s+https?://", lowered):
            add("add_remote_url", "ADD from a remote URL (no checksum, no cache control)")
        if re.match(r"^(copy|add)\s+(--\S+\s+)*\.\s", lowered):
            add("copy_whole_context", "copies the whole build context, which can include .git or .env files")
        if re.search(r"(--password[= ]\S+|(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S{6,})", lowered) \
                or (re.match(r"^(env|arg)\s", lowered) and SECRET_NAME.search(text.split("=")[0])):
            add("secret_in_instruction", "a secret-like name or value appears in the build history (value not shown)")
    return signals


def config_signals(cfg: dict, history: list[dict], layer_count: int) -> list[dict]:
    inner = cfg.get("config") or {}
    signals = []
    user = (inner.get("User") or "").strip()
    if user in ("", "root", "0") or user.startswith(("root:", "0:")):
        signals.append({"kind": "runs_as_root", "ref": "config.User",
                        "detail": "no non-root USER is set, so the process runs as root"})
    if not inner.get("Healthcheck"):
        signals.append({"kind": "no_healthcheck", "ref": "config.Healthcheck",
                        "detail": "no HEALTHCHECK defined (depends on the orchestrator)"})
    for item in mask_env(inner.get("Env") or []):
        if item["secret_like"]:
            signals.append({"kind": "secret_in_env", "ref": "config.Env",
                            "detail": f"environment variable {item['name']} looks like a secret (value masked)"})
    if "22/tcp" in (inner.get("ExposedPorts") or {}):
        signals.append({"kind": "ssh_exposed", "ref": "config.ExposedPorts", "detail": "port 22 is exposed"})
    entry = inner.get("Entrypoint") or []
    if entry and entry[0] in ("/bin/sh", "sh", "/bin/bash", "bash") and "-c" in entry:
        signals.append({"kind": "shell_entrypoint", "ref": "config.Entrypoint",
                        "detail": "entrypoint goes through a shell, which can swallow signals as PID 1"})
    if not entry and not inner.get("Cmd"):
        signals.append({"kind": "no_default_command", "ref": "config.Cmd",
                        "detail": "neither Entrypoint nor Cmd is set"})
    if layer_count > 25:
        signals.append({"kind": "many_layers", "ref": "layers", "detail": f"{layer_count} layers"})
    return signals


def summarize_filesystem(scanner: Scanner) -> dict:
    sized = sorted(((size, path, layer) for path, (layer, size, _) in scanner.merged.items() if size),
                   reverse=True)[:15]
    caches: dict[str, int] = {}
    runtimes, tools = set(), set()
    pycache = 0
    for path, (layer, size, _) in scanner.merged.items():
        for prefix, label in CACHE_PREFIXES.items():
            if path.startswith(prefix) and size:
                caches[label] = caches.get(label, 0) + size
                break
        if path in RUNTIME_PATHS:
            runtimes.add(RUNTIME_PATHS[path])
        if path in TOOL_PATHS:
            tools.add(TOOL_PATHS[path])
        if path.endswith("__pycache__"):
            pycache += 1
    return {
        "entries": len(scanner.merged), "files": sum(1 for v in scanner.merged.values() if v[1]),
        "bytes": sum(v[1] for v in scanner.merged.values()),
        "largest_files": [{"path": p, "bytes": s, "layer": l} for s, p, l in sized],
        "cache_and_waste_candidates": [{"what": k, "bytes": v} for k, v in sorted(caches.items(), key=lambda kv: -kv[1])],
        "runtimes": sorted(runtimes), "build_or_network_tools_present": sorted(tools),
        "setuid_files": scanner.setuid[:50], "pycache_dirs": pycache,
        "app_files_extracted": 0,
    }


def os_info(scanner: Scanner) -> dict | None:
    for path in ("etc/os-release", "usr/lib/os-release"):
        if path in scanner.os_files:
            layer, text = scanner.os_files[path]
            fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', text, re.M))
            return {"name": fields.get("PRETTY_NAME") or fields.get("NAME"), "id": fields.get("ID"),
                    "version": fields.get("VERSION_ID"), "ref": f"layer {layer}: {path}"}
    for path in ("etc/alpine-release", "etc/debian_version"):
        if path in scanner.os_files:
            layer, text = scanner.os_files[path]
            return {"name": path.split("/")[-1].split("-")[0].split("_")[0], "version": text.strip(),
                    "ref": f"layer {layer}: {path}"}
    return None


def app_roots_for(cfg: dict) -> tuple[str, ...]:
    inner = cfg.get("config") or {}
    extra = []
    workdir = norm(inner.get("WorkingDir") or "")
    if workdir:
        extra.append(workdir + "/")
    for token in (inner.get("Entrypoint") or []) + (inner.get("Cmd") or []):
        if isinstance(token, str) and token.startswith("/") and "/" in token[1:]:
            extra.append(norm(posixpath.dirname(token)) + "/")
    return tuple(dict.fromkeys(APP_ROOTS + tuple(extra)))


def inspect(store, source: str, workdir: Path | None, saved: bool = False) -> dict:
    image = load_image(store)
    cfg = image["config"]
    inner = cfg.get("config") or {}
    history = build_history(cfg)
    by_layer = {h["layer_index"]: h for h in history if h["layer_index"] is not None}
    diff_ids = (cfg.get("rootfs") or {}).get("diff_ids") or []
    extract_dir = workdir / "image-files" if workdir else None
    scanner = Scanner(extract_dir, app_roots_for(cfg))
    limits: list[str] = []

    for index, layer in enumerate(image["layers"]):
        digest = layer.get("digest") or (diff_ids[index] if index < len(diff_ids) else None)
        instruction = by_layer.get(index, {}).get("instruction")
        try:
            handle = store.open(layer["name"])
            with handle:
                scanner.scan(index, handle, store.size(layer["name"]), instruction, digest)
            if scanner.layers[-1].get("truncated"):
                limits.append(f"layer {index}: the declared file data exceeds the safety cap "
                              f"({MAX_LAYER_DECLARED_BYTES / 1e9:g} GB), possible decompression bomb; its scan stopped early.")
        except (tarfile.TarError, OSError, EOFError, ValueError, ImageError) as exc:
            failure = {"index": index, "digest": digest, "created_by": instruction,
                       "error": f"layer could not be read ({type(exc).__name__}): "
                                "unsupported compression or corrupt data"}
            if len(scanner.layers) > index:      # scan() had already registered this layer
                scanner.layers[index].update(failure)
            else:
                scanner.layers.append(failure)
            limits.append(f"layer {index} could not be read and is missing from the filesystem analysis.")
    if scanner.truncated:
        limits.append(f"Stopped after {MAX_ENTRIES} filesystem entries; the filesystem analysis is partial.")
    if len(image["layers"]) != len(by_layer):
        limits.append("History and layers do not line up one to one (history may be stripped); "
                      "layer-to-instruction mapping can be wrong.")
    if not history:
        limits.append("The image has no history, so its Dockerfile instructions cannot be reconstructed.")
    if image["other_images"]:
        limits.append(f"The archive holds {len(image['other_images'])} more image(s); only the first was analyzed.")
    if len(image["platforms"]) > 1:
        limits.append(f"Multi-platform image ({', '.join(image['platforms'])}); only the first platform was analyzed.")

    filesystem = summarize_filesystem(scanner)
    filesystem["app_files_extracted"] = len(scanner.extracted)
    wasted = sum(layer.get("wasted_bytes", 0) for layer in scanner.layers)
    signals = config_signals(cfg, history, len(image["layers"])) + instruction_signals(history) + scanner.signals
    if wasted > 1_000_000:
        worst = max(scanner.layers, key=lambda l: l.get("wasted_bytes", 0))
        signals.append({"kind": "wasted_space", "ref": f"layer {worst['index']}",
                        "detail": f"{wasted} bytes in earlier layers are hidden by later layers but still shipped"})
    for tool in filesystem["build_or_network_tools_present"]:
        if tool in ("gcc", "cc", "make"):
            signals.append({"kind": "build_tools_in_final_image", "ref": "filesystem",
                            "detail": f"{tool} is present in the final image (a multi-stage build usually avoids this)"})
            break

    return {
        "source": {"input": source, "layout": image["layout"], "saved_with_docker": saved},
        "image": {"tags": image["tags"], "architecture": cfg.get("architecture"), "os": cfg.get("os"),
                  "variant": cfg.get("variant"), "created": cfg.get("created"),
                  "docker_version": cfg.get("docker_version"), "platforms_available": image["platforms"]},
        "base_os": os_info(scanner),
        "config": {"user": inner.get("User") or None, "workdir": inner.get("WorkingDir") or None,
                   "entrypoint": inner.get("Entrypoint"), "cmd": inner.get("Cmd"),
                   "env": mask_env(inner.get("Env") or []),
                   "exposed_ports": sorted((inner.get("ExposedPorts") or {}).keys()),
                   "volumes": sorted((inner.get("Volumes") or {}).keys()),
                   "healthcheck": inner.get("Healthcheck"), "stop_signal": inner.get("StopSignal"),
                   "labels": dict(list((inner.get("Labels") or {}).items())[:20])},
        "history": history,
        "layers": scanner.layers,
        "totals": {"layers": len(image["layers"]),
                   "compressed_bytes": sum(l.get("compressed_bytes", 0) for l in scanner.layers),
                   "merged_bytes": filesystem["bytes"], "wasted_bytes": wasted},
        "filesystem": filesystem,
        "signals": signals,
        "extracted": {"dir": str(extract_dir) if extract_dir and scanner.extracted else None,
                      "files": sorted(scanner.extracted)[:MAX_EXTRACT_FILES]},
        "limits": limits,
        "how_to_cite": "Cite evidence as 'layer N', 'history[i]' or 'config.<field>', as given in each signal's ref.",
    }


def save_image(reference: str, destination: Path) -> None:
    if not IMAGE_REF.fullmatch(reference):
        raise ImageError(f"{reference[:60]!r} is not a valid image reference (expected name[:tag] or name@sha256:...); "
                         "if it is a file, check the path")
    if not shutil.which("docker"):
        raise ImageError("docker CLI not found; save the image yourself with `docker save -o image.tar NAME` "
                         "and pass the tar")
    try:
        proc = subprocess.run(["docker", "save", "-o", str(destination), reference],
                              capture_output=True, text=True, timeout=SAVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ImageError(f"docker save timed out after {SAVE_TIMEOUT}s")
    if proc.returncode != 0:
        message = (proc.stderr.strip().splitlines() or ["unknown error"])[-1][:300]
        raise ImageError(f"docker save failed: {message}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="docker save tar, extracted image dir, or image reference")
    parser.add_argument("--out", help="work directory for extracted app files (default: new temp dir)")
    parser.add_argument("--no-extract", action="store_true", help="do not copy any file out of the image")
    args = parser.parse_args(argv[1:])
    target = Path(args.target).expanduser()
    workdir = None
    if not args.no_extract:
        workdir = empty_workdir(args.out)
    temp_tar = None
    try:
        saved = False
        if target.is_dir():
            store = DirStore(target)
        elif target.is_file():
            store = TarStore(target)
        else:
            temp_tar = Path(tempfile.mkdtemp(prefix="code-teardown-save-")) / "image.tar"
            save_image(args.target, temp_tar)
            store, saved = TarStore(temp_tar), True
        report = inspect(store, args.target, workdir, saved)
    except (ImageError, tarfile.TarError, json.JSONDecodeError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    finally:
        if temp_tar:
            shutil.rmtree(temp_tar.parent, ignore_errors=True)
    if workdir:
        (workdir / "docker-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
        report["workdir"] = str(workdir)
    dump(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
