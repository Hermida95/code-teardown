import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders as b
import analyze_text as at
from test_analyze_text import AI_LIKE, HUMAN_LIKE, SPANISH_AI

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import evaluate_dataset as ev  # noqa: E402

SCRIPT = EVALS / "evaluate_dataset.py"
N = 45


def run(*args, expect_ok=True):
    done = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=300)
    if expect_ok:
        assert done.returncode == 0, done.stderr
    return done


def load(out):
    return json.loads((out / "summary.json").read_text()), json.loads((out / "results.json").read_text())


def human(i):
    return f"{HUMAN_LIKE} Note {i}: the tyre was flat again on day {i}, which nobody could have guessed."


def non_native(i):
    return (f"I am writing this text for tell you about my city number {i}. The city is very beautiful and have many people, "
            f"and in the weekend we go to the park with my family and we eat many food. I think is important the family. "
            f"My mother she cook very good and my father he work in the bank. In the summer the weather is hot and we go to the beach, "
            f"is a little far but the children they like it. Last year I visit my cousin in the another city and it was very nice experience for me. "
            f"We walk much and we see the old buildings, the people there is friendly. Next year I want to go again with more time {i}.")


def ai_with_residue(i):
    return f"{AI_LIKE} Draft {i}. See 【4:{i}†source】 for details. As an AI language model, I cannot verify this."


def ai_style_only(i):
    return f"{AI_LIKE} Draft {i}, in today's fast-paced world it is important to note that part {i} plays a crucial role."


@pytest.fixture
def texts(tmp_path):
    root = tmp_path / "texts"
    groups = {"real/native": human, "real/non-native": non_native, "generated/with-residue": ai_with_residue,
              "generated/style-only": ai_style_only, "edited/mixed": lambda i: f"{human(i)}\n\n{AI_LIKE}"}
    for group, make in groups.items():
        folder = root / group
        folder.mkdir(parents=True)
        for i in range(N):
            (folder / f"t{i:03d}.txt").write_text(make(i), encoding="utf-8")
    return root


def test_the_modality_is_inferred_and_a_text_run_produces_a_report(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    summary, results = load(out)
    assert summary["modality"] == "text" and all(r["format"] == "text" for r in results)
    report = (out / "report.md").read_text()
    assert "not a way to judge anyone's work" in report and "By text length" in report and "By language" in report
    assert "with residue from a chat assistant" in report


def test_residue_separates_generated_from_real_and_style_alone_stays_weak(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    summary, results = load(out)
    test = [r for r in results if r["split"] == "test"]
    flagged = lambda r: r["assessment"]["generated"]["band"] in ("strong", "very_strong")
    assert not any(flagged(r) for r in test if r["label"] == "real")
    residue = [r for r in test if r["path"].startswith("generated/with-residue")]
    style = [r for r in test if r["path"].startswith("generated/style-only")]
    assert residue and all(flagged(r) for r in residue)
    assert all(r["assessment"]["generated"]["confidence"] in ("low", "very_low") for r in style)     # the style cap holds
    metrics = summary["conditions"]["original"]["claims"]["generated"]["test"]["metrics"]
    assert metrics["false_positive_rate"]["k"] == 0 and metrics["detection_rate"]["k"] >= len(residue)


def test_fairness_table_shows_where_each_item_fires_on_real_sources(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    summary, _ = load(out)
    table = {e["id"]: e for e in summary["conditions"]["original"]["evidence_on_dev"]}
    assert set(table["tx-citation-markers"]["fires_on_real_by_source"]) == {"native", "non-native"}
    assert table["tx-citation-markers"]["fires_on_real_by_source"]["native"][0] == 0
    assert table["tx-citation-markers"]["suggested_weight"] is not None and table["tx-citation-markers"]["likelihood_ratio"] > 5
    assert "fires on real, by source" in (out / "report.md").read_text()


def test_style_alone_is_flagged_but_never_firmly(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    summary, results = load(out)
    metrics = summary["conditions"]["original"]["claims"]["generated"]["test"]["metrics"]
    style = [r for r in results if r["split"] == "test" and r["path"].startswith("generated/style-only")]
    assert style and all(r["assessment"]["generated"]["confidence"] != "medium" for r in style)
    assert metrics["false_positive_rate_firm"]["k"] == 0 and "at least medium confidence" in (out / "report.md").read_text()
    assert metrics["detection_rate_firm"]["k"] <= metrics["detection_rate"]["k"]


def test_length_and_language_breakdowns(tmp_path, texts):
    es = texts / "generated" / "spanish"
    es.mkdir()
    for i in range(10):
        (es / f"s{i}.txt").write_text(SPANISH_AI * 2 + f" {i}", encoding="utf-8")
    short = texts / "real" / "notes"
    short.mkdir()
    for i in range(10):
        (short / f"n{i}.txt").write_text(f"Quick note {i}: back at six, bring milk please.", encoding="utf-8")
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    block = load(out)[0]["conditions"]["original"]
    assert {"under 150 words", "150-400 words", "400+ words"} == set(block["by_length"])
    assert {"en", "es"} <= set(block["by_language"])
    assert block["by_length"]["under 150 words"]["false_positive_rate"]["n"] > 0


def test_cleaning_the_residue_removes_what_the_detection_relied_on(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out, "--augment", "strip")
    summary, results = load(out)
    original = summary["conditions"]["original"]["claims"]["generated"]["test"]["metrics"]["detection_rate"]
    stripped = summary["conditions"]["stripped"]["claims"]["generated"]["test"]["metrics"]["detection_rate"]
    assert stripped["k"] <= original["k"]
    weight = lambda condition: sum(r["assessment"]["generated"]["total_weight"] for r in results if r["condition"] == condition and r["path"].startswith("generated/with-residue"))
    assert weight("stripped") < weight("original")
    cleaned = [r for r in results if r["condition"] == "stripped" and r["path"].startswith("generated/with-residue")]
    assert cleaned and not any(f["id"] in ("tx-citation-markers", "tx-assistant-phrases") for r in cleaned for f in r["fired"])


def test_strip_residue_unit():
    out = ev.strip_residue("Keep this. See 【4:0†source】. As an AI language model, I cannot. Link https://x.org/a?utm_source=chatgpt.com and [Insert date].")
    assert "【" not in out and "utm_source" not in out and "AI language model" not in out and "[Insert" not in out and "Keep this." in out


def test_mixing_images_and_texts_is_refused_and_pixels_do_not_apply_to_text(tmp_path, texts):
    (texts / "real" / "native" / "photo.png").write_bytes(b.png())
    done = run(texts, "--out-dir", tmp_path / "out", expect_ok=False)
    assert done.returncode != 0 and "mixes images and texts" in done.stderr
    done = run(texts, "--out-dir", tmp_path / "out2", "--modality", "text", "--pixels", expect_ok=False)
    assert done.returncode != 0 and "images only" in done.stderr
    assert run(texts, "--out-dir", tmp_path / "out3", "--modality", "text").returncode == 0


def test_check_on_texts_reports_lengths_and_the_length_confound(tmp_path, texts):
    for i in range(120):
        (texts / "generated" / "style-only" / f"long{i}.txt").write_text((AI_LIKE + " ") * 8 + str(i), encoding="utf-8")
    out = run(texts, "--check").stdout
    assert "labelled text files" in out and "median words, real" in out and "median words, generated" in out
    assert "differ more than 2x in length" in out


def test_check_advises_adding_second_language_writers_when_real_texts_have_one_source(tmp_path):
    root = tmp_path / "one"
    for label in ("real", "generated"):
        (root / label).mkdir(parents=True)
        (root / label / "a.txt").write_text(HUMAN_LIKE if label == "real" else AI_LIKE, encoding="utf-8")
    assert "second language" in run(root, "--check").stdout


def test_spanish_report_has_the_purpose_notice(tmp_path, texts):
    out = tmp_path / "out"
    run(texts, "--out-dir", out, "--lang", "es")
    report = (out / "report.md").read_text()
    assert "No dice nada sobre ninguna persona" in report and "con restos de un asistente de chat" in report


def test_hostile_text_names_and_binary_files_do_not_break_the_run(tmp_path, texts):
    (texts / "real" / "native" / "bin.txt").write_bytes(b"\x00\x01\x02" * 50)
    (texts / "real" / "native" / "x|y`<script>\n.txt").write_text(HUMAN_LIKE + " unique", encoding="utf-8")
    out = tmp_path / "out"
    run(texts, "--out-dir", out)
    report = (out / "report.md").read_text()
    assert "<script>" not in report
    _, results = load(out)
    assert any("error" in r and r["path"].endswith("bin.txt") for r in results)
