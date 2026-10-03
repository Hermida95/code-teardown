#!/usr/bin/env python3
"""Step 8 of the pipeline: validate findings and render the self-contained HTML report.

The judgment (findings.json) is written by the model; this script is the guard
rail. It refuses to render anything that breaks the evidence rules:

  * every finding, architecture claim and learning note carries evidence;
  * every file:line reference exists in the analyzed tree (and its optional
    quote really appears there); layer/history/config/bytecode references exist
    in the inspection output;
  * confidence never exceeds what the extraction method supports;
  * no secret-looking literal ends up in the report.

All text is HTML-escaped. The page loads no external resources and ships a
Content-Security-Policy whose only script allowance is a hash of its own script.

Usage: render_report.py FINDINGS.json --out report.html [--force] [--root DIR ...]
                        [--docker-report FILE] [--extraction FILE] [--no-verify]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"code-teardown needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import base64
import datetime
import hashlib
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import TOKEN_VALUE, checked_output_path, safe_join  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "report-template.html"
AXES = ["separation_of_concerns", "error_handling", "security", "testability", "coupling", "performance"]
VERDICTS = ["good", "improvable", "bad", "depends"]
CONFIDENCE = ["high", "medium", "low"]
OVERALL_CONFIDENCE = CONFIDENCE + ["very_low"]
WEIGHTS = ["high", "medium", "low"]
CONTEXTS = ["production_service", "automation_script", "library", "learning", "other"]
LANGUAGES = ["en", "es"]
SEVERITY = {"bad": 3, "improvable": 2, "depends": 1, "good": 0}
RANK = {"high": 3, "medium": 2, "low": 1, "very_low": 0}
MAX_FILE_BYTES = 20_000_000

L = {
    "en": {
        "summary": "Summary", "confidence": "Confidence", "context": "Usage context", "architecture": "Architecture",
        "inventory": "Inventory", "weighting": "Weighting by context", "axes": "Evaluation by axis",
        "learning": "Learning notes", "limits": "Method and limits", "top": "Findings that matter most here",
        "patterns": "Patterns worth copying", "anti": "Anti-patterns to avoid", "why": "Why it matters",
        "fix": "Better approach", "depends_on": "Depends on", "recommendation": "Recommendation",
        "evidence": "Evidence", "context_note": "In this context", "not_assessed": "Not assessed",
        "system": "System", "components": "Components", "key_functions": "Key functions", "modules": "Modules",
        "verification": "Evidence check", "verified": "{n} of {m} evidence references were checked against the analyzed files: all resolved.",
        "unverifiable": "{k} reference(s) are not machine-checkable (commands or free text).",
        "unverified_banner": "Evidence was NOT machine-verified for this report. Treat every citation as a claim to check yourself.",
        "assumed": "Context was not confirmed by the user.", "asked": "Confirmed with the user.",
        "good": "Good", "improvable": "Improvable", "bad": "Bad", "depends": "Depends",
        "high": "high", "medium": "medium", "low": "low", "very_low": "very low", "weight": "weight",
        "confidence_label": "confidence", "all_findings": "Filter findings", "theme_auto": "Theme: auto",
        "theme_light": "Theme: light", "theme_dark": "Theme: dark", "nav": "Sections", "artifact": "Artifact",
        "generated": "Generated", "degradations": "Degradations", "label": "Item", "value": "Value",
        "axis": "Axis", "counts": "Findings", "static": "Static analysis only: the artifact and any decompiled code were never executed.",
        "license": "Use this kind of analysis for learning, on your own software, open-source projects, or with the owner's permission. "
                   "It is not a way around licences or EULAs.",
        "axis_names": {"separation_of_concerns": "Separation of concerns", "error_handling": "Error handling",
                       "security": "Security", "testability": "Testability", "coupling": "Coupling",
                       "performance": "Performance"},
        "context_names": {"production_service": "Production service", "automation_script": "Automation script",
                          "library": "Library", "learning": "Learning", "other": "Other"},
    },
    "es": {
        "summary": "Resumen", "confidence": "Confianza", "context": "Contexto de uso", "architecture": "Arquitectura",
        "inventory": "Inventario", "weighting": "Ponderación por contexto", "axes": "Evaluación por ejes",
        "learning": "Notas de aprendizaje", "limits": "Método y límites", "top": "Hallazgos que más importan aquí",
        "patterns": "Patrones que merece la pena copiar", "anti": "Anti-patrones a evitar", "why": "Por qué importa",
        "fix": "Mejor enfoque", "depends_on": "Depende de", "recommendation": "Recomendación",
        "evidence": "Evidencia", "context_note": "En este contexto", "not_assessed": "No evaluado",
        "system": "Sistema", "components": "Componentes", "key_functions": "Funciones clave", "modules": "Módulos",
        "verification": "Verificación de evidencias", "verified": "Se comprobaron {n} de {m} referencias de evidencia contra los archivos analizados: todas existen.",
        "unverifiable": "{k} referencia(s) no se pueden comprobar automáticamente (comandos o texto libre).",
        "unverified_banner": "Las evidencias NO se han verificado automáticamente en este informe. Trata cada cita como una afirmación que debes comprobar.",
        "assumed": "El contexto no se confirmó con el usuario.", "asked": "Confirmado con el usuario.",
        "good": "Bien", "improvable": "Mejorable", "bad": "Mal", "depends": "Depende",
        "high": "alta", "medium": "media", "low": "baja", "very_low": "muy baja", "weight": "peso",
        "confidence_label": "confianza", "all_findings": "Filtrar hallazgos", "theme_auto": "Tema: auto",
        "theme_light": "Tema: claro", "theme_dark": "Tema: oscuro", "nav": "Secciones", "artifact": "Artefacto",
        "generated": "Generado", "degradations": "Degradaciones", "label": "Elemento", "value": "Valor",
        "axis": "Eje", "counts": "Hallazgos", "static": "Solo análisis estático: el artefacto y el código descompilado nunca se ejecutaron.",
        "license": "Usa este tipo de análisis para aprender, con tu propio software, proyectos de código abierto o con permiso del titular. "
                   "No sirve para saltarse licencias ni EULAs.",
        "axis_names": {"separation_of_concerns": "Separación de responsabilidades", "error_handling": "Manejo de errores",
                       "security": "Seguridad", "testability": "Testabilidad", "coupling": "Acoplamiento",
                       "performance": "Rendimiento"},
        "context_names": {"production_service": "Servicio en producción", "automation_script": "Script de automatización",
                          "library": "Librería", "learning": "Aprendizaje", "other": "Otro"},
    },
}
SYMBOL = {"good": "✔", "improvable": "▲", "bad": "✖", "depends": "◆"}

# Numbers are limited to 9 digits: longer ones are never real line or layer numbers, and
# huge integer strings make int() raise.
FILE_REF = re.compile(r"^(?P<path>[^\s:][^:]{0,1000}?):(?P<start>\d{1,9})(?:-(?P<end>\d{1,9}))?$")
LAYER_REF = re.compile(r"^layer (?P<n>\d{1,9})(?:: .+)?$")
HISTORY_REF = re.compile(r"^history\[(?P<n>\d{1,9})\]$")
CONFIG_REF = re.compile(r"^config\.(?P<key>[A-Za-z_]+)$")
BYTECODE_REF = re.compile(r"^(?P<file>.{1,1000}\.pyc) :: (?P<qual>.{1,500}?)(?: \(line (?P<line>\d{1,9})\))?$")
COMMAND_REF = re.compile(r"^cmd: .+")


class Checker:
    """Collects validation errors and resolves evidence against the real inputs."""

    def __init__(self, roots: list[Path], docker: dict | None, extraction: dict | None, verify: bool):
        self.errors: list[str] = []
        self.roots = [r.resolve() for r in roots]
        self.docker, self.extraction, self.verify = docker, extraction, verify
        self.cache: dict[Path, list[str]] = {}
        self.checked = 0
        self.unverifiable = 0
        self.total = 0

    def err(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    # -- helpers
    def lines_of(self, rel: str) -> list[str] | None:
        for root in self.roots:
            target = safe_join(root, rel)
            if target is None:
                continue
            if target.is_file() and target.stat().st_size <= MAX_FILE_BYTES:
                if target not in self.cache:
                    self.cache[target] = target.read_text(encoding="utf-8", errors="replace").splitlines()
                return self.cache[target]
        return None

    def check_evidence(self, where: str, item) -> dict | None:
        """Validate one evidence item; return it normalized ({'ref', 'quote', 'verified'})."""
        if isinstance(item, str):
            item = {"ref": item}
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str) or not item["ref"].strip():
            self.err(where, "evidence must be a string or an object with a non-empty 'ref'")
            return None
        ref, quote = item["ref"].strip(), item.get("quote")
        if quote is not None and not isinstance(quote, str):
            self.err(where, "'quote' must be a string")
            return None
        self.total += 1
        verified = self._resolve(where, ref, quote)
        if verified is None:
            return None
        return {"ref": ref, "quote": quote, "verified": verified}

    def _resolve(self, where: str, ref: str, quote: str | None) -> bool | None:
        file_match, layer, hist = FILE_REF.match(ref), LAYER_REF.match(ref), HISTORY_REF.match(ref)
        cfg, code = CONFIG_REF.match(ref), BYTECODE_REF.match(ref)
        if file_match:
            return self._file_line(where, ref, file_match, quote)
        if layer or hist or cfg:
            if not self.docker:
                return self._unverifiable(where, ref, "needs --docker-report to be checked")
            if layer:
                count = len(self.docker.get("layers", []))
                if int(layer["n"]) >= count:
                    return self._fail(where, f"{ref}: the image has only {count} layer(s)")
            elif hist:
                count = len(self.docker.get("history", []))
                if int(hist["n"]) >= count:
                    return self._fail(where, f"{ref}: the image history has only {count} entrie(s)")
            else:
                known = {k.replace("_", "").lower() for k in self.docker.get("config", {})}
                known.add("workingdir")
                if cfg["key"].replace("_", "").lower() not in known:
                    return self._fail(where, f"{ref}: unknown config field")
            self.checked += 1
            return True
        if code:
            if not self.extraction:
                return self._unverifiable(where, ref, "needs --extraction to be checked")
            record = next((f for f in self.extraction.get("files", []) if f.get("path") == code["file"]), None)
            if record is None:
                return self._fail(where, f"{ref}: no such .pyc in the extraction output")
            fn = next((f for f in record.get("functions", []) if f.get("qualname") == code["qual"]), None)
            if fn is None:
                return self._fail(where, f"{ref}: no function or class '{code['qual']}' in that .pyc")
            if code["line"] and int(code["line"]) != fn.get("line"):
                return self._fail(where, f"{ref}: '{code['qual']}' is at source line {fn.get('line')}, not {code['line']}")
            self.checked += 1
            return True
        if COMMAND_REF.match(ref):
            self.unverifiable += 1
            return False
        return self._fail(where, f"unrecognized evidence reference '{ref}' "
                                 "(use path:line, layer N, history[i], config.Field, file.pyc :: name, or cmd: ...)")

    def _fail(self, where: str, message: str) -> None:
        self.err(where, message)
        return None

    def _unverifiable(self, where: str, ref: str, reason: str):
        if self.verify:
            return self._fail(where, f"{ref}: {reason}")
        self.unverifiable += 1
        return False

    def _file_line(self, where: str, ref: str, match, quote: str | None):
        start = int(match["start"])
        end = int(match["end"] or start)
        if start < 1 or end < start:
            return self._fail(where, f"{ref}: invalid line range")
        if not self.verify:
            self.unverifiable += 1
            return False
        if not self.roots:
            return self._fail(where, f"{ref}: no --root given to check file evidence against")
        lines = self.lines_of(match["path"])
        if lines is None:
            return self._fail(where, f"{ref}: file not found under the analyzed roots")
        if end > len(lines):
            return self._fail(where, f"{ref}: file has only {len(lines)} line(s)")
        if quote:
            wanted = " ".join(quote.split())
            window = " ".join(" ".join(lines[start - 1:end]).split())
            if wanted not in window:
                return self._fail(where, f"{ref}: the quoted text does not appear on those lines")
        self.checked += 1
        return True


def check_enum(c: Checker, where: str, value, allowed: list[str]) -> bool:
    if value not in allowed:
        c.err(where, f"must be one of {allowed}, got {value!r}")
        return False
    return True


def need_text(c: Checker, where: str, value) -> bool:
    if not isinstance(value, str) or not value.strip():
        c.err(where, "required non-empty text")
        return False
    return True


def evidence_list(c: Checker, where: str, items) -> list[dict]:
    if not isinstance(items, list) or not items:
        c.err(where, "at least one evidence item is required (no claim without evidence)")
        return []
    return [e for i, item in enumerate(items) if (e := c.check_evidence(f"{where}[{i}]", item))]


def scan_secrets(c: Checker, node, where: str = "findings") -> None:
    if isinstance(node, str):
        if TOKEN_VALUE.search(node):
            c.err(where, "contains something that looks like a secret value; redact it and cite only the name and location")
    elif isinstance(node, dict):
        for key, value in node.items():
            scan_secrets(c, value, f"{where}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            scan_secrets(c, value, f"{where}[{i}]")


def validate(data: dict, c: Checker) -> dict:
    """Validate findings.json, normalizing evidence in place. Errors go to c.errors."""
    if not isinstance(data, dict):
        c.err("root", "findings must be a JSON object")
        return data
    scan_secrets(c, data)
    meta = data.get("meta") or {}
    need_text(c, "meta.title", meta.get("title"))
    check_enum(c, "meta.language", meta.get("language", "en"), LANGUAGES)
    artifact = meta.get("artifact") or {}
    need_text(c, "meta.artifact.path", artifact.get("path"))
    need_text(c, "meta.artifact.kind", artifact.get("kind"))
    context = meta.get("context") or {}
    check_enum(c, "meta.context.id", context.get("id"), CONTEXTS)
    need_text(c, "meta.context.label", context.get("label"))
    if not context.get("asked"):
        need_text(c, "meta.context.assumed_reason", context.get("assumed_reason"))

    # Confidence ceiling imposed by how the code was recovered.
    cap = "high"
    if c.extraction:
        cap = {"medium": "medium", "low": "low", "none": "none"}.get(c.extraction.get("weakest_confidence"), "high")
        if c.extraction.get("obfuscation_suspected") and cap in ("high", "medium"):
            cap = "low"
    conf = data.get("confidence") or {}
    if check_enum(c, "confidence.overall", conf.get("overall"), OVERALL_CONFIDENCE) and cap != "high":
        ceiling = "very_low" if cap == "none" else cap
        if RANK[conf["overall"]] > RANK[ceiling]:
            c.err("confidence.overall", f"'{conf['overall']}' exceeds the ceiling '{ceiling}' set by the extraction method")
    need_text(c, "confidence.explanation", conf.get("explanation"))
    if not isinstance(conf.get("degradations", []), list):
        c.err("confidence.degradations", "must be a list")

    summary = data.get("summary") or {}
    need_text(c, "summary.headline", summary.get("headline"))
    need_text(c, "summary.verdict", summary.get("verdict"))

    arch = data.get("architecture") or {}
    need_text(c, "architecture.system", arch.get("system"))
    for i, comp in enumerate(arch.get("components") or []):
        need_text(c, f"architecture.components[{i}].name", comp.get("name"))
        need_text(c, f"architecture.components[{i}].role", comp.get("role"))
        comp["evidence"] = evidence_list(c, f"architecture.components[{i}].evidence", comp.get("evidence"))
    for i, fn in enumerate(arch.get("key_functions") or []):
        need_text(c, f"architecture.key_functions[{i}].name", fn.get("name"))
        need_text(c, f"architecture.key_functions[{i}].role", fn.get("role"))
        fn["evidence"] = evidence_list(c, f"architecture.key_functions[{i}].evidence", fn.get("evidence"))
    if not arch.get("components"):
        c.err("architecture.components", "at least one component is required")

    weights = data.get("context_weights") or {}
    need_text(c, "context_weights.rationale", weights.get("rationale"))

    seen_axes, finding_ids = set(), set()
    axes = data.get("axes")
    if not isinstance(axes, list):
        c.err("axes", "must be a list with one entry per axis")
        axes = []
    for i, axis in enumerate(axes):
        where = f"axes[{i}]"
        if not check_enum(c, f"{where}.id", axis.get("id"), AXES):
            continue
        if axis["id"] in seen_axes:
            c.err(where, f"axis '{axis['id']}' appears twice")
        seen_axes.add(axis["id"])
        if axis.get("status", "assessed") == "not_assessed":
            need_text(c, f"{where}.reason", axis.get("reason"))
            continue
        check_enum(c, f"context_weights.{axis['id']}", (weights.get(axis["id"])), WEIGHTS)
        need_text(c, f"{where}.summary", axis.get("summary"))
        findings = axis.get("findings")
        if not isinstance(findings, list) or not findings:
            c.err(f"{where}.findings", "an assessed axis needs at least one finding (or mark it not_assessed with a reason)")
            continue
        for j, finding in enumerate(findings):
            fw = f"{where}.findings[{j}]"
            fid = finding.get("id")
            if not isinstance(fid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fid or ""):
                c.err(f"{fw}.id", "required slug (lowercase letters, digits, hyphens)")
            elif fid in finding_ids:
                c.err(f"{fw}.id", f"duplicate id '{fid}'")
            finding_ids.add(fid)
            need_text(c, f"{fw}.title", finding.get("title"))
            need_text(c, f"{fw}.detail", finding.get("detail"))
            check_enum(c, f"{fw}.verdict", finding.get("verdict"), VERDICTS)
            if check_enum(c, f"{fw}.confidence", finding.get("confidence"), CONFIDENCE):
                if cap == "none":
                    c.err(fw, "no quality findings are allowed when the extraction recovered no structure; "
                              "mark the axis not_assessed instead")
                elif cap != "high" and RANK[finding["confidence"]] > RANK[cap]:
                    c.err(f"{fw}.confidence", f"'{finding['confidence']}' exceeds the ceiling '{cap}' for this extraction method")
            if finding.get("verdict") == "depends":
                need_text(c, f"{fw}.depends_on", finding.get("depends_on"))
                need_text(c, f"{fw}.context_note", finding.get("context_note"))
            finding["evidence"] = evidence_list(c, f"{fw}.evidence", finding.get("evidence"))
    for axis_id in AXES:
        if axis_id not in seen_axes:
            c.err("axes", f"axis '{axis_id}' is missing (assess it or mark it not_assessed with a reason)")
    for key in ("patterns_to_copy", "anti_patterns"):
        for i, note in enumerate((data.get("learning") or {}).get(key) or []):
            need_text(c, f"learning.{key}[{i}].title", note.get("title"))
            need_text(c, f"learning.{key}[{i}].why", note.get("why"))
            note["evidence"] = evidence_list(c, f"learning.{key}[{i}].evidence", note.get("evidence"))
    if not isinstance(data.get("limits"), list):
        c.err("limits", "must be a list (it may be empty)")
    if c.extraction and c.extraction.get("obfuscation_suspected") and not data.get("limits"):
        c.err("limits", "obfuscation was suspected, so the limits section must say how the analysis was constrained")
    return data


# --- rendering --------------------------------------------------------------------------

def e(value) -> str:
    return html.escape(str(value), quote=True)


def render_evidence(items: list[dict], t: dict) -> str:
    out = []
    for item in items:
        cls = "ev" + ("" if item["verified"] else " unverified")
        quote = f"<pre>{e(item['quote'])}</pre>" if item.get("quote") else ""
        out.append(f'<li class="{cls}"><code class="ref">{e(item["ref"])}</code>{quote}</li>')
    return f'<ul class="evidence" aria-label="{e(t["evidence"])}">' + "".join(out) + "</ul>"


def badge(kind: str, text: str, symbol: str = "") -> str:
    sym = f'<span aria-hidden="true">{symbol}</span>' if symbol else ""
    return f'<span class="badge {kind}">{sym}{e(text)}</span>'


def render_finding(f: dict, t: dict) -> str:
    verdict = f["verdict"]
    parts = [f'<article class="card finding" id="f-{e(f["id"])}" data-verdict="{verdict}">',
             "<header>", f"<h3>{e(f['title'])}</h3>",
             badge(f"v-{verdict}", t[verdict], SYMBOL[verdict]),
             badge("c-badge", f"{t['confidence_label']}: {t[f['confidence']]}"), "</header>",
             f"<p>{e(f['detail'])}</p>"]
    if verdict == "depends":
        parts.append(f'<div class="field"><b>{e(t["depends_on"])}</b>{e(f["depends_on"])}</div>')
        parts.append(f'<div class="field"><b>{e(t["context_note"])}</b>{e(f["context_note"])}</div>')
    elif f.get("context_note"):
        parts.append(f'<div class="field"><b>{e(t["context_note"])}</b>{e(f["context_note"])}</div>')
    if f.get("why_it_matters"):
        parts.append(f'<div class="field"><b>{e(t["why"])}</b>{e(f["why_it_matters"])}</div>')
    if f.get("recommendation"):
        parts.append(f'<div class="field"><b>{e(t["recommendation"])}</b>{e(f["recommendation"])}</div>')
    parts.append(render_evidence(f["evidence"], t))
    parts.append("</article>")
    return "".join(parts)


def render(data: dict, c: Checker, template: str) -> str:
    meta, conf, ctx = data["meta"], data["confidence"], data["meta"]["context"]
    lang = meta.get("language", "en")
    t = L[lang]
    weights = data["context_weights"]
    axes = {a["id"]: a for a in data["axes"]}
    findings = [(a["id"], f) for a in data["axes"] if a.get("status", "assessed") != "not_assessed"
                for f in a["findings"]]

    nav_items = [("summary", t["summary"]), ("architecture", t["architecture"])]
    if data.get("inventory"):
        nav_items.append(("inventory", t["inventory"]))
    nav_items += [("weighting", t["weighting"]), ("axes", t["axes"]), ("learning", t["learning"]), ("limits", t["limits"])]
    nav = "\n".join(f'<li><a href="#{i}">{e(label)}</a></li>' for i, label in nav_items)

    body: list[str] = []
    # Summary
    body.append(f'<section id="summary"><h1>{e(meta["title"])}</h1>')
    generated = meta.get("generated") or datetime.date.today().isoformat()
    body.append(f'<div class="meta"><span>{e(t["artifact"])}: <code>{e(meta["artifact"]["path"])}</code> '
                f'({e(meta["artifact"]["kind"])})</span><span>{e(t["generated"])}: {e(generated)}</span></div>')
    if not c.verify:
        body.append(f'<div class="banner" role="alert">{e(t["unverified_banner"])}</div>')
    body.append(f'<div class="card"><h3>{e(data["summary"]["headline"])}</h3><p>{e(data["summary"]["verdict"])}</p></div>')
    body.append(f'<div class="card"><h3>{e(t["confidence"])}: {badge("c-badge", t[conf["overall"]])}</h3>'
                f'<p>{e(conf["explanation"])}</p>')
    if conf.get("degradations"):
        body.append(f'<div class="field"><b>{e(t["degradations"])}</b><ul>' +
                    "".join(f"<li>{e(d)}</li>" for d in conf["degradations"]) + "</ul></div>")
    body.append("</div>")
    ctx_line = t["asked"] if ctx.get("asked") else f'{t["assumed"]} {ctx.get("assumed_reason", "")}'
    body.append(f'<div class="card"><h3>{e(t["context"])}: {e(ctx["label"])} '
                f'<span class="badge c-badge">{e(t["context_names"][ctx["id"]])}</span></h3><p class="muted">{e(ctx_line)}</p></div>')
    status = t["verified"].format(n=c.checked, m=c.total) if c.verify else ""
    if c.verify and c.unverifiable:
        status += " " + t["unverifiable"].format(k=c.unverifiable)
    if status:
        body.append(f'<div class="card"><h3>{e(t["verification"])}</h3><p>{e(status)}</p></div>')

    ranked = sorted(((SEVERITY[f["verdict"]] * {"high": 3, "medium": 2, "low": 1}[weights[axis]],
                      RANK[f["confidence"]], f) for axis, f in findings if f["verdict"] != "good"),
                    key=lambda x: (-x[0], -x[1], x[2]["id"]))[:5]
    if ranked:
        body.append(f'<h3>{e(t["top"])}</h3><ol>' + "".join(
            f'<li><a href="#f-{e(f["id"])}">{e(f["title"])}</a> {badge("v-" + f["verdict"], t[f["verdict"]], SYMBOL[f["verdict"]])}</li>'
            for _, _, f in ranked) + "</ol>")
    body.append("</section>")

    # Architecture
    arch = data["architecture"]
    body.append(f'<section id="architecture"><h2>{e(t["architecture"])}</h2>'
                f'<div class="card"><h3>{e(t["system"])}</h3><p>{e(arch["system"])}</p></div>'
                f'<h3>{e(t["components"])}</h3><div class="grid">')
    for comp in arch["components"]:
        modules = "".join(f"<li><code>{e(m)}</code></li>" for m in comp.get("modules") or [])
        body.append(f'<div class="card"><h3>{e(comp["name"])}</h3><p>{e(comp["role"])}</p>'
                    + (f'<ul class="chips" aria-label="{e(t["modules"])}">{modules}</ul>' if modules else "")
                    + render_evidence(comp["evidence"], t) + "</div>")
    body.append("</div>")
    if arch.get("key_functions"):
        body.append(f'<h3>{e(t["key_functions"])}</h3>')
        for fn in arch["key_functions"]:
            body.append(f'<div class="card"><code>{e(fn["name"])}</code><p>{e(fn["role"])}</p>'
                        f'{render_evidence(fn["evidence"], t)}</div>')
    body.append("</section>")

    # Inventory
    if data.get("inventory"):
        rows = "".join(f"<tr><td>{e(i.get('label', ''))}</td><td>{e(i.get('value', ''))}</td></tr>" for i in data["inventory"])
        body.append(f'<section id="inventory"><h2>{e(t["inventory"])}</h2><table><thead><tr><th>{e(t["label"])}</th>'
                    f'<th>{e(t["value"])}</th></tr></thead><tbody>{rows}</tbody></table></section>')

    # Weighting + overview
    rows = []
    for axis_id in AXES:
        a = axes[axis_id]
        if a.get("status", "assessed") == "not_assessed":
            rows.append(f'<tr><td>{e(t["axis_names"][axis_id])}</td><td colspan="2" class="muted">{e(t["not_assessed"])}: {e(a["reason"])}</td></tr>')
            continue
        counts = {v: sum(1 for f in a["findings"] if f["verdict"] == v) for v in VERDICTS}
        chips = " ".join(badge(f"v-{v}", f"{t[v]} {n}", SYMBOL[v]) for v, n in counts.items() if n)
        wlabel = t["weight"] + ": " + t[weights[axis_id]]
        rows.append(f'<tr><td><a href="#axis-{axis_id}">{e(t["axis_names"][axis_id])}</a></td>'
                    f'<td>{badge("w-badge", wlabel)}</td><td>{chips}</td></tr>')
    body.append(f'<section id="weighting"><h2>{e(t["weighting"])}</h2><p>{e(weights["rationale"])}</p>'
                f'<table><thead><tr><th>{e(t["axis"])}</th><th>{e(t["weight"])}</th><th>{e(t["counts"])}</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></section>')

    # Axes
    body.append(f'<section id="axes"><h2>{e(t["axes"])}</h2><div class="filters" role="group" aria-label="{e(t["all_findings"])}">'
                + "".join(f'<button type="button" data-filter="{v}" aria-pressed="false">{SYMBOL[v]} {e(t[v])}</button>' for v in VERDICTS)
                + "</div>")
    for axis_id in AXES:
        a = axes[axis_id]
        if a.get("status", "assessed") == "not_assessed":
            continue
        wlabel = t["weight"] + ": " + t[weights[axis_id]]
        body.append(f'<div id="axis-{axis_id}"><h3>{e(t["axis_names"][axis_id])} '
                    f'{badge("w-badge", wlabel)}</h3><p>{e(a["summary"])}</p>')
        body.extend(render_finding(f, t) for f in a["findings"])
        body.append("</div>")
    body.append("</section>")

    # Learning
    learn = data.get("learning") or {}
    body.append(f'<section id="learning"><h2>{e(t["learning"])}</h2>')
    for key, title in (("patterns_to_copy", t["patterns"]), ("anti_patterns", t["anti"])):
        notes = learn.get(key) or []
        if not notes:
            continue
        body.append(f"<h3>{e(title)}</h3>")
        for n in notes:
            fix = f'<div class="field"><b>{e(t["fix"])}</b>{e(n["fix"])}</div>' if n.get("fix") else ""
            body.append(f'<div class="card"><h3>{e(n["title"])}</h3><p>{e(n["why"])}</p>{fix}{render_evidence(n["evidence"], t)}</div>')
    body.append("</section>")

    # Limits
    limits = "".join(f"<li>{e(x)}</li>" for x in data["limits"])
    body.append(f'<section id="limits"><h2>{e(t["limits"])}</h2><div class="card"><p>{e(t["static"])}</p>'
                f'<ul>{limits}</ul></div></section>')

    template_script = re.search(r"<script>(.*?)</script>", template, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(template_script.encode("utf-8")).digest()).decode()
    csp = (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{digest}'; "
           "img-src data:; base-uri 'none'; form-action 'none'")
    replacements = {
        "@@LANG@@": lang, "@@TITLE@@": e(meta["title"]), "@@CSP@@": csp, "@@NAV@@": nav,
        "@@NAV_LABEL@@": e(t["nav"]), "@@BODY@@": "\n".join(body),
        "@@THEME_AUTO@@": e(t["theme_auto"]), "@@THEME_LIGHT@@": e(t["theme_light"]), "@@THEME_DARK@@": e(t["theme_dark"]),
        "@@FOOTER@@": f'<p>{e(t["license"])}</p><p>code-teardown · MIT</p>',
    }
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], template)


def load_json(path: str | None, what: str) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read {what} {path}: {exc}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("findings")
    parser.add_argument("--out", required=True, help="the .html file to write")
    parser.add_argument("--force", action="store_true", help="allow --out to overwrite an existing file")
    parser.add_argument("--root", action="append", default=[], help="directory the file:line evidence is relative to (repeatable)")
    parser.add_argument("--docker-report")
    parser.add_argument("--extraction")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip file evidence checks (the report then carries a visible banner)")
    args = parser.parse_args(argv[1:])

    out_path = checked_output_path(args.out, ".html", args.force)
    data = load_json(args.findings, "findings")
    checker = Checker([Path(r) for r in args.root], load_json(args.docker_report, "docker report"),
                      load_json(args.extraction, "extraction"), verify=not args.no_verify)
    try:
        validate(data, checker)
    except (AttributeError, TypeError, KeyError) as exc:
        checker.err("structure", f"findings has an unexpected shape ({type(exc).__name__}: {exc}); "
                                 "see references/report-schema.md")
    if checker.errors:
        print(f"{len(checker.errors)} problem(s) in {args.findings}; no report written:", file=sys.stderr)
        for message in checker.errors:
            print(f"  - {message}", file=sys.stderr)
        return 1
    template = TEMPLATE.read_text(encoding="utf-8")
    out_path.write_text(render(data, checker, template), encoding="utf-8")
    print(json.dumps({"output": args.out, "findings": sum(len(a.get("findings", [])) for a in data["axes"]),
                      "evidence_checked": checker.checked, "evidence_total": checker.total,
                      "evidence_unverifiable": checker.unverifiable, "verified": not args.no_verify}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
