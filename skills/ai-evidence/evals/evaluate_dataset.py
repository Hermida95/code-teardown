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

It works on images, texts and codebases (one modality per run). The model's own inspection (looking at an image, reading a text) is not part of it: it needs a model in the loop.

This is a tool for checking and learning about the skill. It measures how the skill agrees with labels you supply; it says nothing about any person, and it is not a way to judge anyone's work.

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
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import analyze_image as ai  # noqa: E402
import analyze_code as ac  # noqa: E402
import analyze_text as at  # noqa: E402
import score_evidence as se  # noqa: E402
from _common import clean_quote  # noqa: E402

LABELS = ("real", "generated", "edited")
ALIASES = {"real": "real", "human": "real", "authentic": "real", "generated": "generated", "ai": "generated",
           "ai_generated": "generated", "ai_written": "generated", "synthetic": "generated", "edited": "edited", "ai_edited": "edited",
           "ai_assisted": "edited", "assisted": "edited", "inpainted": "edited"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TEXT_EXTENSIONS = {".txt", ".md", ".text"}
EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | set(ac.LANGS)      # source files are only here so a code dataset can be recognised
SIZE_BUCKETS = (("under 200 lines", 0, 200), ("200-1000 lines", 200, 1000), ("1000+ lines", 1000, 10 ** 9))
LENGTH_BUCKETS = (("under 150 words", 0, 150), ("150-400 words", 150, 400), ("400+ words", 400, 10 ** 9))
MAX_FILE_BYTES = 64 * 1024 * 1024
DEV_SHARE = 70
FLAGGED = ("strong", "very_strong")
ABSTAIN = ("insufficient", "conflict")
MIN_PER_CLASS = 30           # below this, suggested weights and strong claims are withheld
TARGET_PER_CLASS = 100       # what the guide recommends for a first useful read
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


def discover_code(dataset: Path, manifest: Path | None, max_files: int) -> tuple[list[dict], list[str]]:
    """Code samples are projects (folders) or single source files: <label>/<source>/<project-or-file>."""
    rows: list[dict] = []
    problems: list[str] = []
    root = dataset.resolve()

    def inside(path: Path) -> bool:
        try:
            path.resolve().relative_to(root)
            return not path.is_symlink()
        except ValueError:
            return False

    def is_code_file(path: Path) -> bool:
        return path.is_file() and not path.is_symlink() and path.suffix.lower() in ac.LANGS

    if manifest:
        with manifest.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"path", "label"} <= set(reader.fieldnames):
                raise SystemExit("error: the manifest needs the columns path and label (source and split are optional)")
            for line, rec in enumerate(reader, start=2):
                label = ALIASES.get((rec.get("label") or "").strip().lower())
                target = dataset / (rec.get("path") or "")
                split = (rec.get("split") or "").strip().lower() or None
                if label is None:
                    problems.append(f"manifest line {line}: unknown label {rec.get('label')!r}")
                elif not inside(target) or not (target.is_dir() or is_code_file(target)):
                    problems.append(f"manifest line {line}: {rec.get('path')!r} is not a project folder or source file inside the dataset")
                elif split not in (None, "dev", "test"):
                    problems.append(f"manifest line {line}: split must be dev or test")
                else:
                    rows.append({"path": str(target.relative_to(dataset)), "abs": target, "label": label,
                                 "source": (rec.get("source") or "").strip() or "unspecified", "split": split})
    else:
        for top in sorted(p for p in dataset.iterdir() if p.is_dir() and not p.is_symlink()):
            label = ALIASES.get(top.name.lower())
            if label is None:
                problems.append(f"folder {top.name!r} is not a label (use real, generated or edited); ignored")
                continue
            for source in sorted(top.iterdir()):
                if not inside(source):
                    continue
                if source.is_dir():
                    for item in sorted(source.iterdir()):
                        if inside(item) and (item.is_dir() or is_code_file(item)):
                            rows.append({"path": str(item.relative_to(dataset)), "abs": item, "label": label, "source": source.name, "split": None})
                elif is_code_file(source):
                    rows.append({"path": str(source.relative_to(dataset)), "abs": source, "label": label, "source": "unspecified", "split": None})
    if len(rows) > max_files:
        problems.append(f"{len(rows)} samples found; only the first {max_files} are used (see --max-files)")
        rows = rows[:max_files]
    return rows, problems


def discover(dataset: Path, manifest: Path | None, max_files: int, code: bool = False) -> tuple[list[dict], list[str]]:
    if code:
        return discover_code(dataset, manifest, max_files)
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


def modality_of(path: Path) -> str | None:
    if path.is_dir():
        return "code"
    suffix = path.suffix.lower()
    return "image" if suffix in IMAGE_EXTENSIONS else "text" if suffix in TEXT_EXTENSIONS else "code" if suffix in ac.LANGS else None


def looks_like_code(rows: list[dict]) -> bool:
    """True when the labelled files include source files (the README.md inside each project would otherwise
    make a code dataset look like a text dataset). Source files next to images is a mixed dataset."""
    kinds = Counter(modality_of(r["abs"]) for r in rows)
    if kinds["code"] and kinds["image"]:
        raise SystemExit("error: the dataset mixes images and code; evaluate them separately (separate folders) or force one with --modality")
    return bool(kinds["code"])


def infer_modality(rows: list[dict], forced: str | None) -> str:
    kinds = Counter(modality_of(r["abs"]) for r in rows)
    if forced:
        return forced
    kinds.pop(None, None)
    if len(kinds) > 1:
        raise SystemExit("error: the dataset mixes images and texts; evaluate them separately (separate folders) or force one with --modality")
    return next(iter(kinds), "image")


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
            "has_provenance": not any(e["id"] == "no-metadata" for e in items),
            "assessment": {c: {k: a[k] for k in ("score", "band", "status", "confidence", "total_weight")} for c, a in assessment.items()},
            "fired": [{"id": e["id"], "claim": e["claim"], "score": e["score"], "weight": e["weight"]} for e in items if e["weight"] > 0]}


def read_text_file(path: Path) -> str:
    with path.open("rb") as handle:
        raw = handle.read(at.MAX_BYTES)
    if b"\x00" in raw[:4096]:
        raise ValueError("looks like a binary file, not text")
    return raw.decode("utf-8-sig", "replace")


def strip_residue(text: str) -> str:
    """Remove what a chat assistant leaves behind, the way someone who knows about it would: this shows how much
    the result depends on residue rather than on style."""
    for pattern, _ in at.CITATION_MARKERS:
        text = pattern.sub("", text)
    text = re.sub(r"[?&]utm_source=[^&\s)]+", "", text)
    text = at.PLACEHOLDERS.sub("", text)
    kept = []
    for sentence in at.SENTENCE_END.split(text):
        if any(rx.search(sentence) for rx in list(at.ASSISTANT_STRONG_RES.values()) + list(at.ASSISTANT_SOFT_RES.values())):
            continue
        kept.append(sentence)
    return " ".join(kept)


def run_text(text: str, name: str) -> dict:
    result = at.analyze(text, name)
    items = [se.validate_item(e, "evidence") for e in result["evidence"]]
    se.apply_group_caps(items)
    assessment = {c: se.aggregate(items, c) for c in se.CLAIMS}
    return {"format": "text", "sha256": result["file"]["sha256"], "words": result["file"]["words"], "language": result["file"]["language"],
            "has_provenance": any(e["source"] == "residue" and e["weight"] > 0 for e in items),
            "assessment": {c: {k: a[k] for k in ("score", "band", "status", "confidence", "total_weight")} for c, a in assessment.items()},
            "fired": [{"id": e["id"], "claim": e["claim"], "score": e["score"], "weight": e["weight"], "source": e["source"]} for e in items if e["weight"] > 0]}


def read_git_log(path: Path) -> str | None:
    """The optional git-log.txt a user exported into a project folder. Git is never run by the harness."""
    log = path / "git-log.txt" if path.is_dir() else None
    if log is None or log.is_symlink() or not log.is_file():
        return None
    with log.open("rb") as handle:
        return handle.read(ac.MAX_LOG_BYTES).decode("utf-8", "replace")


def strip_code_copy(source: Path, target: Path) -> None:
    """Copy a project the way someone who knows the traces would leave it: assistant files not copied, residue comments,
    pasted fences and README residue removed. Bounded, text files only, symlinks never followed."""
    files, _ = ac.walk_files(source)
    markers = ac.SOURCE_MARKERS + ac.PLACEHOLDERS + [ac.CHAT_REPLY]
    for rel, text in files:
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if Path(rel).name.lower().startswith("readme"):
            text = strip_residue(text)
        elif Path(rel).suffix.lower() in ac.LANGS:
            lines = [line for line in text.split("\n") if not (line.strip().startswith(ac.COMMENT_START) and any(m.search(line.strip()) for m in markers))]
            while lines and lines[0].strip().startswith("```"):
                lines.pop(0)
            while lines and (not lines[-1].strip() or lines[-1].strip().startswith("```")):
                lines.pop()
            text = "\n".join(lines) + "\n"
        destination.write_text(text, encoding="utf-8")


def strip_trailers(log: str | None) -> str | None:
    if log is None:
        return None
    return "\n".join(line for line in log.split("\n") if not ac.TRAILER.search(line))


def run_code(path: Path, git_log: str | None) -> dict:
    result = ac.analyze(path, git_log)
    items = [se.validate_item(e, "evidence") for e in result["evidence"]]
    se.apply_group_caps(items)
    assessment = {c: se.aggregate(items, c) for c in se.CLAIMS}
    return {"format": "code", "sha256": result["file"]["sha256"], "files": result["file"]["files"], "lines": result["file"]["lines"],
            "language": result["file"].get("language", "unknown"), "has_log": git_log is not None,
            "has_provenance": any(e["source"] == "residue" and e["weight"] > 0 for e in items),
            "assessment": {c: {k: a[k] for k in ("score", "band", "status", "confidence", "total_weight")} for c, a in assessment.items()},
            "fired": [{"id": e["id"], "claim": e["claim"], "score": e["score"], "weight": e["weight"], "source": e["source"]} for e in items if e["weight"] > 0]}


def evaluate(rows: list[dict], conditions: list[str], use_pixels: bool, drop_filename: bool, progress: bool, modality: str = "image") -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory(prefix="ai-evidence-eval-") as scratch:
        for index, row in enumerate(rows, start=1):
            for condition in conditions:
                entry = {"path": row["path"], "label": row["label"], "source": row["source"], "condition": condition}
                try:
                    if modality != "code" and row["abs"].stat().st_size > MAX_FILE_BYTES:
                        raise ValueError("file is larger than 64 MB")
                    if modality == "code":
                        log = read_git_log(row["abs"])
                        if condition == "stripped":
                            work = Path(scratch) / f"{index}"
                            work.mkdir()
                            strip_code_copy(row["abs"], work)
                            entry.update(run_code(work, strip_trailers(log)))
                        else:
                            entry.update(run_code(row["abs"], log))
                    elif modality == "text":
                        text = read_text_file(row["abs"])
                        entry.update(run_text(strip_residue(text) if condition == "stripped" else text, row["abs"].name))
                    else:
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
                print(f"  {index}/{len(rows)} {modality}s", file=sys.stderr)
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

    def firm(e) -> bool:
        a = e["assessment"][claim]
        return a["band"] in FLAGGED and a["confidence"] in ("medium", "high")
    return {
        "n_positive": len(pos), "n_negative": len(neg),
        "detection_rate": rate(flagged_pos, len(pos)),
        "false_positive_rate": rate(flagged_neg, len(neg)),
        "detection_rate_firm": rate(sum(firm(e) for e in pos), len(pos)),
        "false_positive_rate_firm": rate(sum(firm(e) for e in neg), len(neg)),
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
    on_real: dict = defaultdict(Counter)
    real_by_source = Counter(e["source"] for e in usable if e["label"] == "real")
    meta: dict = {}
    totals = Counter(e["label"] for e in usable)
    for e in usable:
        for f in e["fired"]:
            counts[f["id"]][e["label"]] += 1
            if e["label"] == "real":
                on_real[f["id"]][e["source"]] += 1
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
                     "suggested_weight": suggestion, "enough_data": enough,
                     "fires_on_real_by_source": {src: [on_real[ident].get(src, 0), n] for src, n in sorted(real_by_source.items())}})
    return rows


def duplicates(results: list[dict]) -> list[dict]:
    seen: dict = defaultdict(set)
    paths: dict = defaultdict(set)
    for e in results:
        if e["condition"] == "original" and e.get("content_sha256"):
            seen[e["content_sha256"]].add(e["label"])
            paths[e["content_sha256"]].add(e["path"])
    return [{"paths": sorted(paths[sha]), "labels": sorted(labels)} for sha, labels in seen.items() if len(paths[sha]) > 1]


def summarize(results: list[dict], conditions: list[str], modality: str = "image") -> dict:
    summary: dict = {"modality": modality, "files": len({r["path"] for r in results}), "errors": [r for r in results if "error" in r][:20],
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
        with_prov = [r for r in scoped if "error" not in r and r["label"] == "generated" and r["split"] == "test"]
        block["generated_detection_by_provenance"] = {
            key: rate(sum(r["assessment"]["generated"]["band"] in FLAGGED for r in group), len(group))
            for key, group in (("with_provenance", [r for r in with_prov if r["has_provenance"]]),
                               ("without_provenance", [r for r in with_prov if not r["has_provenance"]]))}
        if modality in ("text", "code"):
            test = [r for r in scoped if "error" not in r and r["split"] == "test"]

            def breakdown(groups):
                out = {}
                for name, group in groups:
                    reals = [r for r in group if r["label"] == "real"]
                    gens = [r for r in group if r["label"] == "generated"]
                    out[name] = {"false_positive_rate": rate(sum(r["assessment"]["generated"]["band"] in FLAGGED for r in reals), len(reals)),
                                 "detection_rate": rate(sum(r["assessment"]["generated"]["band"] in FLAGGED for r in gens), len(gens))}
                return out
            if modality == "text":
                block["by_length"] = breakdown((name, [r for r in test if low <= r["words"] < high]) for name, low, high in LENGTH_BUCKETS)
            else:
                block["by_size"] = breakdown((name, [r for r in test if low <= r["lines"] < high]) for name, low, high in SIZE_BUCKETS)
            block["by_language"] = breakdown((lang, [r for r in test if r["language"] == lang]) for lang in sorted({r["language"] for r in test}))
        block["evidence_on_dev"] = evidence_table([r for r in scoped if r["split"] == "dev"])
        summary["conditions"][condition] = block
    return summary


# --- report ----------------------------------------------------------------------------------

TEXT = {
    "en": {
        "files": "files", "label": "label", "source": "source", "fired": "fired (real / generated / edited)", "suggested": "suggested weight",
        "few_detail": "test: {pos} positives, {neg} negatives; at least {need} of each are needed", "too_few": "n/a (too few)",
        "with_meta": "with metadata", "without_meta": "without metadata", "with_resid": "with residue from a chat assistant", "without_resid": "without residue",
        "with_trace": "with traces of an assistant", "without_trace": "without traces", "by_size": "By code size (test split)", "by_main_language": "By main language (test split)",
        "code_hint": "For code, the false positives that matter are human projects written with formatters, templates, scaffolding tools or tutorial-style comments, and projects that call AI services. Include some of each among the real ones. Real projects must predate AI assistants or be known to have been written without one.",
        "by_length": "By text length (test split)", "by_language": "By language (test split)", "bucket": "group", "real_by_source": "fires on real, by source",
        "purpose": "This evaluation is a way to check and learn about the tool. It measures how the tool agrees with the labels you supplied. It says nothing about any person, and it is not a way to judge anyone's work.",
        "dup": "duplicate content", "dup_labels": " with different labels",
        "id": "id", "claim_col": "claim", "score_col": "score", "weight_col": "weight", "direction": "direction",
        "title": "ai-evidence evaluation", "dataset": "Dataset", "warnings": "Warnings", "headline": "Headline numbers (test split)",
        "metric": "Metric", "value": "Value", "fpr": "False-positive rate on real images", "tpr": "Detection rate",
        "fpr_firm": "False positives with at least medium confidence", "tpr_firm": "Detection with at least medium confidence",
        "abstain_pos": "\"Not enough evidence\" on positives", "abstain_neg": "\"Not enough evidence\" on real images",
        "decisive": "Positives decided by a conclusive item", "auc": "AUC (abstention counted as neutral)", "prec": "Precision (depends on this dataset's mix)",
        "bands": "Where each kind of image lands (test split)", "sources": "False positives by source of the real images (test split)",
        "meta": "Detection of generated images with and without metadata (test split)", "evidence": "Evidence on the dev split",
        "evidence_note": "LR is how much more often the item fires on positives than on negatives. A suggested weight appears only with at least 30 positives and 30 negatives in dev. It is a rule of thumb (LR 10 gives 0.5, LR 100 gives 0.97), not a probability.",
        "ci": "95% CI", "few": "too few images to conclude anything", "reading": "How to read this",
        "reading_text": [
            "The false-positive rate is the number to watch: a real photo marked as AI is the harm this tool must avoid. Look at the upper end of its interval, not the point estimate.",
            "\"Clear signs\" with low confidence still counts as flagged in the first rows; the rows with at least medium confidence show how often the tool is firm, which for texts is the number to read.",
            "A high abstention rate is not a failure. It means the file carried no evidence, which is the honest answer for stripped images.",
            "Weights are suggested from the dev split and the headline comes from the test split. Do not change weights while looking at test, or the test numbers stop meaning anything.",
            "Precision depends on how many real and generated images you included, so it does not transfer to real life, where the mix is different.",
            "These numbers describe this dataset. A different mix of generators, cameras and platforms will give different ones.",
        ],
        "cond": {"original": "Original files", "stripped": "Stripped copies (images: no metadata, JPEG q80, max 1600 px; texts: assistant residue removed; code: assistant files, trailers and residue comments removed)"},
        "claim": {"generated": "Generated by AI", "ai_edited": "Edited with AI"},
    },
    "es": {
        "files": "archivos", "label": "etiqueta", "source": "origen", "fired": "salta en (real / generada / editada)", "suggested": "peso sugerido",
        "few_detail": "test: {pos} positivos, {neg} negativos; hacen falta al menos {need} de cada", "too_few": "n/d (muy pocas)",
        "with_meta": "con metadatos", "without_meta": "sin metadatos", "with_resid": "con restos de un asistente de chat", "without_resid": "sin restos",
        "with_trace": "con rastros de un asistente", "without_trace": "sin rastros", "by_size": "Según el tamaño del código (split test)", "by_main_language": "Según el lenguaje principal (split test)",
        "code_hint": "En código, los falsos positivos que importan son proyectos humanos escritos con formateadores, plantillas, herramientas de andamiaje o comentarios de estilo tutorial, y los proyectos que llaman a servicios de IA. Incluye algunos de cada entre los reales. Los proyectos reales deben ser anteriores a los asistentes de IA o saberse escritos sin uno.",
        "by_length": "Según la longitud del texto (split test)", "by_language": "Según el idioma (split test)", "bucket": "grupo", "real_by_source": "salta en reales, por origen",
        "purpose": "Esta evaluación sirve para comprobar y aprender sobre la herramienta. Mide cuánto coincide la herramienta con las etiquetas que tú aportaste. No dice nada sobre ninguna persona y no es una forma de juzgar el trabajo de nadie.",
        "dup": "contenido duplicado", "dup_labels": " con etiquetas distintas",
        "id": "id", "claim_col": "pregunta", "score_col": "nota", "weight_col": "peso", "direction": "sentido",
        "title": "Evaluación de ai-evidence", "dataset": "Conjunto de datos", "warnings": "Avisos", "headline": "Cifras principales (split test)",
        "metric": "Medida", "value": "Valor", "fpr": "Tasa de falsos positivos en imágenes reales", "tpr": "Tasa de detección",
        "fpr_firm": "Falsos positivos con confianza al menos media", "tpr_firm": "Detección con confianza al menos media",
        "abstain_pos": "«Sin pruebas suficientes» en positivos", "abstain_neg": "«Sin pruebas suficientes» en imágenes reales",
        "decisive": "Positivos decididos por una prueba concluyente", "auc": "AUC (la abstención cuenta como neutra)", "prec": "Precisión (depende de la mezcla de este conjunto)",
        "bands": "Dónde cae cada tipo de imagen (split test)", "sources": "Falsos positivos según el origen de las imágenes reales (split test)",
        "meta": "Detección de imágenes generadas con y sin metadatos (split test)", "evidence": "Evidencias en el split dev",
        "evidence_note": "LR indica cuánto más a menudo salta la prueba en positivos que en negativos. Solo se sugiere un peso con al menos 30 positivos y 30 negativos en dev. Es una regla práctica (LR 10 da 0,5; LR 100 da 0,97), no una probabilidad.",
        "ci": "IC 95 %", "few": "muy pocas imágenes para concluir nada", "reading": "Cómo leerlo",
        "reading_text": [
            "La tasa de falsos positivos es el número que importa: una foto real marcada como IA es el daño que esta herramienta debe evitar. Mira el extremo alto de su intervalo, no la estimación puntual.",
            "«Indicios claros» con confianza baja cuenta como marcado en las primeras filas; las filas con confianza al menos media muestran cuántas veces la herramienta es firme, que en texto es el número que hay que leer.",
            "Una abstención alta no es un fallo. Significa que el archivo no traía pruebas, que es la respuesta honesta para imágenes sin metadatos.",
            "Los pesos se sugieren con el split dev y las cifras principales salen del split test. No cambies pesos mirando el test, o sus números dejan de significar algo.",
            "La precisión depende de cuántas imágenes reales y generadas incluyas, así que no se traslada a la vida real, donde la mezcla es otra.",
            "Estas cifras describen este conjunto. Otra mezcla de generadores, cámaras y plataformas dará otras.",
        ],
        "cond": {"original": "Archivos originales", "stripped": "Copias limpiadas (imágenes: sin metadatos, JPEG q80, máx. 1600 px; textos: sin restos de asistente; código: sin archivos de asistente, marcas de commit ni comentarios con restos)"},
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
    out = [f"# {t['title']}", "", f"> {t['purpose']}", "", f"## {t['dataset']}", ""]
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
                    f"| {t['fpr_firm']} | {pct(m['false_positive_rate_firm'])} |",
                    f"| {t['tpr_firm']} | {pct(m['detection_rate_firm'])} |",
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
        meta = block["generated_detection_by_provenance"]
        kind = summary.get("modality", "image")
        suffix = {"text": "resid", "code": "trace"}.get(kind, "meta")
        out += ["", f"### {t['meta']}", "", "| | |", "| --- | --- |",
                f"| {t['with_' + suffix]} | {pct(meta['with_provenance'])} |",
                f"| {t['without_' + suffix]} | {pct(meta['without_provenance'])} |"]
        for key, title in (("by_length", "by_length"), ("by_size", "by_size"), ("by_language", "by_main_language" if kind == "code" else "by_language")):
            if key in block:
                out += ["", f"### {t[title]}", "", f"| {t['bucket']} | {t['fpr']} | {t['tpr']} |", "| --- | --- | --- |"]
                out += [f"| {cell(name)} | {pct(v['false_positive_rate'])} | {pct(v['detection_rate'])} |" for name, v in block[key].items()]
        out += ["", f"### {t['evidence']}", "", t["evidence_note"], "",
                f"| {t['id']} | {t['claim_col']} | {t['score_col']} | {t['weight_col']} | {t['fired']} | LR | {t['suggested']} | {t['real_by_source']} |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for e in block["evidence_on_dev"]:
            fired = " / ".join(f"{e['fired_on'][label]}/{e['n'][label]}" for label in LABELS)
            suggestion = t["too_few"] if e["suggested_weight"] is None else f"{e['suggested_weight']:.2f}"
            flag = "" if e["direction_matches_score"] in (True, None) else f" ⚠ {t['direction']}"
            by_source = ", ".join(f"{cell(src)} {k}/{n}" for src, (k, n) in e["fires_on_real_by_source"].items() if k) or "-"
            out.append(f"| `{e['id']}` | {e['claim']} | {e['current_score']:g} | {e['current_weight']:g} | {fired} | {e['likelihood_ratio']:g}{flag} | {suggestion} | {by_source} |")
    out += ["", f"## {t['reading']}", ""] + [f"- {line}" for line in t["reading_text"]]
    if summary.get("modality") == "code":
        out.append(f"- {t['code_hint']}")
    return "\n".join(out) + "\n"


def check_dataset(rows: list[dict], problems: list[str], modality: str = "image") -> str:
    """A quick look at a dataset while it is being collected: counts, gaps and duplicates. Runs nothing."""
    by_label = Counter(r["label"] for r in rows)
    by_source: dict = defaultdict(Counter)
    shas: dict = defaultdict(list)
    for r in rows:
        by_source[r["label"]][r["source"]] += 1
        sha = ac.tree_digest(r["abs"]) if modality == "code" else content_sha(r["abs"])
        if sha:
            shas[sha].append(r)
    lines = [f"{len(rows)} labelled {'code samples' if modality == 'code' else modality + ' files'}"]
    code_lines: dict = defaultdict(list)
    no_log = 0
    if modality == "code":
        for r in rows:
            files, _ = ac.walk_files(r["abs"])
            code_lines[r["label"]].append(sum(1 for _, text in files for line in text.split("\n") if line.strip()))
            no_log += read_git_log(r["abs"]) is None
    words_by_label: dict = defaultdict(list)
    if modality == "text":
        for r in rows:
            try:
                words_by_label[r["label"]].append(len(at.WORD.findall(read_text_file(r["abs"]))))
            except (OSError, ValueError):
                pass
    for label in LABELS:
        sources = ", ".join(f"{cell(src)}: {n}" for src, n in sorted(by_source[label].items())) or "none"
        lines.append(f"  {label}: {by_label[label]}  ({sources})")
    notes = [cell(p) for p in problems]
    if modality == "code":
        medians = {label: statistics.median(v) for label, v in code_lines.items() if v}
        lines += [f"  median non-blank lines, {label}: {medians[label]:g}" for label in LABELS if label in medians]
        if "real" in medians and "generated" in medians and max(medians["real"], medians["generated"]) > 3 * max(1, min(medians["real"], medians["generated"])):
            notes.append("real and generated projects differ more than 3x in size: a tool could look good just by size. Match them (same kind of task, similar size)")
        small = sum(1 for v in code_lines.values() for n in v if n < ac.MIN_CODE_LINES)
        if small:
            notes.append(f"{small} samples have fewer than {ac.MIN_CODE_LINES} non-blank lines: style is not scored for them, only residue")
        if no_log == len(rows):
            notes.append("no sample has a git-log.txt, so commit trailers and history shape are not evaluated; export one per project with the command in references/code-signals.md (no author names)")
        elif no_log:
            notes.append(f"{no_log} of {len(rows)} samples have no git-log.txt; history is evaluated only for the others")
        if by_label["real"] and len(by_source["real"]) < 2:
            notes.append("real projects come from a single source; add different kinds of human code (formatter-heavy, template-based, tutorial-style, and apps that call AI services)")
    if modality == "text":
        medians = {label: statistics.median(v) for label, v in words_by_label.items() if v}
        lines += [f"  median words, {label}: {medians[label]:g}" for label in LABELS if label in medians]
        if "real" in medians and "generated" in medians and max(medians["real"], medians["generated"]) > 2 * max(1, min(medians["real"], medians["generated"])):
            notes.append("real and generated texts differ more than 2x in length: a tool could look good just by length. Match the lengths (and topics) of the two groups")
        short = sum(1 for v in words_by_label.values() for n in v if n < at.MIN_WORDS_STYLE)
        if short:
            notes.append(f"{short} texts have fewer than {at.MIN_WORDS_STYLE} words: style is not scored for them, only residue")
        if by_label["real"] and len(by_source["real"]) < 2:
            notes.append("add human texts from different kinds of writers, including people writing in a second language: that is where false positives are most likely")
    for label in LABELS:
        n = by_label[label]
        if n == 0 and label != "edited":
            notes.append(f"no '{label}' images: both claims need real and generated ones")
        elif 0 < n < TARGET_PER_CLASS:
            notes.append(f"{label}: {n} images; aim for at least {TARGET_PER_CLASS}, so that the 30% test split reaches the {MIN_PER_CLASS} the report needs")
    if by_label["real"] and len(by_source["real"]) < 2:
        notes.append("real images come from a single source; add the channels you will actually check (messaging apps, social networks, screenshots)")
    if by_label["generated"] and len(by_source["generated"]) < 2:
        notes.append("generated images come from a single generator; add at least two or three")
    for group in shas.values():
        if len(group) > 1:
            labels = sorted({r["label"] for r in group})
            notes.append(f"duplicate content{' with different labels' if len(labels) > 1 else ''}: " + ", ".join(cell(r["path"]) for r in group[:3]))
    return "\n".join(lines + (["", "To fix:"] + [f"  - {n}" for n in notes[:30]] if notes else ["", "Nothing to fix."])) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ai-evidence on a labelled set of images or texts.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out-dir", type=Path, help="a new or empty directory for results.json, summary.json and report.md (required unless --check)")
    parser.add_argument("--check", action="store_true", help="only count, find gaps and duplicates in the dataset; runs nothing")
    parser.add_argument("--manifest", type=Path, help="manifest.csv with path,label[,source,split]")
    parser.add_argument("--pixels", action="store_true", help="also run the optional pixel module (needs Pillow and NumPy)")
    parser.add_argument("--augment", choices=["strip"], help="also evaluate cleaned copies: images re-encoded like a messaging app (needs Pillow), texts with assistant residue removed")
    parser.add_argument("--modality", choices=["image", "text", "code"], help="force the kind of dataset (default: inferred from the file extensions; a dataset must not mix both)")
    parser.add_argument("--no-filename", action="store_true", help="drop the file-name evidence (names can leak the label in a sorted dataset)")
    parser.add_argument("--lang", choices=["en", "es"], default="en")
    parser.add_argument("--max-files", type=int, default=20000)
    args = parser.parse_args()

    if not args.dataset.is_dir():
        sys.exit(f"error: {args.dataset} is not a directory")
    if not args.check and args.out_dir is None:
        sys.exit("error: --out-dir is required (or use --check)")
    if args.out_dir and args.out_dir.exists() and (not args.out_dir.is_dir() or any(args.out_dir.iterdir())):
        sys.exit(f"error: {args.out_dir} must not exist yet or must be an empty directory")
    rows, problems = discover(args.dataset, args.manifest, args.max_files, code=args.modality == "code")
    if rows and not args.modality and looks_like_code(rows):
        rows, problems = discover(args.dataset, args.manifest, args.max_files, code=True)
        args.modality = "code"
    if not rows and not args.modality:       # nothing that looks like images or texts: try the project layout
        code_rows, code_problems = discover(args.dataset, args.manifest, args.max_files, code=True)
        if code_rows:
            rows, problems, args.modality = code_rows, code_problems, "code"
    if not rows:
        sys.exit("error: no labelled files found. " + " ".join(problems))
    modality = infer_modality(rows, args.modality)
    kept = [r for r in rows if modality_of(r["abs"]) == modality]
    if len(kept) < len(rows):
        problems.append(f"{len(rows) - len(kept)} files are not {modality}s and were ignored")
    rows = kept
    if not rows:
        sys.exit(f"error: no {modality} files found")
    if modality != "image" and args.pixels:
        sys.exit("error: --pixels applies to images only")
    if args.pixels or (args.augment and modality == "image"):
        try:
            import numpy  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            sys.exit("error: --pixels and --augment need Pillow and NumPy; try: uv run --with pillow --with numpy python evals/evaluate_dataset.py ...")
    if args.check:
        print(check_dataset(rows, problems, modality), end="")
        return
    conditions = ["original"] + (["stripped"] if args.augment == "strip" else [])
    print(f"evaluating {len(rows)} {modality} samples ({', '.join(conditions)})", file=sys.stderr)
    results = evaluate(rows, conditions, args.pixels, args.no_filename, progress=True, modality=modality)
    attach_splits(rows, results)
    summary = summarize(results, conditions, modality)
    summary["settings"] = {"pixels": args.pixels, "augment": args.augment, "no_filename": args.no_filename, "dev_share_percent": DEV_SHARE}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.out_dir / "report.md").write_text(render_markdown(summary, problems, args.lang), encoding="utf-8")
    print(f"wrote {args.out_dir}/report.md, summary.json and results.json")


if __name__ == "__main__":
    main()
