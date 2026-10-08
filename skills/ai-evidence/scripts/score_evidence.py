#!/usr/bin/env python3
"""Step 3 of the image pipeline: combine evidence into scores and render the report.

Input: the evidence.json written by analyze_image.py, plus optionally a visual.json with what
the model saw when it looked at the image. Each evidence item has

    score   0..10   0 = this points to "not AI", 10 = this points to "AI"
    weight  0..1    how much the item can be trusted (0 = informative only, >=0.9 = conclusive)
    claim   "generated" (made by a model) or "ai_edited" (a real image or text changed by a model)

The two claims are scored separately. The score is a weighted mean of the items, with two rules
on top: a conclusive item decides the score, and two conclusive items that disagree make the
result "conflict" instead of averaging them. The confidence comes from how much evidence exists.

Usage: score_evidence.py evidence.json [--visual visual.json] [--pixels pixels.json] --out report.html
                         [--json-out result.json] [--lang es|en] [--force]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import datetime
import html
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import TOKEN_VALUE, checked_output_path, clean_quote, dump  # noqa: E402

CONCLUSIVE = 0.9
CLAIMS = ["generated", "ai_edited"]
LANGUAGES = ["es", "en"]

# A visual impression is the least reliable kind of evidence, so the weight is capped by kind
# no matter what the model wrote. A recognisable vendor watermark is the one strong exception.
VISUAL_CAPS = {"known_watermark": 0.6, "anatomy_text_errors": 0.35, "physics_lighting": 0.3,
               "natural_cues": 0.3, "texture_style": 0.25, "composition": 0.2, "other": 0.2,
               # what the model notices when it reads a text
               "fabricated_references": 0.35, "voice_and_specificity": 0.3, "generic_content": 0.25, "text_other": 0.2,
               # what the model notices when it reads code
               "hallucinated_apis": 0.35, "human_idiosyncrasy": 0.3, "generic_scaffolding": 0.25, "code_other": 0.2}

# Weak signals of one kind are correlated, so adding them up must not buy confidence. Per group and claim,
# the total weight is scaled down to the cap. Stock phrases, dashes and sentence rhythm are all "style".
GROUP_CAPS = {"style": 0.5, "visual": 1.0, "pixels": 0.5}

# Pixel measurements come from heuristics that are not calibrated on real images, so whatever
# analyze_pixels.py writes, no item counts for more than this.
PIXEL_CAP = 0.25

# (upper bound exclusive, band id)
BANDS = [(2.0, "none"), (4.0, "low"), (6.0, "mixed"), (8.0, "strong"), (10.01, "very_strong")]

UI = {
    "es": {
        "title": "Informe de evidencias de IA", "file": "Archivo", "format": "Formato", "size": "Tamaño", "generated_on": "Generado",
        "generated": "¿Generada por IA?", "ai_edited": "¿Editada con IA?",
        "generated_text": "¿Escrito por IA?", "ai_edited_text": "¿Mezclado o pulido con IA?", "text_measurements": "Medidas del texto",
        "generated_code": "¿Escrito en gran parte por IA?", "ai_edited_code": "¿Hecho con ayuda de IA?", "code_measurements": "Medidas del código",
        "files_word": "archivos", "lines_word": "líneas",
        "disclaimer_code": "Usar IA para programar es normal y legítimo. Este informe solo dice qué rastros hay: no dice si el código es bueno ni si el trabajo es honrado (para la calidad, usa code-teardown), y no sirve para acusar a nadie.",
        "scaled": "peso reducido de {a} a {b}: tope del grupo «{g}»", "words": "palabras",
        "disclaimer_text": "En texto es todavía menos fiable: quien escribe en un segundo idioma, los registros formales y los textos muy editados se marcan mucho más que la escritura nativa e informal. No uses este informe para acusar a nadie de haber usado IA, por ejemplo en un trabajo escolar o académico.",
        "scale": "0 = no es IA · 10 = es IA", "lo": "no es IA", "hi": "es IA", "confidence": "Confianza",
        "conf": {"high": "alta", "medium": "media", "low": "baja", "very_low": "muy baja"},
        "band": {"none": "Sin indicios de IA", "low": "Pocos indicios", "mixed": "Indicios mixtos",
                 "strong": "Indicios claros", "very_strong": "Muy probablemente IA", "insufficient": "Sin pruebas suficientes",
                 "conflict": "Pruebas contradictorias"},
        "reading": {
            "insufficient": "Hay muy poca evidencia con peso. La nota que se ve es solo orientativa y no permite concluir nada.",
            "conflict": "Hay pruebas de peso alto que apuntan en sentidos opuestos. No se promedian: hay que revisarlo a mano.",
            "conclusive": "Una prueba de peso alto decide la nota. Sigue siendo una declaración en el archivo que alguien pudo falsificar o dejar sin querer.",
            "none": "Las pruebas con peso apuntan a un contenido no generado por IA. No lo descarta: faltan comprobaciones.",
            "low": "Algo apunta a IA, pero con poco peso. Más cerca de «no» que de «sí».",
            "mixed": "Las pruebas se reparten en ambos sentidos. No se puede inclinar la balanza.",
            "strong": "Varias pruebas apuntan a IA con peso razonable. Es un indicio fuerte, no una prueba.",
            "very_strong": "Las pruebas con peso apuntan con claridad a IA.",
        },
        "evidence": "Evidencias", "no_evidence": "No hay evidencias para esta pregunta.",
        "weight": "Peso", "share": "Cuota", "where": "Dónde", "score": "Nota",
        "src": {"metadata": "metadatos", "structure": "estructura", "filename": "nombre", "visual": "visual", "pixels": "píxeles", "residue": "restos de IA", "style": "estilo"},
        "measurements": "Medidas de píxeles", "info": "informativa (no puntúa)", "not_checked": "Qué no se ha podido comprobar", "limits": "Límites",
        "method": "Cómo se calcula",
        "method_text": "Cada evidencia tiene una nota de 0 (apunta a «no es IA») a 10 (apunta a «es IA») y un peso de 0 a 1 que dice cuánto se puede fiar de ella. "
                       "La nota final es la media ponderada por peso. Una evidencia de peso 0,9 o más decide por sí sola, y si dos de ellas se contradicen no se promedian: se marca como contradictoria. "
                       "La confianza depende de cuánto peso total hay, no de la nota.",
        "disclaimer": "Esta herramienta no acusa a nadie: sirve para ver, comprobar y aprender. Esto es un conjunto de indicios, no una prueba ni un veredicto. Ningún análisis automático distingue con certeza un contenido generado de uno real, "
                      "y los metadatos pueden faltar, copiarse o falsificarse. No uses este informe para acusar a nadie.",
        "visual_note": "Observación visual del modelo", "clamped": "peso limitado de {a} a {b}",
    },
    "en": {
        "title": "AI evidence report", "file": "File", "format": "Format", "size": "Size", "generated_on": "Generated",
        "generated": "Generated by AI?", "ai_edited": "Edited with AI?",
        "generated_text": "Written by AI?", "ai_edited_text": "Mixed with or polished by AI?", "text_measurements": "Text measurements",
        "generated_code": "Largely written by AI?", "ai_edited_code": "Built with AI assistance?", "code_measurements": "Code measurements",
        "files_word": "files", "lines_word": "lines",
        "disclaimer_code": "Using AI to write code is normal and legitimate. This report only says which traces exist: it does not say whether the code is good or the work honest (for quality, use code-teardown), and it is not for accusing anyone.",
        "scaled": "weight reduced from {a} to {b}: cap of the \"{g}\" group", "words": "words",
        "disclaimer_text": "For text it is even less reliable: non-native writers, formal registers and heavily edited text are flagged far more often than native, informal writing. Do not use this report to accuse anyone of using AI, for example in schoolwork or academic work.",
        "scale": "0 = not AI · 10 = AI", "lo": "not AI", "hi": "AI", "confidence": "Confidence",
        "conf": {"high": "high", "medium": "medium", "low": "low", "very_low": "very low"},
        "band": {"none": "No signs of AI", "low": "Few signs", "mixed": "Mixed signs",
                 "strong": "Clear signs", "very_strong": "Very likely AI", "insufficient": "Not enough evidence",
                 "conflict": "Conflicting evidence"},
        "reading": {
            "insufficient": "There is very little weighty evidence. The score shown is only a hint and supports no conclusion.",
            "conflict": "Heavy evidence points in opposite directions. It is not averaged: review it by hand.",
            "conclusive": "One heavy item decides the score. It is still a statement inside the file that someone could have forged or left in by accident.",
            "none": "The weighty evidence points to content not made by AI. It does not rule it out: some checks were not possible.",
            "low": "Something points to AI, but with little weight. Closer to \"no\" than to \"yes\".",
            "mixed": "The evidence splits both ways. The balance cannot be tipped.",
            "strong": "Several items point to AI with reasonable weight. A strong indication, not proof.",
            "very_strong": "The weighty evidence clearly points to AI.",
        },
        "evidence": "Evidence", "no_evidence": "There is no evidence for this question.",
        "weight": "Weight", "share": "Share", "where": "Where", "score": "Score",
        "src": {"metadata": "metadata", "structure": "structure", "filename": "file name", "visual": "visual", "pixels": "pixels", "residue": "AI residue", "style": "style"},
        "measurements": "Pixel measurements", "info": "informative (not scored)", "not_checked": "What could not be checked", "limits": "Limits",
        "method": "How it is computed",
        "method_text": "Each piece of evidence has a score from 0 (points to \"not AI\") to 10 (points to \"AI\") and a weight from 0 to 1 saying how far it can be trusted. "
                       "The final score is the weight-weighted mean. An item with weight 0.9 or more decides on its own, and two that contradict each other are not averaged: the result is marked as conflicting. "
                       "Confidence depends on how much total weight exists, not on the score.",
        "disclaimer": "This tool does not accuse anyone: it is for seeing, checking and learning. This is a set of indications, not proof or a verdict. No automatic analysis tells generated content from real content with certainty, "
                      "and metadata can be missing, copied or forged. Do not use this report to accuse anyone.",
        "visual_note": "Visual observation by the model", "clamped": "weight capped from {a} to {b}",
    },
}


class InputError(SystemExit):
    pass


def fail(message: str) -> None:
    raise InputError(f"error: {message}")


def loc(value, lang: str) -> str:
    """Pick the language of a bilingual {en, es} text, or return a plain string as is."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or next(iter(value.values()), ""))
    return str(value)


def number(value, name: str, low: float, high: float, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{where}: {name} must be a number")
    if not low <= value <= high:
        fail(f"{where}: {name} must be between {low} and {high} (got {value})")
    return float(value)


def validate_item(item, where: str) -> dict:
    if not isinstance(item, dict):
        fail(f"{where}: must be an object")
    for key in ("id", "claim", "score", "weight", "title"):
        if key not in item:
            fail(f"{where}: missing '{key}'")
    if item["claim"] not in CLAIMS:
        fail(f"{where}: claim must be one of {CLAIMS}")
    clean = dict(item)
    clean["score"] = number(item["score"], "score", 0, 10, where)
    clean["weight"] = number(item["weight"], "weight", 0, 1, where)
    clean["id"] = str(item["id"])[:80]
    clean.setdefault("source", "metadata")
    clean.setdefault("where", "")
    clean.setdefault("detail", "")
    return clean


def load_json(path: str):
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {path}: {exc}")


def load_visual(path: str) -> list[dict]:
    """Validate the model's visual observations and cap their weight by kind."""
    data = load_json(path)
    observations = data.get("observations") if isinstance(data, dict) else data
    if not isinstance(observations, list):
        fail("visual file must be a list, or an object with an 'observations' list")
    items = []
    for index, obs in enumerate(observations):
        where = f"visual[{index}]"
        if not isinstance(obs, dict):
            fail(f"{where}: must be an object")
        kind = obs.get("kind", "other")
        if kind not in VISUAL_CAPS:
            fail(f"{where}: kind must be one of {sorted(VISUAL_CAPS)}")
        for key in ("title", "where", "score", "weight"):
            if key not in obs:
                fail(f"{where}: missing '{key}' (say what you saw, where, and how sure you are)")
        if not str(obs["where"]).strip():
            fail(f"{where}: 'where' must point to a place in the image")
        claim = obs.get("claim", "generated")
        item = validate_item({"id": obs.get("id", f"visual-{index + 1}"), "claim": claim, "score": obs["score"],
                              "weight": obs["weight"], "title": obs["title"], "detail": obs.get("detail", ""),
                              "where": obs["where"], "source": "visual"}, where)
        cap = VISUAL_CAPS[kind]
        if item["weight"] > cap:
            item["clamped_from"] = item["weight"]
            item["weight"] = cap
        item["kind"] = kind
        items.append(item)
    return items


def load_pixels(path: str) -> dict:
    """Validate the output of analyze_pixels.py and cap every weight at PIXEL_CAP."""
    doc = load_json(path)
    if not isinstance(doc, dict) or doc.get("tool") != "analyze_pixels" or not isinstance(doc.get("evidence"), list):
        fail("pixels file must be the JSON written by analyze_pixels.py")
    items = []
    for index, raw in enumerate(doc["evidence"]):
        item = validate_item(raw, f"pixels[{index}]")
        if not item["id"].startswith("px-"):
            fail(f"pixels[{index}]: ids from the pixel module must start with 'px-'")
        item["source"] = "pixels"
        if item["weight"] > PIXEL_CAP:
            item["clamped_from"] = item["weight"]
            item["weight"] = PIXEL_CAP
        items.append(item)
    return {"evidence": items, "measurements": doc.get("measurements", []), "limits": doc.get("limits", []),
            "coverage": doc.get("coverage", {})}


def apply_group_caps(items: list[dict]) -> None:
    """Scale down groups of weak, correlated items so that their sum cannot exceed the group's cap."""
    for source, cap in GROUP_CAPS.items():
        for claim in CLAIMS:
            group = [e for e in items if e["source"] == source and e["claim"] == claim and e["weight"] > 0]
            total = sum(e["weight"] for e in group)
            if total > cap:
                for e in group:
                    e["scaled_from"] = round(e["weight"], 4)
                    e["weight"] = e["weight"] * cap / total


def band_for(score: float) -> str:
    for upper, band in BANDS:
        if score < upper:
            return band
    return BANDS[-1][1]


def aggregate(evidence: list[dict], claim: str) -> dict:
    items = [e for e in evidence if e["claim"] == claim]
    scored = [e for e in items if e["weight"] > 0]
    total = sum(e["weight"] for e in scored)
    conclusive = [e for e in scored if e["weight"] >= CONCLUSIVE]
    result = {"claim": claim, "total_weight": round(total, 3), "evidence_count": len(items), "conflict": False}

    def mean(group: list[dict]) -> float:
        return sum(e["score"] * e["weight"] for e in group) / sum(e["weight"] for e in group)

    if not scored:
        result.update(score=None, band="insufficient", confidence="very_low", status="insufficient")
        return result
    high = [e for e in conclusive if e["score"] >= 6]
    low = [e for e in conclusive if e["score"] <= 4]
    if high and low:
        result.update(score=round(mean(scored), 1), band="conflict", confidence="very_low", status="conflict", conflict=True)
    elif conclusive:
        score = round(mean(conclusive), 1)
        result.update(score=score, band=band_for(score), confidence="high", status="conclusive")
    else:
        score = round(mean(scored), 1)
        confidence = "medium" if total >= 1.2 else "low" if total >= 0.4 else "very_low"
        status = "ok" if confidence != "very_low" else "insufficient"
        result.update(score=score, band="insufficient" if status == "insufficient" else band_for(score),
                      confidence=confidence, status=status)
    result["shares"] = {e["id"]: round(e["weight"] / total, 3) for e in scored}
    return result


# --- rendering -----------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f6f3;--card:#fff;--ink:#1c1b19;--muted:#6a675f;--line:#e3e0d8;--code:#f0eee8;
--none:#1a7f4b;--low:#5b8a1f;--mixed:#b7791f;--strong:#c2540a;--very_strong:#b42318;--insufficient:#6a675f;--conflict:#7a3fb0;
--none-bg:#e4f4ea;--low-bg:#eef4dc;--mixed-bg:#fbf0d8;--strong-bg:#fbe6d6;--very_strong-bg:#fbdedb;--insufficient-bg:#ecebe6;--conflict-bg:#efe3f8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe6;--muted:#a09d94;--line:#34332e;--code:#2a2925;
--none:#52c98a;--low:#9bcb55;--mixed:#e6b44c;--strong:#f08a4b;--very_strong:#f2766a;--insufficient:#a09d94;--conflict:#c79af0;
--none-bg:#17301f;--low-bg:#25301a;--mixed-bg:#33290f;--strong-bg:#35210f;--very_strong-bg:#361a17;--insufficient-bg:#272622;--conflict-bg:#2c1d38}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 16px 64px}h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.15rem;margin:36px 0 12px}
.meta{color:var(--muted);font-size:.9rem;display:flex;flex-wrap:wrap;gap:4px 18px;margin:0 0 24px;overflow-wrap:anywhere}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
.q{font-size:.95rem;color:var(--muted);margin:0}.big{font-size:2.6rem;font-weight:700;line-height:1.1;margin:6px 0 0}
.big small{font-size:1rem;font-weight:400;color:var(--muted)}.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:.9rem;margin:6px 0 10px}
.scale{position:relative;height:10px;border-radius:5px;margin:14px 0 4px;background:linear-gradient(90deg,var(--none) 0 20%,var(--low) 20% 40%,var(--mixed) 40% 60%,var(--strong) 60% 80%,var(--very_strong) 80% 100%);opacity:.85}
.scale i{position:absolute;top:-5px;width:4px;height:20px;border-radius:2px;background:var(--ink);box-shadow:0 0 0 2px var(--card);transform:translateX(-2px)}
.scale.off{background:var(--line)}.ticks{display:flex;justify-content:space-between;font-size:.75rem;color:var(--muted)}
.read{margin:12px 0 0;font-size:.95rem}.conf{color:var(--muted);font-size:.9rem;margin:8px 0 0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
td,th{padding:12px 14px;text-align:left;vertical-align:top;border-top:1px solid var(--line);font-size:.92rem}th{border-top:0;color:var(--muted);font-weight:600;font-size:.8rem}
.chip{display:inline-block;min-width:2.6em;text-align:center;padding:2px 8px;border-radius:8px;font-weight:700;font-variant-numeric:tabular-nums}
.t{font-weight:600}.d{color:var(--muted);margin-top:2px}.w{font-size:.8rem;color:var(--muted);white-space:nowrap}
.bar{display:block;height:6px;border-radius:3px;background:var(--line);margin-top:4px;min-width:60px}.bar b{display:block;height:100%;border-radius:3px;background:var(--ink);opacity:.55}
code{background:var(--code);padding:1px 6px;border-radius:4px;font-size:.85em;overflow-wrap:anywhere}.tag{font-size:.75rem;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:0 8px;margin-left:6px}
ul{padding-left:20px}.note{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;color:var(--muted);font-size:.92rem}
.scroll{overflow-x:auto}@media(max-width:560px){td,th{padding:10px 8px}.big{font-size:2.2rem}}
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def claim_label(u: dict, claim: str, modality: str) -> str:
    return u.get(f"{claim}_{modality}", u[claim]) if modality in ("text", "code") else u[claim]


def card(claim: str, agg: dict, lang: str, modality: str = "image") -> str:
    u = UI[lang]
    label = claim_label(u, claim, modality)
    band = agg["band"]
    off = band in ("insufficient", "conflict") or agg["score"] is None
    score = agg["score"]
    shown = "—" if score is None else f"{score:.1f}"
    marker = "" if score is None else f'<i style="left:{score * 10:.1f}%"></i>'
    reading_key = agg["status"] if agg["status"] in ("insufficient", "conflict", "conclusive") else band
    return (f'<section class="card" aria-label="{esc(label)}"><p class="q">{esc(label)}</p>'
            f'<p class="big" style="color:var(--{"insufficient" if off and band == "insufficient" else band})">{shown}<small> / 10</small></p>'
            f'<span class="badge" style="background:var(--{band}-bg);color:var(--{band})">{esc(u["band"][band])}</span>'
            f'<div class="scale{" off" if off else ""}" role="img" aria-label="{esc(u["scale"])}">{marker}</div>'
            f'<div class="ticks"><span>0 · {esc(u["lo"])}</span><span>{esc(u["hi"])} · 10</span></div>'
            f'<p class="read">{esc(u["reading"][reading_key])}</p>'
            f'<p class="conf">{esc(u["confidence"])}: <b>{esc(u["conf"][agg["confidence"]])}</b> · {esc(u["weight"])} Σ {agg["total_weight"]:.2f}</p></section>')


def evidence_table(claim: str, items: list[dict], agg: dict, lang: str) -> str:
    u = UI[lang]
    rows = []
    shares = agg.get("shares", {})
    for e in sorted(items, key=lambda x: (-x["weight"], x["id"])):
        informative = e["weight"] == 0
        band = band_for(e["score"])
        chip = (f'<span class="chip" style="background:var(--{band}-bg);color:var(--{band})">{e["score"]:g}</span>'
                if not informative else '<span class="chip" style="background:var(--insufficient-bg);color:var(--insufficient)">·</span>')
        quote = f'<div class="d"><code>{esc(clean_quote(str(e["quote"])))}</code></div>' if e.get("quote") else ""
        clamp = ""
        if "clamped_from" in e:
            note = u["clamped"].format(a=format(e["clamped_from"], "g"), b=format(e["weight"], "g"))
            clamp = f'<div class="w">{esc(note)}</div>'
        if "scaled_from" in e:
            note = u["scaled"].format(a=format(e["scaled_from"], ".2f"), b=format(e["weight"], ".2f"), g=e["source"])
            clamp += f'<div class="w">{esc(note)}</div>'
        weight_cell = (esc(u["info"]) if informative else
                       f'{e["weight"]:.2f}<span class="bar"><b style="width:{shares.get(e["id"], 0) * 100:.0f}%"></b></span>'
                       f'<div class="w">{shares.get(e["id"], 0) * 100:.0f}%</div>')
        rows.append(f'<tr><td>{chip}</td><td><div class="t">{esc(loc(e["title"], lang))}'
                    f'<span class="tag">{esc(u["src"].get(e["source"], e["source"]))}</span></div>'
                    f'<div class="d">{esc(loc(e["detail"], lang))}</div>{quote}'
                    f'<div class="w">{esc(u["where"])}: {esc(loc(e["where"], lang))}</div>{clamp}</td><td>{weight_cell}</td></tr>')
    if not rows:
        return f'<p class="note">{esc(u["no_evidence"])}</p>'
    return (f'<div class="scroll"><table><thead><tr><th>{esc(u["score"])}</th><th>{esc(u["evidence"])}</th>'
            f'<th>{esc(u["weight"])} · {esc(u["share"])}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render(report: dict, lang: str) -> str:
    u = UI[lang]
    f = report["file"]
    modality = f.get("modality", "image")
    cards = "".join(card(c, report["assessment"][c], lang, modality) for c in CLAIMS)
    sections = "".join(f'<h2>{esc(claim_label(u, c, modality))} · {esc(u["evidence"])}</h2>'
                       f'{evidence_table(c, [e for e in report["evidence"] if e["claim"] == c], report["assessment"][c], lang)}'
                       for c in CLAIMS)
    measured = "".join(f"<tr><td>{esc(loc(m['label'], lang))}</td><td><code>{esc(m['value'])}</code></td></tr>"
                       for m in report.get("measurements", []))
    measurements = (f'<h2>{esc(u[{"text": "text_measurements", "code": "code_measurements"}.get(modality, "measurements")])}</h2><div class="scroll"><table><tbody>{measured}</tbody></table></div>'
                    if measured else "")
    not_checked = "".join(f"<li>{esc(loc(x, lang))}</li>" for x in report["coverage"].get("not_checked", []))
    limits = "".join(f"<li>{esc(loc(x, lang))}</li>" for x in report["limits"])
    size = (f"· {f.get('words', '?')} {u['words']}" if modality == "text"
            else f"· {f.get('files', '?')} {u['files_word']}, {f.get('lines', '?')} {u['lines_word']}" if modality == "code"
            else f"{f['width']}×{f['height']}" if f.get("width") else "?")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">'
            f'<title>{esc(u["title"])}</title><style>{CSS}</style></head><body><main>'
            f'<h1>{esc(u["title"])}</h1><p class="meta"><span>{esc(u["file"])}: <b>{esc(f["name"])}</b></span>'
            f'<span>{esc(u["format"])}: {esc(f["format"])} {esc(size)}</span>'
            f'<span>sha256: {esc(f["sha256"][:16])}…</span><span>{esc(u["generated_on"])}: {esc(report["generated_at"])}</span></p>'
            f'<div class="cards">{cards}</div>{sections}{measurements}'
            f'<h2>{esc(u["not_checked"])}</h2><ul>{not_checked}</ul><h2>{esc(u["limits"])}</h2><ul>{limits}</ul>'
            f'<h2>{esc(u["method"])}</h2><p class="note">{esc(u["method_text"])}</p>'
            f'<p class="note" style="margin-top:16px"><b>{esc(u["disclaimer"])}</b></p>'
            + (f'<p class="note" style="margin-top:12px"><b>{esc(u["disclaimer_text"])}</b></p>' if modality == "text" else "")
            + (f'<p class="note" style="margin-top:12px"><b>{esc(u["disclaimer_code"])}</b></p>' if modality == "code" else "") + '</main></body></html>')


def build_report(evidence_doc: dict, visual: list[dict], pixels: dict | None = None) -> dict:
    if not isinstance(evidence_doc, dict) or not isinstance(evidence_doc.get("evidence"), list):
        fail("evidence file must be the JSON written by analyze_image.py")
    items = [validate_item(e, f"evidence[{i}]") for i, e in enumerate(evidence_doc["evidence"])]
    items += visual
    coverage = dict(evidence_doc.get("coverage", {}))
    limits = list(evidence_doc.get("limits", []))
    measurements: list = list(evidence_doc.get("measurements", []))
    if pixels is not None:
        items += pixels["evidence"]
        measurements += pixels["measurements"]
        # the pixel module ran, so the "not run" notes from step 1 no longer apply
        coverage["not_checked"] = [x for x in coverage.get("not_checked", []) if not (isinstance(x, dict) and x.get("id") == "pixels")]
        coverage["checked"] = list(coverage.get("checked", [])) + pixels["coverage"].get("checked", [])
        limits = [x for x in limits if not (isinstance(x, dict) and x.get("id") == "pixels")] + pixels["limits"]
    apply_group_caps(items)
    ids = [e["id"] for e in items]
    if len(ids) != len(set(ids)):
        fail("evidence ids must be unique (rename the visual observations that repeat an id)")
    for e in items:
        for text in (e.get("detail"), e.get("title")):
            if TOKEN_VALUE.search(loc(text, "en")):
                fail(f"evidence '{e['id']}' contains something that looks like a secret; remove it")
    return {
        "file": evidence_doc.get("file", {"name": "?", "format": "?", "sha256": "0" * 64}),
        "generated_at": datetime.date.today().isoformat(),
        "evidence": items,
        "assessment": {c: aggregate(items, c) for c in CLAIMS},
        "coverage": coverage,
        "limits": limits,
        "measurements": measurements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine AI-evidence items into scores and an HTML report.")
    parser.add_argument("evidence", help="evidence.json from analyze_image.py")
    parser.add_argument("--visual", help="visual.json with the model's visual observations")
    parser.add_argument("--pixels", help="pixels.json from analyze_pixels.py (optional module)")
    parser.add_argument("--out", required=True, help="HTML report to write")
    parser.add_argument("--json-out", help="also write the combined result as JSON")
    parser.add_argument("--lang", choices=LANGUAGES, default="es")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out = checked_output_path(args.out, ".html", args.force)
    json_out = checked_output_path(args.json_out, ".json", args.force) if args.json_out else None
    visual = load_visual(args.visual) if args.visual else []
    pixels = load_pixels(args.pixels) if args.pixels else None
    report = build_report(load_json(args.evidence), visual, pixels)
    out.write_text(render(report, args.lang), encoding="utf-8")
    if json_out:
        json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    top = sorted((e for e in report["evidence"] if e["weight"] > 0), key=lambda e: -e["weight"])[:3]
    dump({"report": str(out),
          "assessment": {c: {k: v for k, v in a.items() if k != "shares"} for c, a in report["assessment"].items()},
          "top_evidence": [{"id": e["id"], "score": e["score"], "weight": e["weight"], "title": loc(e["title"], args.lang)}
                           for e in top]})


if __name__ == "__main__":
    main()
