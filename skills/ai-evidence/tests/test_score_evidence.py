import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders as b
import score_evidence as se

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def item(id, score, weight, claim="generated", **extra):
    return {"id": id, "claim": claim, "score": score, "weight": weight, "title": id, "detail": "", "where": "x",
            "source": "metadata", **extra}


def test_weighted_mean_uses_weights_not_counts():
    agg = se.aggregate([item("a", 9, 0.6), item("b", 1, 0.2), item("c", 1, 0.2)], "generated")
    assert agg["score"] == pytest.approx(5.8, abs=0.05)
    assert agg["status"] == "ok" and agg["confidence"] == "low"  # total weight 1.0 < 1.2


def test_one_conclusive_item_decides_even_when_weak_items_disagree():
    agg = se.aggregate([item("c", 10, 0.97), item("w1", 1, 0.5), item("w2", 2, 0.25)], "generated")
    assert agg["score"] == 10 and agg["band"] == "very_strong" and agg["confidence"] == "high"
    assert agg["status"] == "conclusive"


def test_two_conclusive_items_in_opposite_directions_are_a_conflict_not_an_average():
    agg = se.aggregate([item("ai", 10, 0.95), item("cam", 0, 0.92)], "generated")
    assert agg["status"] == "conflict" and agg["band"] == "conflict" and agg["confidence"] == "very_low"


def test_little_evidence_is_insufficient_whatever_the_score():
    agg = se.aggregate([item("no-meta", 6, 0.1)], "generated")
    assert agg["band"] == "insufficient" and agg["status"] == "insufficient"


def test_no_evidence_for_a_claim_and_informative_items_do_not_score():
    assert se.aggregate([item("a", 9, 0.9, claim="generated")], "ai_edited")["score"] is None
    only_info = se.aggregate([item("i", 5, 0)], "generated")
    assert only_info["score"] is None and only_info["evidence_count"] == 1


def test_claims_are_scored_separately():
    ev = [item("g", 10, 0.97), item("e", 1, 0.5, claim="ai_edited")]
    assert se.aggregate(ev, "generated")["score"] == 10
    assert se.aggregate(ev, "ai_edited")["score"] == 1


@pytest.mark.parametrize("score,band", [(0, "none"), (1.9, "none"), (2, "low"), (4, "mixed"), (6, "strong"), (8, "very_strong"), (10, "very_strong")])
def test_band_edges(score, band):
    assert se.band_for(score) == band


def test_shares_add_up_to_one():
    agg = se.aggregate([item("a", 9, 0.6), item("b", 1, 0.2), item("z", 5, 0)], "generated")
    assert sum(agg["shares"].values()) == pytest.approx(1, abs=0.01) and "z" not in agg["shares"]


# --- visual observations -----------------------------------------------------------------

def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def obs(**over):
    base = {"kind": "anatomy_text_errors", "title": "Six fingers on the left hand", "where": "left hand, centre",
            "score": 9, "weight": 0.9}
    base.update(over)
    return base


def test_visual_weight_is_capped_by_kind(tmp_path):
    items = se.load_visual(write(tmp_path, "v.json", {"observations": [obs()]}))
    assert items[0]["weight"] == 0.35 and items[0]["clamped_from"] == 0.9 and items[0]["source"] == "visual"


def test_known_watermark_may_weigh_more_but_never_conclusive(tmp_path):
    items = se.load_visual(write(tmp_path, "v.json", [obs(kind="known_watermark", weight=1.0)]))
    assert items[0]["weight"] == 0.6 < se.CONCLUSIVE


def test_visual_alone_can_never_decide_the_score(tmp_path):
    items = se.load_visual(write(tmp_path, "v.json", [obs(id=f"v{i}", kind="known_watermark", weight=1) for i in range(1)]))
    assert se.aggregate(items, "generated")["status"] != "conclusive"


@pytest.mark.parametrize("bad", [{"score": 11}, {"score": "9"}, {"weight": 2}, {"kind": "vibes"}, {"where": " "}, {"claim": "fake"}])
def test_invalid_visual_observations_are_rejected(tmp_path, bad):
    with pytest.raises(SystemExit):
        se.load_visual(write(tmp_path, "v.json", [obs(**bad)]))


def test_visual_without_a_place_or_title_is_rejected(tmp_path):
    data = obs()
    del data["where"]
    with pytest.raises(SystemExit):
        se.load_visual(write(tmp_path, "v.json", [data]))


def test_duplicate_ids_and_secrets_are_rejected():
    doc = {"evidence": [item("a", 5, 0.5), item("a", 5, 0.5)]}
    with pytest.raises(SystemExit):
        se.build_report(doc, [])
    token = "sk-" + "A" * 24
    with pytest.raises(SystemExit):
        se.build_report({"evidence": [item("a", 5, 0.5, detail=f"key {token}")]}, [])


# --- rendering and CLI -----------------------------------------------------------------------

def run_cli(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=60)


def test_end_to_end_on_a_png_with_stable_diffusion_metadata(tmp_path):
    image = tmp_path / "00001-123456789-cat.png"
    image.write_bytes(b.png(512, 512, text={"parameters": "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 5"}))
    evidence = tmp_path / "evidence.json"
    assert run_cli(SCRIPTS / "analyze_image.py", image, "--out", evidence).returncode == 0
    report = tmp_path / "r.html"
    done = run_cli(SCRIPTS / "score_evidence.py", evidence, "--out", report, "--json-out", tmp_path / "r.json", "--lang", "en")
    assert done.returncode == 0, done.stderr
    summary = json.loads(done.stdout)
    assert summary["assessment"]["generated"]["score"] == 10
    assert summary["assessment"]["generated"]["confidence"] == "high"
    html = report.read_text(encoding="utf-8")
    assert "Very likely AI" in html and "Stable Diffusion generation parameters" in html
    assert "<script" not in html and "default-src 'none'" in html


def test_spanish_is_the_default_language(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b.png(text={"a": "b"}))
    evidence = tmp_path / "e.json"
    run_cli(SCRIPTS / "analyze_image.py", image, "--out", evidence)
    report = tmp_path / "r.html"
    assert run_cli(SCRIPTS / "score_evidence.py", evidence, "--out", report).returncode == 0
    html = report.read_text(encoding="utf-8")
    assert 'lang="es"' in html and "Sin pruebas suficientes" in html


def test_report_escapes_everything_that_comes_from_the_image(tmp_path):
    payload = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    evidence = {"file": {"name": payload, "format": "png", "sha256": "0" * 64},
                "evidence": [item("x", 9, 0.6, title=payload, detail=payload, quote=payload, where=payload)]}
    visual = [obs(title=payload, where=payload)]
    report = se.build_report(evidence, se.load_visual(write(tmp_path, "v.json", visual)))
    html = se.render(report, "en")
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_cli_refuses_to_overwrite_and_to_write_the_wrong_extension(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b.png())
    evidence = tmp_path / "e.json"
    assert run_cli(SCRIPTS / "analyze_image.py", image, "--out", evidence).returncode == 0
    assert run_cli(SCRIPTS / "analyze_image.py", image, "--out", evidence).returncode != 0
    assert run_cli(SCRIPTS / "analyze_image.py", image, "--out", evidence, "--force").returncode == 0
    assert run_cli(SCRIPTS / "analyze_image.py", image, "--out", tmp_path / "notes.txt").returncode != 0
    report = tmp_path / "r.html"
    assert run_cli(SCRIPTS / "score_evidence.py", evidence, "--out", report).returncode == 0
    assert run_cli(SCRIPTS / "score_evidence.py", evidence, "--out", report).returncode != 0
    assert run_cli(SCRIPTS / "score_evidence.py", evidence, "--out", tmp_path / "r.sh").returncode != 0


def test_malformed_evidence_file_gives_a_clean_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"evidence": [{"id": "a"}]}')
    done = run_cli(SCRIPTS / "score_evidence.py", bad, "--out", tmp_path / "r.html")
    assert done.returncode != 0 and "Traceback" not in done.stderr
