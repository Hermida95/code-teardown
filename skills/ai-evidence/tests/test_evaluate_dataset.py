import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders as b

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import evaluate_dataset as ev  # noqa: E402

SCRIPT = EVALS / "evaluate_dataset.py"
N = 45          # per group, enough for the 30-per-class rule after the 70/30 split


def sd_png(i):
    return b.png(512 + i, 512, text={"parameters": f"cat {i}\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: {i}"})


def camera_jpeg(i):
    exif = b.tiff({0x010F: "Canon", 0x0110: f"EOS {i}"}, {0x829A: (1, 250), 0x829D: (28, 10), 0x8827: 200, 0x9003: "2024:05:01 10:00:00"})
    return b.jpeg(640 + i, 480, exif=exif)


def composite_jpeg(i):
    return b.jpeg(700 + i, 500, app11=b.jumbf(b.IPTC + "compositeWithTrainedAlgorithmicMedia", f"edit {i}"))


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "data"
    groups = {"real/camera": camera_jpeg, "real/whatsapp": lambda i: b.png(300 + i, 200),
              "generated/sd": sd_png, "generated/stripped": lambda i: b.png(900 + i, 700),
              "edited/photoshop": composite_jpeg}
    for group, make in groups.items():
        folder = root / group
        folder.mkdir(parents=True)
        ext = "jpg" if make in (camera_jpeg, composite_jpeg) else "png"
        for i in range(N):
            (folder / f"img{i:03d}.{ext}").write_bytes(make(i))
    return root


def run(*args, expect_ok=True):
    done = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=300)
    if expect_ok:
        assert done.returncode == 0, done.stderr
    return done


def load(out):
    return json.loads((out / "summary.json").read_text()), json.loads((out / "results.json").read_text())


# --- statistics ------------------------------------------------------------------------------

def test_wilson_interval_known_values():
    assert ev.wilson(0, 0) == (0.0, 1.0)
    low, high = ev.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.278, abs=0.005)
    low, high = ev.wilson(5, 10)
    assert (low, high) == (pytest.approx(0.237, abs=0.005), pytest.approx(0.763, abs=0.005))


def test_auc_perfect_reversed_tied_and_empty():
    assert ev.auc([8, 9], [1, 2]) == 1.0 and ev.auc([1, 2], [8, 9]) == 0.0
    assert ev.auc([5, 5], [5, 5]) == 0.5 and ev.auc([], [1]) is None
    assert ev.auc([2, 8], [1, 5]) == pytest.approx(0.75)      # 3 of the 4 pairs rank the positive higher


def test_suggested_weight_rule_of_thumb():
    assert ev.suggested_weight(100) == 0.97 and ev.suggested_weight(0.01) == 0.97
    assert ev.suggested_weight(10) == 0.5 and ev.suggested_weight(1) == 0


def test_split_is_stable_and_roughly_70_30():
    shas = [format(i * 2654435761 % 2 ** 32, "08x") + "0" * 56 for i in range(2000)]
    dev = sum(ev.split_for(s) == "dev" for s in shas)
    assert 1250 < dev < 1550 and ev.split_for(shas[3]) == ev.split_for(shas[3])


# --- end to end ------------------------------------------------------------------------------

def test_headline_numbers_match_the_per_image_results(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, results = load(out)
    test = [r for r in results if r["split"] == "test"]
    metrics = summary["conditions"]["original"]["claims"]["generated"]["test"]["metrics"]
    flagged_real = sum(r["assessment"]["generated"]["band"] in ("strong", "very_strong") for r in test if r["label"] == "real")
    flagged_gen = sum(r["assessment"]["generated"]["band"] in ("strong", "very_strong") for r in test if r["label"] == "generated")
    assert metrics["false_positive_rate"]["k"] == flagged_real == 0
    assert metrics["detection_rate"]["k"] == flagged_gen > 0
    assert metrics["detection_rate"]["n"] == sum(r["label"] == "generated" for r in test)
    # the stripped generated images carry nothing: the tool must abstain, not guess
    assert metrics["abstain_on_positive"]["k"] > 0
    assert metrics["auc_abstain_as_neutral"] > 0.6


def test_edited_images_are_scored_on_the_edited_question(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, _ = load(out)
    metrics = summary["conditions"]["original"]["claims"]["ai_edited"]["test"]["metrics"]
    assert metrics["detection_rate"]["rate"] == 1.0 and metrics["false_positive_rate"]["k"] == 0


def test_evidence_table_uses_dev_only_and_suggests_weights(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, results = load(out)
    table = {e["id"]: e for e in summary["conditions"]["original"]["evidence_on_dev"]}
    sd = table["sd-parameters"]
    dev_sd = sum(1 for r in results if r["split"] == "dev" and r["label"] == "generated" and any(f["id"] == "sd-parameters" for f in r["fired"]))
    assert sd["fired_on"]["generated"] == dev_sd and sd["fired_on"]["real"] == 0
    assert sd["suggested_weight"] is not None and sd["suggested_weight"] > 0.5 and sd["direction_matches_score"] is True
    camera = table["exif-camera"]
    assert camera["likelihood_ratio"] < 1 and camera["direction_matches_score"] is True
    assert table["no-metadata"]["fired_on"]["real"] > 0


def test_false_positives_are_reported_by_source(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, _ = load(out)
    sources = summary["conditions"]["original"]["false_positive_by_source"]
    assert set(sources) == {"camera", "whatsapp"}
    report = (out / "report.md").read_text()
    assert "False-positive rate on real images" in report and "whatsapp" in report


def test_a_tiny_dataset_withholds_suggestions_and_says_so(tmp_path):
    root = tmp_path / "tiny"
    for label, make in (("real", camera_jpeg), ("generated", sd_png)):
        (root / label).mkdir(parents=True)
        for i in range(6):
            (root / label / f"{i}.{'jpg' if label == 'real' else 'png'}").write_bytes(make(i))
    out = tmp_path / "out"
    run(root, "--out-dir", out)
    summary, _ = load(out)
    assert all(e["suggested_weight"] is None for e in summary["conditions"]["original"]["evidence_on_dev"])
    assert "too few images" in (out / "report.md").read_text()


def test_spanish_report(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out, "--lang", "es")
    report = (out / "report.md").read_text()
    assert "Evaluación de ai-evidence" in report and "positivos" in report
    for english in ("files", "positives", "suggested weight", "with metadata", "| label |", "| source |"):
        assert english not in report, english


def test_duplicates_with_different_labels_are_flagged(tmp_path, dataset):
    (dataset / "generated" / "sd" / "copy.jpg").write_bytes((dataset / "real" / "camera" / "img000.jpg").read_bytes())
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, _ = load(out)
    assert any(d["labels"] == ["generated", "real"] for d in summary["duplicates"])
    assert "duplicate content with different labels" in (out / "report.md").read_text()
    run(dataset, "--out-dir", tmp_path / "out_es", "--lang", "es")
    assert "contenido duplicado con etiquetas distintas" in (tmp_path / "out_es" / "report.md").read_text()


def test_file_name_evidence_can_be_dropped(tmp_path):
    root = tmp_path / "names"
    (root / "generated").mkdir(parents=True)
    (root / "real").mkdir()
    for i in range(3):
        (root / "generated" / f"ChatGPT Image {i}.png").write_bytes(b.png(100 + i, 100, text={"a": "b"}))
        (root / "real" / f"holiday{i}.png").write_bytes(b.png(100 + i, 100, text={"a": "b"}))
    for flag, expected in ((None, True), ("--no-filename", False)):
        out = tmp_path / f"out{bool(flag)}"
        run(root, "--out-dir", out, *([flag] if flag else []))
        _, results = load(out)
        assert any(f["id"] == "filename-hint" for r in results for f in r["fired"]) is expected


def test_one_unreadable_file_does_not_stop_the_run(tmp_path, dataset):
    (dataset / "real" / "camera" / "broken.jpg").write_bytes(b"\x00" * 10)
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    _, results = load(out)
    assert all("error" not in r for r in results)          # garbage bytes are still "an image with no evidence"
    assert any(r["path"].endswith("broken.jpg") for r in results)


# --- manifest and safety ---------------------------------------------------------------------

def test_manifest_labels_sources_and_splits_are_respected(tmp_path, dataset):
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "label", "source", "split"])
        for i in range(4):
            writer.writerow([f"real/camera/img{i:03d}.jpg", "human", "my phone", "test"])
            writer.writerow([f"generated/sd/img{i:03d}.png", "ai", "sdxl", "dev"])
    out = tmp_path / "out"
    run(dataset, "--manifest", manifest, "--out-dir", out)
    _, results = load(out)
    assert {r["label"] for r in results} == {"real", "generated"}
    assert {(r["label"], r["split"], r["source"]) for r in results} == {("real", "test", "my phone"), ("generated", "dev", "sdxl")}


def test_manifest_cannot_point_outside_the_dataset(tmp_path, dataset):
    outside = tmp_path / "secret.png"
    outside.write_bytes(b.png())
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(f"path,label\n../secret.png,real\n{outside},real\nreal/camera/img000.jpg,martian\nreal/camera/img001.jpg,real\n")
    out = tmp_path / "out"
    run(dataset, "--manifest", manifest, "--out-dir", out)
    _, results = load(out)
    assert [r["path"] for r in results] == ["real/camera/img001.jpg"]
    report = (out / "report.md").read_text()
    assert "not a file inside the dataset" in report and "unknown label" in report


def test_manifest_without_the_required_columns_is_refused(tmp_path, dataset):
    manifest = tmp_path / "m.csv"
    manifest.write_text("file,kind\nx,y\n")
    done = run(dataset, "--manifest", manifest, "--out-dir", tmp_path / "out", expect_ok=False)
    assert done.returncode != 0 and "Traceback" not in done.stderr


def test_symlinks_and_unknown_folders_are_ignored(tmp_path, dataset):
    (tmp_path / "private.png").write_bytes(b.png())
    (dataset / "real" / "link.png").symlink_to(tmp_path / "private.png")
    (dataset / "misc").mkdir()
    (dataset / "misc" / "x.png").write_bytes(b.png())
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    _, results = load(out)
    assert not any("link.png" in r["path"] or r["path"].startswith("misc") for r in results)
    assert "'misc' is not a label" in (out / "report.md").read_text()


def test_the_output_directory_must_be_new_or_empty(tmp_path, dataset):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    done = run(dataset, "--out-dir", out, expect_ok=False)
    assert done.returncode != 0 and (out / "keep.txt").read_text() == "mine"
    assert run(dataset, "--out-dir", tmp_path / "fresh").returncode == 0


def test_an_empty_dataset_is_an_error_not_a_report(tmp_path):
    (tmp_path / "empty").mkdir()
    done = run(tmp_path / "empty", "--out-dir", tmp_path / "out", expect_ok=False)
    assert done.returncode != 0 and "no labelled images" in done.stderr


# --- optional modules --------------------------------------------------------------------------

def test_stripped_copies_lose_the_metadata_evidence(tmp_path, dataset):
    pytest.importorskip("PIL")
    for folder in ("real/camera", "generated/sd", "generated/stripped", "real/whatsapp", "edited/photoshop"):
        for extra in sorted((dataset / folder).iterdir())[20:]:
            extra.unlink()                       # keep it quick; Pillow must be able to decode what is left
    # synthetic PNG/JPEG stubs are not decodable images, so use real pixels for this test
    import pixel_builders as pb
    for folder, text in (("generated/sd", True), ("real/camera", False)):
        for index, file in enumerate(sorted((dataset / folder).iterdir())):
            pixels = pb.noisy(3.0, n=300, seed=index + 1)
            if text:
                from PIL import Image, PngImagePlugin
                info = PngImagePlugin.PngInfo()
                info.add_text("parameters", f"Steps: 20, Sampler: Euler, CFG scale: 7, Seed: {index}")
                import numpy as np
                Image.fromarray(np.clip(np.round(pixels), 0, 255).astype("uint8")).convert("RGB").save(file, "PNG", pnginfo=info)
            else:
                pb.save(pixels, file, quality=90)
    out = tmp_path / "out"
    run(dataset, "--out-dir", out, "--augment", "strip", "--manifest", _manifest_for(dataset, tmp_path))
    summary, _ = load(out)
    original = summary["conditions"]["original"]["claims"]["generated"]["dev"]["metrics"]["detection_rate"]
    stripped = summary["conditions"]["stripped"]["claims"]["generated"]["dev"]["metrics"]["detection_rate"]
    assert original["rate"] > 0.9 and stripped["rate"] == 0


def _manifest_for(dataset, tmp_path):
    manifest = tmp_path / "m.csv"
    with manifest.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "label", "source", "split"])
        for folder, label in (("generated/sd", "generated"), ("real/camera", "real")):
            for file in sorted((dataset / folder).iterdir()):
                writer.writerow([str(file.relative_to(dataset)), label, folder.split("/")[1], "dev"])
    return manifest


def test_pixel_module_can_be_included(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import pixel_builders as pb
    root = tmp_path / "px"
    for label, sigma in (("real", 3.0), ("generated", 0.0)):
        (root / label).mkdir(parents=True)
        for i in range(3):
            pb.save(pb.noisy(sigma, seed=i + 1) if sigma else pb.scene(seed=i + 1), root / label / f"{i}.png")
    out = tmp_path / "out"
    run(root, "--out-dir", out, "--pixels")
    _, results = load(out)
    fired = {f["id"] for r in results for f in r["fired"]}
    assert "px-noise-level" in fired
    assert all(f["weight"] <= 0.25 for r in results for f in r["fired"] if f["id"].startswith("px-"))


def test_pixels_flag_without_dependencies_fails_with_a_hint(tmp_path, dataset):
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "numpy.py").write_text("raise ImportError('blocked for the test')")
    done = subprocess.run([sys.executable, str(SCRIPT), str(dataset), "--out-dir", str(tmp_path / "out"), "--pixels"],
                          capture_output=True, text=True, timeout=60, env={"PYTHONPATH": str(blocker), "PATH": ""})
    assert done.returncode != 0 and "uv run --with pillow --with numpy" in done.stderr


# --- hardening: bounded hashing, safe re-encoding, sanitised report -----------------------------

def test_content_sha_is_chunked_and_matches_the_plain_hash(tmp_path):
    import hashlib
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024 + 17))
    assert ev.content_sha(big) == hashlib.sha256(big.read_bytes()).hexdigest()
    assert ev.content_sha(tmp_path / "missing.bin") is None


def test_files_over_the_cap_are_never_read_when_assigning_a_split(tmp_path, monkeypatch):
    big = tmp_path / "big.png"
    big.write_bytes(b.png() + b"\x00" * 400)
    monkeypatch.setattr(ev, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("read a file over the cap")))
    assert ev.content_sha(big) is None
    rows = [{"path": "big.png", "abs": big, "label": "real", "source": "x", "split": None}]
    results = [{"path": "big.png", "label": "real", "source": "x", "condition": "original", "error": "too large"}]
    ev.attach_splits(rows, results)
    assert results[0]["split"] in ("dev", "test") and results[0]["content_sha256"] is None
    assert ev.duplicates(results) == []


def test_names_from_the_dataset_cannot_inject_markdown_or_html_into_the_report(tmp_path):
    root = tmp_path / "hostile"
    nasty_source = "x|y`z<script>alert(1)</script>"
    for label in ("real", "generated"):
        folder = root / label / nasty_source
        folder.mkdir(parents=True)
        for i in range(12):      # enough that some real images land in the test split, where the by-source table is built
            (folder / f"a\nb|{i}[x](evil).png").write_bytes(b.png(100 + i, 100, text={"a": "b"}))
    (root / "real" / nasty_source / "dup\n# injected heading.png").write_bytes((root / "real" / nasty_source / "a\nb|0[x](evil).png").read_bytes())
    (root / "unknown<img src=x>").mkdir()
    out = tmp_path / "out"
    run(root, "--out-dir", out)
    report = (out / "report.md").read_text()
    assert "<script>" not in report and "<img" not in report
    assert "\n# injected heading" not in report and "[x](evil)" not in report
    table_rows = [line for line in report.splitlines() if line.startswith("| x")]
    assert table_rows and all(line.count("\n") == 0 and line.endswith("|") for line in table_rows)


def test_cell_escapes_every_markdown_delimiter_and_bounds_length():
    text = ev.cell("a|b`c<d>&[e](f)\n" + "z" * 500)
    assert "|" not in text.replace("\\|", "") and "`" not in text and "<" not in text and ">" not in text
    assert "\n" not in text and len(text) < 260


def test_a_huge_image_is_refused_before_it_is_re_encoded(tmp_path):
    pytest.importorskip("PIL")
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00" * 100)) + chunk(b"IEND", b""))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(Exception) as caught:
        ev.stripped_copy(bomb, scratch)
    assert "pixels" in str(caught.value) or "decompression" in str(caught.value).lower()
    assert list(scratch.iterdir()) == []


# --- --check ----------------------------------------------------------------------------------

def test_check_counts_sources_and_flags_gaps_without_running_anything(tmp_path, dataset):
    (dataset / "generated" / "sd" / "copy.jpg").write_bytes((dataset / "real" / "camera" / "img000.jpg").read_bytes())
    done = run(dataset, "--check")
    assert "225 labelled images" not in done.stdout and "226 labelled images" in done.stdout
    assert "real: 90" in done.stdout and "camera: 45" in done.stdout and "whatsapp: 45" in done.stdout
    assert "aim for at least 100" in done.stdout                      # 45 and 90 are below the target
    assert "duplicate content with different labels" in done.stdout
    assert not any(tmp_path.glob("out*"))                              # nothing is written


def test_check_on_a_single_source_dataset_says_what_to_add(tmp_path):
    root = tmp_path / "one"
    for label in ("real", "generated"):
        (root / label).mkdir(parents=True)
        (root / label / "a.png").write_bytes(b.png(100 if label == "real" else 120, 100, text={"a": "b"}))
    out = run(root, "--check").stdout
    assert "single source" in out and "single generator" in out


def test_check_needs_no_out_dir_but_a_normal_run_does(tmp_path, dataset):
    assert run(dataset, "--check").returncode == 0
    done = run(dataset, expect_ok=False)
    assert done.returncode != 0 and "--out-dir is required" in done.stderr
