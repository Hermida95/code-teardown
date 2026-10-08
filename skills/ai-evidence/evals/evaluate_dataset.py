#!/usr/bin/env python3
"""Measure ai-evidence on a labelled set of images, and suggest weights from the data.

The skill's weights are reasoned estimates. This script is how they get checked: it runs the
automatic layers (file evidence, and optionally the pixel module) on images whose label you know
and reports, with confidence intervals,

  * the false-positive rate on real images (the number that matters most),
  * the detection rate on generated and on AI-edited images,
  * how often the result is "not enough evidence" (abstention), per label,
  * how each piece of evidence behaves: how often it fires on each kind of image, and a suggested
    weight to compare with the current one.

The model's own visual inspection is not part of it: it needs a model in the loop.

Dataset layout (see README.md):  <dataset>/<label>/<source>/<file>   with label in
real | generated | edited, or a manifest.csv with the columns path,label[,source,split].

Images are split deterministically by content hash into dev (70 %) and test (30 %) unless the
manifest names a split. Weights are suggested from dev only and the headline numbers come from
test, so you do not grade the weights on the images they were fitted to.

Usage: evaluate_dataset.py DATASET --out-dir DIR [--pixels] [--augment strip] [--no-filename]
                           [--manifest FILE] [--lang en|es] [--max-files N]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}).")

import argparse
import csv
import hashlib
import json
import math
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import analyze_image as ai  # noqa: E402
import score_evidence as se  # noqa: E402
from _common import clean_quote  # noqa: E402

LABELS = ("real", "generated", "edited")
ALIASES = {"real": "real", "human": "real", "authentic": "real", "generated": "generated", "ai": "generated",
           "ai_generated": "generated", "synthetic": "generated", "edited": "edited", "ai_edited": "edited",
           "inpainted": "edited"}
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_FILE_BYTES = 64 * 1024 * 1024
DEV_SHARE = 70
FLAGGED = ("strong", "very_strong")
ABSTAIN = ("insufficient", "conflict")
MIN_PER_CLASS = 30           # below this, suggested weights and strong claims are withheld
# claim -> (label that should score high, label it is contrasted with)
CONTRASTS = {"generated": ("generated", "real"), "ai_edited": ("edited", "real")}


# --- statistics (standard library) -------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for a proportion k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def auc(positive: list[float], negative: list[float]) -> float | None:
    """Probability that a random positive scores above a random negative (ties count half)."""
    if not positive or not negative:
        return None
    pooled = sorted([(s, 1) for s in positive] + [(s, 0) for s in negative])
    ranks, i = [0.0] * len(pooled), 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rank_sum = sum(r for r, (_, is_pos) in zip(ranks, pooled) if is_pos)
    n_pos, n_neg = len(positive), len(negative)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def likelihood_ratio(fired_pos: int, n_pos: int, fired_neg: int, n_neg: int) -> float:
    """How much more often an item fires on positives than on negatives (add-one smoothing)."""
    return ((fired_pos + 1) / (n_pos + 2)) / ((fired_neg + 1) / (n_neg + 2))


def suggested_weight(lr: float) -> float:
    """Rule of thumb, not a derived probability: LR 100 (or 1/100) gives 0.97, LR 10 gives 0.5, LR 3 about 0.24."""
    return round(min(0.97, abs(math.log(lr)) / math.log(100)), 2)


# --- dataset ---------------------------------------------------------------------------------

def split_for(sha256: str) -> str:
    return "dev" if int(sha256[:8], 16) % 100 < DEV_SHARE else "test"


def discover(dataset: Path, manifest: Path | None, max_files: int) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    problems: list[str] = []
    root = dataset.resolve()

    def inside(path: Path) -> bool:
        try:
            path.resolve().relative_to(root)
            return True
        except ValueError:
            return False

    if manifest:
        with manifest.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"path", "label"} <= set(reader.fieldnames):
                raise SystemExit("error: the manifest needs the columns path and label (source and split are optional)")
            for line, rec in enumerate(reader, start=2):
                label = ALIASES.get((rec.get("label") or "").strip().lower())
                target = dataset / (rec.get("path") or "")
                if label is None:
                    problems.append(f"manifest line {line}: unknown label {rec.get('label')!r}")
                elif not inside(target) or not target.is_file():
                    problems.append(f"manifest line {line}: {rec.get('path')!r} is not a file inside the dataset")
                else:
                    split = (rec.get("split") or "").strip().lower() or None
                    if split not in (None, "dev", "test"):
                        problems.append(f"manifest line {line}: split must be dev or test")
                        continue
                    rows.append({"path": str(target.relative_to(dataset)), "abs": target, "label": label,
                                 "source": (rec.get("source") or "").strip() or "unspecified", "split": split})
    else:
        for top in sorted(p for p in dataset.iterdir() if p.is_dir()):
            label = ALIASES.get(top.name.lower())
            if label is None:
                problems.append(f"folder {top.name!r} is not a label (use real, generated or edited); ignored")
                continue
            for file in sorted(top.rglob("*")):
                if file.is_file() and not file.is_symlink() and file.suffix.lower() in EXTENSIONS and inside(file):
                    parts = file.relative_to(top).parts
                    rows.append({"path": str(file.relative_to(dataset)), "abs": file, "label": label,
                                 "source": parts[0] if len(parts) > 1 else "unspecified", "split": None})
    if len(rows) > max_files:
        problems.append(f"{len(rows)} files found; only the first {max_files} are used (see --max-files)")
        rows = rows[:max_files]
    return rows, problems


# --- running the skill on one image ------------------------------------------------------------

def stripped_copy(source: Path, directory: Path) -> Path:
    """Re-encode like a messaging app: no metadata, longest edge 1600, JPEG quality 80."""
    import warnings
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 50_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        im = Image.open(source)
        if im.size[0] * im.size[1] > Image.MAX_IMAGE_PIXELS:
            raise ValueError("image has too many pixels to re-encode safely")
        im.draft("RGB", (1600, 1600))          # JPEG only: decode at a reduced size when it is going to shrink anyway
    with im:
        im = im.convert("RGB")
        if max(im.size) > 1600:
            scale = 1600 / max(im.size)
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        target = directory / "image.jpg"
        im.save(target, "JPEG", quality=80)
    return target


def run_image(path: Path, use_pixels: bool, drop_filename: bool) -> dict:
    result = ai.analyze(path)
    items = [se.validate_item(e, "evidence") for e in result["evidence"]]
    if drop_filename:
        items = [e for e in items if e["id"] != "filename-hint"]
    if use_pixels:
        import analyze_pixels as ap
        try:
            for e in ap.analyze(path)["evidence"]:
                item = se.validate_item(e, "pixels")
                item["weight"] = min(item["weight"], se.PIXEL_CAP)
                items.append(item)
        except ValueError as exc:
            result.setdefault("limits", []).append({"en": f"pixel module skipped: {exc}", "es": f"módulo de píxeles omitido: {exc}"})
    assessment = {c: se.aggregate(items, c) for c in se.CLAIMS}
    return {"format": result["file"]["format"], "sha256": result["file"]["sha256"],
            "has_metadata": not any(e["id"] == "no-metadata" for e in items),
            "assessment": {c: {k: a[k] for k in ("score", "band", "status", "confidence", "total_weight")} for c, a in assessment.items()},
            "fired": [{"id": e["id"], "claim": e["claim"], "score": e["score"], "weight": e["weight"]} for e in items if e["weight"] > 0]}


def evaluate(rows: list[dict], conditions: list[str], use_pixels: bool, drop_filename: bool, progress: bool) -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory(prefix="ai-evidence-eval-") as scratch:
        for index, row in enumerate(rows, start=1):
            for condition in conditions:
                entry = {"path": row["path"], "label": row["label"], "source": row["source"], "condition": condition}
                try:
                    if row["abs"].stat().st_size > MAX_FILE_BYTES:
                        raise ValueError("file is larger than 64 MB")
                    target = row["abs"]
                    if condition == "stripped":
                        work = Path(scratch) / f"{index}"
                        work.mkdir()
                        target = stripped_copy(row["abs"], work)
                    entry.update(run_image(target, use_pixels, drop_filename))
                except Exception as exc:  # one bad file must not stop a long run
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                results.append(entry)
            if progress and index % 25 == 0:
                print(f"  {index}/{len(rows)} images", file=sys.stderr)
    return results


# --- summaries -------------------------------------------------------------------------------

def content_sha(path: Path) -> str | None:
    """SHA-256 in 1 MB chunks. Files over the size cap are never read: they get None and a split from their path."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def attach_splits(rows: list[dict], results: list[dict]) -> None:
    by_path = {r["path"]: r for r in rows}
    original_sha = {}
    for entry in results:
        if entry["condition"] == "original" and "sha256" in entry:
            original_sha[entry["path"]] = entry["sha256"]
    for entry in results:
        row = by_path[entry["path"]]
        sha = original_sha.get(entry["path"]) or content_sha(row["abs"])
        entry["split"] = row["split"] or split_for(sha or hashlib.sha256(entry["path"].encode("utf-8", "replace")).hexdigest())
        entry["content_sha256"] = sha


def rate(k: int, n: int) -> dict:
    low, high = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci95": [round(low, 3), round(high, 3)]}


def claim_metrics(entries: list[dict], claim: str) -> dict:
    pos_label, neg_label = CONTRASTS[claim]
    usable = [e for e in entries if "error" not in e]
    pos = [e for e in usable if e["label"] == pos_label]
    neg = [e for e in usable if e["label"] == neg_label]

    def band(e):
        return e["assessment"][claim]["band"]

    def score(e):
        s = e["assessment"][claim]["score"]
        return 5.0 if s is None or band(e) in ABSTAIN else s

    flagged_pos, flagged_neg = sum(band(e) in FLAGGED for e in pos), sum(band(e) in FLAGGED for e in neg)
    return {
        "n_positive": len(pos), "n_negative": len(neg),
        "detection_rate": rate(flagged_pos, len(pos)),
        "false_positive_rate": rate(flagged_neg, len(neg)),
        "abstain_on_positive": rate(sum(band(e) in ABSTAIN for e in pos), len(pos)),
        "abstain_on_negative": rate(sum(band(e) in ABSTAIN for e in neg), len(neg)),
        "decisive_on_positive": rate(sum(e["assessment"][claim]["status"] == "conclusive" for e in pos), len(pos)),
        "precision_if_this_mix": (flagged_pos / (flagged_pos + flagged_neg) if flagged_pos + flagged_neg else None),
        "auc_abstain_as_neutral": auc([score(e) for e in pos], [score(e) for e in neg]),
        "enough_data": len(pos) >= MIN_PER_CLASS and len(neg) >= MIN_PER_CLASS,
    }


def band_table(entries: list[dict], claim: str) -> dict:
    table: dict = {label: Counter() for label in LABELS}
    for e in entries:
        if "error" not in e:
            table[e["label"]][e["assessment"][claim]["band"]] += 1
    return {label: dict(counter) for label, counter in table.items()}


def evidence_table(entries: list[dict]) -> list[dict]:
    """How each evidence id behaves on dev images, with the current weight next to a suggested one."""
    usable = [e for e in entries if "error" not in e]
    counts: dict = defaultdict(lambda: Counter())
    meta: dict = {}
    totals = Counter(e["label"] for e in usable)
    for e in usable:
        for f in e["fired"]:
            counts[f["id"]][e["label"]] += 1
            meta[f["id"]] = f
    rows = []
    for ident, per_label in sorted(counts.items()):
        item = meta[ident]
        pos_label, neg_label = CONTRASTS[item["claim"]]
        n_pos, n_neg = totals[pos_label], totals[neg_label]
        lr = likelihood_ratio(per_label[pos_label], n_pos, per_label[neg_label], n_neg)
        expected_high = item["score"] >= 5
        agrees = (lr > 1) == expected_high if abs(math.log(lr)) > 0.05 else None
        enough = n_pos >= MIN_PER_CLASS and n_neg >= MIN_PER_CLASS
        suggestion = suggested_weight(lr) if enough else None
        if suggestion is not None and ident.startswith("px-"):
            suggestion = min(suggestion, se.PIXEL_CAP)
        rows.append({"id": ident, "claim": item["claim"], "current_score": item["score"], "current_weight": item["weight"],
                     "fired_on": {label: per_label.get(label, 0) for label in LABELS}, "n": {label: totals[label] for label in LABELS},
                     "likelihood_ratio": round(lr, 2), "direction_matches_score": agrees,
                     "suggested_weight": suggestion, "enough_data": enough})
    return rows


def duplicates(results: list[dict]) -> list[dict]:
    seen: dict = defaultdict(set)
    paths: dict = defaultdict(set)
    for e in results:
        if e["condition"] == "original" and e.get("content_sha256"):
            seen[e["content_sha256"]].add(e["label"])
            paths[e["content_sha256"]].add(e["path"])
    return [{"paths": sorted(paths[sha]), "labels": sorted(labels)} for sha, labels in seen.items() if len(paths[sha]) > 1]


def summarize(results: list[dict], conditions: list[str]) -> dict:
    summary: dict = {"files": len({r["path"] for r in results}), "errors": [r for r in results if "error" in r][:20],
                     "duplicates": duplicates(results),
                     "by_label": dict(Counter(r["label"] for r in results if r["condition"] == "original")),
                     "by_split": dict(Counter(f"{r['split']}/{r['label']}" for r in results if r["condition"] == "original")),
                     "conditions": {}}
    for condition in conditions:
        scoped = [r for r in results if r["condition"] == condition]
        block: dict = {"claims": {}}
        for claim in CONTRASTS:
            block["claims"][claim] = {
                split: {"metrics": claim_metrics([r for r in scoped if r["split"] == split], claim),
                        "bands": band_table([r for r in scoped if r["split"] == split], claim)}
                for split in ("test", "dev")}
        reals = [r for r in scoped if r["label"] == "real" and r["split"] == "test" and "error" not in r]
        by_source: dict = defaultdict(list)
        for r in reals:
            by_source[r["source"]].append(r)
        block["false_positive_by_source"] = {
            src: {claim: rate(sum(r["assessment"][claim]["band"] in FLAGGED for r in group), len(group)) for claim in CONTRASTS}
            for src, group in sorted(by_source.items())}
        with_meta = [r for r in scoped if "error" not in r and r["label"] == "generated" and r["split"] == "test"]
        block["generated_detection_by_metadata"] = {
            key: rate(sum(r["assessment"]["generated"]["band"] in FLAGGED for r in group), len(group))
            for key, group in (("with_metadata", [r for r in with_meta if r["has_metadata"]]),
                               ("without_metadata", [r for r in with_meta if not r["has_metadata"]]))}
        block["evidence_on_dev"] = evidence_table([r for r in scoped if r["split"] == "dev"])
        summary["conditions"][condition] = block
    return summary


# --- report ----------------------------------------------------------------------------------

TEXT = {
    "en": {
        "files": "files", "label": "label", "source": "source", "fired": "fired (real / generated / edited)", "suggested": "suggested weight",
        "few_detail": "test: {pos} positives, {neg} negatives; at least {need} of each are needed", "too_few": "n/a (too few)",
        "with_meta": "with metadata", "without_meta": "without metadata", "dup": "duplicate content", "dup_labels": " with different labels",
        "id": "id", "claim_col": "claim", "score_col": "score", "weight_col": "weight", "direction": "direction",
        "title": "ai-evidence evaluation", "dataset": "Dataset", "warnings": "Warnings", "headline": "Headline numbers (test split)",
        "metric": "Metric", "value": "Value", "fpr": "False-positive rate on real images", "tpr": "Detection rate",
        "abstain_pos": "\"Not enough evidence\" on positives", "abstain_neg": "\"Not enough evidence\" on real images",
        "decisive": "Positives decided by a conclusive item", "auc": "AUC (abstention counted as neutral)", "prec": "Precision (depends on this dataset's mix)",
        "bands": "Where each kind of image lands (test split)", "sources": "False positives by source of the real images (test split)",
        "meta": "Detection of generated images with and without metadata (test split)", "evidence": "Evidence on the dev split",
        "evidence_note": "LR is how much more often the item fires on positives than on negatives. A suggested weight appears only with at least 30 positives and 30 negatives in dev. It is a rule of thumb (LR 10 gives 0.5, LR 100 gives 0.97), not a probability.",
        "ci": "95% CI", "few": "too few images to conclude anything", "reading": "How to read this",
        "reading_text": [
            "The false-positive rate is the number to watch: a real photo marked as AI is the harm this tool must avoid. Look at the upper end of its interval, not the point estimate.",
            "A high abstention rate is not a failure. It means the file carried no evidence, which is the honest answer for stripped images.",
            "Weights are suggested from the dev split and the headline comes from the test split. Do not change weights while looking at test, or the test numbers stop meaning anything.",
            "Precision depends on how many real and generated images you included, so it does not transfer to real life, where the mix is different.",
            "These numbers describe this dataset. A different mix of generators, cameras and platforms will give different ones.",
        ],
        "cond": {"original": "Original files", "stripped": "Stripped copies (no metadata, JPEG q80, max 1600 px)"},
        "claim": {"generated": "Generated by AI", "ai_edited": "Edited with AI"},
    },
    "es": {
        "files": "archivos", "label": "etiqueta", "source": "origen", "fired": "salta en (real / generada / editada)", "suggested": "peso sugerido",
        "few_detail": "test: {pos} positivos, {neg} negativos; hacen falta al menos {need} de cada", "too_few": "n/d (muy pocas)",
        "with_meta": "con metadatos", "without_meta": "sin metadatos", "dup": "contenido duplicado", "dup_labels": " con etiquetas distintas",
        "id": "id", "claim_col": "pregunta", "score_col": "nota", "weight_col": "peso", "direction": "sentido",
        "title": "Evaluación de ai-evidence", "dataset": "Conjunto de datos", "warnings": "Avisos", "headline": "Cifras principales (split test)",
        "metric": "Medida", "value": "Valor", "fpr": "Tasa de falsos positivos en imágenes reales", "tpr": "Tasa de detección",
        "abstain_pos": "«Sin pruebas suficientes» en positivos", "abstain_neg": "«Sin pruebas suficientes» en imágenes reales",
        "decisive": "Positivos decididos por una prueba concluyente", "auc": "AUC (la abstención cuenta como neutra)", "prec": "Precisión (depende de la mezcla de este conjunto)",
        "bands": "Dónde cae cada tipo de imagen (split test)", "sources": "Falsos positivos según el origen de las imágenes reales (split test)",
        "meta": "Detección de imágenes generadas con y sin metadatos (split test)", "evidence": "Evidencias en el split dev",
        "evidence_note": "LR indica cuánto más a menudo salta la prueba en positivos que en negativos. Solo se sugiere un peso con al menos 30 positivos y 30 negativos en dev. Es una regla práctica (LR 10 da 0,5; LR 100 da 0,97), no una probabilidad.",
        "ci": "IC 95 %", "few": "muy pocas imágenes para concluir nada", "reading": "Cómo leerlo",
        "reading_text": [
            "La tasa de falsos positivos es el número que importa: una foto real marcada como IA es el daño que esta herramienta debe evitar. Mira el extremo alto de su intervalo, no la estimación puntual.",
            "Una abstención alta no es un fallo. Significa que el archivo no traía pruebas, que es la respuesta honesta para imágenes sin metadatos.",
            "Los pesos se sugieren con el split dev y las cifras principales salen del split test. No cambies pesos mirando el test, o sus números dejan de significar algo.",
            "La precisión depende de cuántas imágenes reales y generadas incluyas, así que no se traslada a la vida real, donde la mezcla es otra.",
            "Estas cifras describen este conjunto. Otra mezcla de generadores, cámaras y plataformas dará otras.",
        ],
        "cond": {"original": "Archivos originales", "stripped": "Copias sin metadatos (JPEG q80, máx. 1600 px)"},
        "claim": {"generated": "Generada por IA", "ai_edited": "Editada con IA"},
    },
}


def cell(value) -> str:
    """Make text that came from file names, folder names or a manifest safe for a Markdown line or table cell."""
    text = clean_quote(str(value), 120)
    for char, escaped in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("|", "\\|"), ("`", "'"), ("[", "\\["), ("]", "\\]")):
        text = text.replace(char, escaped)
    return text


def pct(r: dict) -> str:
    if r["n"] == 0:
        return "n/a"
    return f"{r['rate'] * 100:.1f}% ({r['k']}/{r['n']}; {r['ci95'][0] * 100:.0f}-{r['ci95'][1] * 100:.0f}%)"


def render_markdown(summary: dict, problems: list[str], lang: str) -> str:
    t = TEXT[lang]
    out = [f"# {t['title']}", "", f"## {t['dataset']}", ""]
    out.append(f"- {summary['files']} {t['files']}. " + ", ".join(f"{k}: {v}" for k, v in sorted(summary["by_label"].items())))
    out.append("- split: " + ", ".join(f"{k}: {v}" for k, v in sorted(summary["by_split"].items())))
    warnings = list(problems)
    for dup in summary["duplicates"]:
        flag = t["dup_labels"] if len(dup["labels"]) > 1 else ""
        warnings.append(f"{t['dup']}{flag}: {', '.join(cell(p) for p in dup['paths'][:4])}")
    for e in summary["errors"]:
        warnings.append(f"{cell(e['path'])}: {cell(e['error'])}")
    if warnings:
        out += ["", f"### {t['warnings']}", ""] + [f"- {cell(w)}" for w in warnings[:40]]
    for condition, block in summary["conditions"].items():
        out += ["", f"## {t['cond'][condition]}"]
        for claim, per_split in block["claims"].items():
            m = per_split["test"]["metrics"]
            out += ["", f"### {t['claim'][claim]}: {t['headline']}", ""]
            if not m["enough_data"]:
                out.append(f"> {t['few']} ({t['few_detail'].format(pos=m['n_positive'], neg=m['n_negative'], need=MIN_PER_CLASS)}).")
                out.append("")
            out += [f"| {t['metric']} | {t['value']} |", "| --- | --- |",
                    f"| **{t['fpr']}** | **{pct(m['false_positive_rate'])}** |",
                    f"| {t['tpr']} | {pct(m['detection_rate'])} |",
                    f"| {t['abstain_pos']} | {pct(m['abstain_on_positive'])} |",
                    f"| {t['abstain_neg']} | {pct(m['abstain_on_negative'])} |",
                    f"| {t['decisive']} | {pct(m['decisive_on_positive'])} |",
                    f"| {t['auc']} | {'n/a' if m['auc_abstain_as_neutral'] is None else format(m['auc_abstain_as_neutral'], '.3f')} |",
                    f"| {t['prec']} | {'n/a' if m['precision_if_this_mix'] is None else format(m['precision_if_this_mix'], '.2f')} |"]
            bands = per_split["test"]["bands"]
            names = ["none", "low", "mixed", "strong", "very_strong", "insufficient", "conflict"]
            out += ["", f"**{t['bands']}**", "", f"| {t['label']} | " + " | ".join(names) + " |", "| --- |" + " --- |" * len(names)]
            out += [f"| {label} | " + " | ".join(str(bands[label].get(n, 0)) for n in names) + " |" for label in LABELS]
        if block["false_positive_by_source"]:
            out += ["", f"### {t['sources']}", "", f"| {t['source']} | " + " | ".join(t["claim"].values()) + " |", "| --- | --- | --- |"]
            out += [f"| {cell(src)} | " + " | ".join(pct(v[c]) for c in CONTRASTS) + " |" for src, v in block["false_positive_by_source"].items()]
        meta = block["generated_detection_by_metadata"]
        out += ["", f"### {t['meta']}", "", "| | |", "| --- | --- |",
                f"| {t['with_meta']} | {pct(meta['with_metadata'])} |", f"| {t['without_meta']} | {pct(meta['without_metadata'])} |"]
        out += ["", f"### {t['evidence']}", "", t["evidence_note"], "",
                f"| {t['id']} | {t['claim_col']} | {t['score_col']} | {t['weight_col']} | {t['fired']} | LR | {t['suggested']} |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for e in block["evidence_on_dev"]:
            fired = " / ".join(f"{e['fired_on'][label]}/{e['n'][label]}" for label in LABELS)
            suggestion = t["too_few"] if e["suggested_weight"] is None else f"{e['suggested_weight']:.2f}"
            flag = "" if e["direction_matches_score"] in (True, None) else f" ⚠ {t['direction']}"
            out.append(f"| `{e['id']}` | {e['claim']} | {e['current_score']:g} | {e['current_weight']:g} | {fired} | {e['likelihood_ratio']:g}{flag} | {suggestion} |")
    out += ["", f"## {t['reading']}", ""] + [f"- {line}" for line in t["reading_text"]]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ai-evidence on a labelled image set.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True, help="a new or empty directory for results.json, summary.json and report.md")
    parser.add_argument("--manifest", type=Path, help="manifest.csv with path,label[,source,split]")
    parser.add_argument("--pixels", action="store_true", help="also run the optional pixel module (needs Pillow and NumPy)")
    parser.add_argument("--augment", choices=["strip"], help="also evaluate copies re-encoded like a messaging app (needs Pillow)")
    parser.add_argument("--no-filename", action="store_true", help="drop the file-name evidence (names can leak the label in a sorted dataset)")
    parser.add_argument("--lang", choices=["en", "es"], default="en")
    parser.add_argument("--max-files", type=int, default=20000)
    args = parser.parse_args()

    if not args.dataset.is_dir():
        sys.exit(f"error: {args.dataset} is not a directory")
    if args.out_dir.exists() and (not args.out_dir.is_dir() or any(args.out_dir.iterdir())):
        sys.exit(f"error: {args.out_dir} must not exist yet or must be an empty directory")
    if args.pixels or args.augment:
        try:
            import numpy  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            sys.exit("error: --pixels and --augment need Pillow and NumPy; try: uv run --with pillow --with numpy python evals/evaluate_dataset.py ...")
    rows, problems = discover(args.dataset, args.manifest, args.max_files)
    if not rows:
        sys.exit("error: no labelled images found. " + " ".join(problems))
    conditions = ["original"] + (["stripped"] if args.augment == "strip" else [])
    print(f"evaluating {len(rows)} images ({', '.join(conditions)})", file=sys.stderr)
    results = evaluate(rows, conditions, args.pixels, args.no_filename, progress=True)
    attach_splits(rows, results)
    summary = summarize(results, conditions)
    summary["settings"] = {"pixels": args.pixels, "augment": args.augment, "no_filename": args.no_filename, "dev_share_percent": DEV_SHARE}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.out_dir / "report.md").write_text(render_markdown(summary, problems, args.lang), encoding="utf-8")
    print(f"wrote {args.out_dir}/report.md, summary.json and results.json")


if __name__ == "__main__":
    main()
