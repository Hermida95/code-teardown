import json
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

import analyze_pixels as ap  # noqa: E402
import pixel_builders as pb  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def items(path) -> dict:
    return {e["id"]: e for e in ap.analyze(path)["evidence"]}


def test_a_clean_image_is_reported_as_almost_noise_free(tmp_path):
    pb.save(pb.scene(), tmp_path / "a.png")
    noise = items(tmp_path / "a.png")["px-noise-level"]
    assert noise["score"] > 5 and 0 < noise["weight"] <= ap.PIXEL_CAP


def test_sensor_like_noise_is_measured_and_points_away_from_ai(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.png")
    result = ap.analyze(tmp_path / "a.png")
    noise = {e["id"]: e for e in result["evidence"]}["px-noise-level"]
    sigma = float(next(m["value"] for m in result["measurements"] if "Noise level" in m["label"]["en"]))
    assert 2.4 < sigma < 3.6          # the estimate recovers the sigma that was added
    assert noise["score"] < 5 and noise["weight"] > 0


def test_noise_between_the_thresholds_is_neutral(tmp_path):
    pb.save(pb.noisy(1.3), tmp_path / "a.png")
    assert items(tmp_path / "a.png")["px-noise-level"]["weight"] == 0


def test_a_patch_with_different_noise_is_flagged_as_edited_but_uniform_noise_is_not(tmp_path):
    pixels = pb.noisy(1.5)
    pixels[200:420, 300:560] += np.random.default_rng(9).normal(0, 9, (220, 260))
    pb.save(pixels, tmp_path / "spliced.png")
    flagged = items(tmp_path / "spliced.png")["px-noise-inconsistent"]
    assert flagged["claim"] == "ai_edited" and flagged["weight"] > 0
    pb.save(pb.noisy(1.5), tmp_path / "plain.png")
    assert items(tmp_path / "plain.png")["px-noise-inconsistent"]["weight"] == 0


@pytest.mark.parametrize("name,pattern", [
    ("checker", lambda shape: (np.indices(shape).sum(axis=0) % 2) * 2.4 - 1.2),     # period 2 on both axes
    ("stripes", lambda shape: (np.indices(shape)[1] % 4 < 2) * 2.0 - 1.0),          # period 4 on one axis only
])
def test_periodic_patterns_are_found_even_when_they_vary_along_one_axis(tmp_path, name, pattern):
    pixels = pb.noisy(2.0) + pattern((768, 768))
    pb.save(pixels, tmp_path / f"{name}.png")
    found = items(tmp_path / f"{name}.png")["px-periodic-artifact"]
    assert found["weight"] > 0 and found["score"] > 5


def test_plain_noise_has_no_periodic_pattern_and_upscaling_does_not_make_one(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.png")
    assert items(tmp_path / "a.png")["px-periodic-artifact"]["weight"] == 0
    small = pb.save(pb.noisy(3.0, n=384, seed=3), tmp_path / "small.png")
    from PIL import Image
    Image.open(tmp_path / "small.png").resize((768, 768), Image.BILINEAR).save(tmp_path / "up.png")
    assert items(tmp_path / "up.png")["px-periodic-artifact"]["weight"] == 0


def test_jpeg_block_grid_peaks_are_not_scored_as_ai(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.jpg", quality=75)
    assert items(tmp_path / "a.jpg")["px-periodic-artifact"]["weight"] == 0


def test_jpeg_quality_is_estimated(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.jpg", quality=95)
    assert "~95" in [m["value"] for m in ap.analyze(tmp_path / "a.jpg")["measurements"]][0]


def test_low_quality_jpeg_suppresses_the_scored_noise_and_ela_evidence(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.jpg", quality=40)
    found = items(tmp_path / "a.jpg")
    assert found["px-jpeg-quality"]["weight"] == 0
    assert found["px-noise-level"]["weight"] == 0 and "px-ela" not in found


def test_error_level_analysis_flags_a_patch_with_another_compression_history(tmp_path):
    base = pb.jpeg_roundtrip(pb.noisy(2.5, seed=2), 70)
    other = pb.noisy(2.5, seed=50)
    base[224:432, 288:544] = other[224:432, 288:544]
    pb.save(base, tmp_path / "edit.jpg", quality=95)
    assert items(tmp_path / "edit.jpg")["px-ela"]["weight"] > 0
    pb.save(pb.noisy(2.5, seed=2), tmp_path / "plain.jpg", quality=95)
    assert items(tmp_path / "plain.jpg")["px-ela"]["weight"] == 0


def test_small_images_and_graphics_are_skipped_not_scored(tmp_path):
    pb.save(pb.noisy(3.0, n=200), tmp_path / "small.png")
    assert list(items(tmp_path / "small.png")) == ["px-skipped"]
    from PIL import Image
    palette = Image.new("P", (400, 400))
    palette.putpalette([i // 3 for i in range(768)])
    palette.save(tmp_path / "graphic.png")
    assert list(items(tmp_path / "graphic.png")) == ["px-skipped"]


def test_a_black_and_white_photo_is_not_mistaken_for_a_graphic(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "bw.png")        # R == G == B, so at most 256 distinct colours
    assert "px-skipped" not in items(tmp_path / "bw.png")


def test_no_pixel_evidence_ever_weighs_more_than_the_cap(tmp_path):
    for index, pixels in enumerate([pb.scene(), pb.noisy(3.0), pb.noisy(2.0) + (np.indices((768, 768)).sum(axis=0) % 2) * 2.4 - 1.2]):
        pb.save(pixels, tmp_path / f"{index}.png")
        assert all(e["weight"] <= ap.PIXEL_CAP and e["source"] == "pixels" for e in ap.analyze(tmp_path / f"{index}.png")["evidence"])


# --- hostile input: clean failures, no tracebacks -------------------------------------------------

def run(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=120)


def test_not_an_image_and_truncated_files_fail_cleanly(tmp_path):
    (tmp_path / "x.png").write_bytes(b"this is not an image")
    done = run(SCRIPTS / "analyze_pixels.py", tmp_path / "x.png")
    assert done.returncode != 0 and "Traceback" not in done.stderr and "cannot read the image" in done.stderr
    pb.save(pb.noisy(3.0), tmp_path / "ok.png")
    data = (tmp_path / "ok.png").read_bytes()
    (tmp_path / "cut.png").write_bytes(data[: len(data) // 2])
    done = run(SCRIPTS / "analyze_pixels.py", tmp_path / "cut.png")
    assert "Traceback" not in done.stderr


def test_an_image_that_declares_a_huge_size_is_refused_before_decoding(tmp_path):
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    bomb = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 100)) + chunk(b"IEND", b""))
    (tmp_path / "bomb.png").write_bytes(bomb)
    done = run(SCRIPTS / "analyze_pixels.py", tmp_path / "bomb.png")
    assert done.returncode != 0 and "Traceback" not in done.stderr and "too large to analyze safely" in done.stderr


def test_missing_dependencies_exit_with_status_3_and_an_explanation(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.png")
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "numpy.py").write_text("raise ImportError('blocked for the test')")
    done = subprocess.run([sys.executable, str(SCRIPTS / "analyze_pixels.py"), str(tmp_path / "a.png")],
                          capture_output=True, text=True, timeout=60, env={"PYTHONPATH": str(blocker), "PATH": ""})
    assert done.returncode == 3 and "uv run --with pillow --with numpy" in done.stderr


def test_cli_writes_json_and_refuses_to_overwrite(tmp_path):
    pb.save(pb.noisy(3.0), tmp_path / "a.png")
    out = tmp_path / "pixels.json"
    assert run(SCRIPTS / "analyze_pixels.py", tmp_path / "a.png", "--out", out).returncode == 0
    assert json.loads(out.read_text())["tool"] == "analyze_pixels"
    assert run(SCRIPTS / "analyze_pixels.py", tmp_path / "a.png", "--out", out).returncode != 0
    assert run(SCRIPTS / "analyze_pixels.py", tmp_path / "a.png", "--out", tmp_path / "x.txt").returncode != 0
