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
