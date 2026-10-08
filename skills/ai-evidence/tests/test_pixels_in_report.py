import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders as b
import score_evidence as se

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def px_item(id="px-noise-level", claim="generated", score=9, weight=0.9, **extra):
    return {"id": id, "claim": claim, "score": score, "weight": weight, "title": id, "detail": "d", "where": "w",
            "source": "pixels", **extra}


def pixels_doc(items, **extra):
    return {"tool": "analyze_pixels", "evidence": items, "measurements": [], "limits": [], "coverage": {}, **extra}


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_pixel_weights_are_capped_even_if_the_file_claims_more(tmp_path):
    pixels = se.load_pixels(write(tmp_path, "p.json", pixels_doc([px_item(weight=0.95)])))
    item = pixels["evidence"][0]
    assert item["weight"] == se.PIXEL_CAP and item["clamped_from"] == 0.95


def test_pixel_evidence_alone_can_never_decide_a_result(tmp_path):
    pixels = se.load_pixels(write(tmp_path, "p.json", pixels_doc([px_item(weight=1.0), px_item("px-ela", "ai_edited", 10, 1.0)])))
    report = se.build_report({"evidence": []}, [], pixels)
    assert report["assessment"]["generated"]["status"] != "conclusive"
    assert report["assessment"]["generated"]["confidence"] in ("very_low", "low")


def test_pixel_items_cannot_outvote_a_conclusive_metadata_item(tmp_path):
    meta = {"evidence": [{"id": "sd", "claim": "generated", "score": 10, "weight": 0.97, "title": "t", "detail": "", "where": ""}]}
    pixels = se.load_pixels(write(tmp_path, "p.json", pixels_doc([px_item(score=0, weight=1.0)])))
    assert se.build_report(meta, [], pixels)["assessment"]["generated"]["score"] == 10


@pytest.mark.parametrize("bad", [{"tool": "other"}, {"evidence": "x"}])
def test_a_pixels_file_from_somewhere_else_is_rejected(tmp_path, bad):
    doc = pixels_doc([px_item()])
    doc.update(bad)
    with pytest.raises(SystemExit):
        se.load_pixels(write(tmp_path, "p.json", doc))


def test_ids_must_use_the_pixel_prefix_so_they_cannot_collide_with_metadata(tmp_path):
    with pytest.raises(SystemExit):
        se.load_pixels(write(tmp_path, "p.json", pixels_doc([px_item(id="sd-parameters")])))


def test_the_pixel_notes_replace_the_not_run_notes_and_measurements_show(tmp_path):
    evidence = {"evidence": [], "coverage": {"not_checked": [{"en": "pixel-level forensics", "es": "x", "id": "pixels"}, {"en": "other", "es": "otro"}]},
                "limits": [{"en": "pixel analysis was not run", "es": "x", "id": "pixels"}, {"en": "keep me", "es": "x"}]}
    doc = pixels_doc([px_item(weight=0.1)], measurements=[{"label": {"en": "Noise σ", "es": "Ruido σ"}, "value": "3.01"}],
                     limits=[{"en": "uncalibrated heuristics", "es": "heurísticas sin calibrar"}])
    report = se.build_report(evidence, [], se.load_pixels(write(tmp_path, "p.json", doc)))
    texts = [x["en"] for x in report["coverage"]["not_checked"]]
    assert texts == ["other"]
    assert [x["en"] for x in report["limits"]] == ["keep me", "uncalibrated heuristics"]
    html = se.render(report, "en")
    assert "Pixel measurements" in html and "3.01" in html and "uncalibrated heuristics" in html and "pixels" in html


def test_report_without_pixels_keeps_the_not_run_note(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b.png(text={"a": "b"}))
    evidence = tmp_path / "e.json"
    subprocess.run([sys.executable, str(SCRIPTS / "analyze_image.py"), str(image), "--out", str(evidence)], check=True, capture_output=True)
    report = se.build_report(json.loads(evidence.read_text()), [], None)
    assert any(x.get("id") == "pixels" for x in report["coverage"]["not_checked"])


def test_end_to_end_with_the_pixel_module(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import pixel_builders as pb
    image = tmp_path / "photo.png"
    pb.save(pb.noisy(3.0), image)
    evidence, pixels, report = tmp_path / "e.json", tmp_path / "p.json", tmp_path / "r.html"
    for args in ([SCRIPTS / "analyze_image.py", image, "--out", evidence], [SCRIPTS / "analyze_pixels.py", image, "--out", pixels]):
        assert subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True).returncode == 0
    done = subprocess.run([sys.executable, str(SCRIPTS / "score_evidence.py"), str(evidence), "--pixels", str(pixels),
                           "--out", str(report), "--lang", "en"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    html = report.read_text(encoding="utf-8")
    assert "Noise level" in html and "pixel-level forensics" not in html and "<script" not in html
    summary = json.loads(done.stdout)
    assert summary["assessment"]["generated"]["status"] != "conclusive"
