import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import analyze_text as at
import score_evidence as se

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

AI_LIKE = (
    "In today's fast-paced digital world, remote work has become a pivotal part of modern life. It is important to note that this shift plays a crucial role in how organizations operate. "
    "Moreover, it fosters a sense of flexibility that many employees value. Furthermore, it is not just a trend, it is a fundamental transformation of the workplace.\n\n"
    "Remote work offers a rich tapestry of benefits. Companies can leverage global talent pools, while employees navigate the complexities of work-life balance more seamlessly. "
    "Additionally, businesses that embrace holistic strategies often see improved engagement — a testament to the power of trust. Overall, the advantages are multifaceted and far-reaching.\n\n"
    "However, it is worth noting that challenges remain. Communication can suffer, and isolation may undermine morale. Organizations must therefore invest in meticulous planning to foster connection. "
    "In conclusion, remote work underscores the need for adaptability in an ever-evolving landscape. It is not only a convenience but also a catalyst for innovation — one that will shape the future of work for years to come."
)
HUMAN_LIKE = (
    "So I finally got round to fixing the bike last weekend. Took me ages. The chain had snapped (which, fine, I knew) but the rear derailleur was also bent, and nobody tells you that until you've "
    "already bought the wrong chain. Twice. My neighbour Dave came over, looked at it for about four seconds, and said \"that's not going to shift\" which was annoying because he was right.\n\n"
    "Anyway. Spent the whole of Saturday afternoon in the garage with the radio on. There's a smell in there I can't describe, oil and old cardboard and something sweet, maybe the paint tins. "
    "Got it done around six. Rode it down to the shop for milk just to check. It clicked a bit on the third gear and I almost cried with relief, honestly, because I'd told my wife it'd be an hour.\n\n"
    "Next job: the brakes. I've been putting that off since March. Dont tell her."
)
SPANISH_AI = (
    "En el mundo actual, la educación digital desempeña un papel crucial en la sociedad. Cabe destacar que su impacto es multifacético. Además, fomenta la autonomía de los estudiantes. "
    "Asimismo, no solo mejora el acceso, sino también la calidad del aprendizaje. Por otro lado, es importante destacar que los desafíos persisten. Sin embargo, las instituciones pueden impulsar "
    "estrategias holísticas. En conclusión, la educación digital es un testimonio de la capacidad de adaptación. Además, requiere una planificación cuidadosa. Por lo tanto, es fundamental invertir. "
    "En resumen, el futuro depende de la colaboración. Asimismo, la tecnología seguirá evolucionando. Cabe señalar que cada contexto es distinto. Además, los docentes necesitan apoyo constante."
)


def ids(result: dict) -> dict:
    return {e["id"]: e for e in result["evidence"]}


# --- residue -------------------------------------------------------------------------------------

@pytest.mark.parametrize("snippet", ["see 【4:0†source】 for details", "claim.[oaicite:0]", "turn0search3 says", "https://x.org/a?utm_source=chatgpt.com"])
def test_citation_markers_are_strong_but_not_conclusive(snippet):
    item = ids(at.analyze(f"Some intro text. {snippet}"))["tx-citation-markers"]
    assert item["weight"] == 0.85 < se.CONCLUSIVE and item["score"] > 9 and item["source"] == "residue"


def test_assistant_phrases_in_both_languages_and_soft_ones_weigh_less():
    assert ids(at.analyze("As an AI language model, I cannot give opinions."))["tx-assistant-phrases"]["weight"] >= 0.8
    assert "tx-assistant-phrases" in ids(at.analyze("Como modelo de lenguaje no tengo opiniones personales."))
    soft = ids(at.analyze("Here is a revised version of your email. Let me know if you'd like me to adjust the tone."))
    assert "tx-assistant-phrases" not in soft and soft["tx-assistant-soft-phrases"]["weight"] <= 0.15


def test_placeholders_and_chat_markdown_in_plain_text():
    assert ids(at.analyze("Dear [Your Name], thanks for [Insert date]."))["tx-placeholders"]["weight"] <= 0.2
    md = "# Title\n\n## One\n**Bold** text\n\n### Two\nMore **bold** text."
    assert "tx-chat-markdown" in ids(at.analyze(md, "note.txt"))
    assert "tx-chat-markdown" not in ids(at.analyze(md, "note.md"))


def test_invisible_characters_are_reported_but_never_scored():
    result = at.analyze("Hello​ world  again.")
    item = ids(result)["tx-invisible-chars"]
    assert item["weight"] == 0 and any("zero-width" in m["value"] for m in result["measurements"])


# --- style -----------------------------------------------------------------------------------------

def test_ai_like_prose_raises_style_items_but_never_past_the_cap():
    result = at.analyze(AI_LIKE)
    found = ids(result)
    assert found["tx-phrase-density"]["score"] >= 7 and "tx-em-dash" in found and "tx-transition-openers" in found
    report = se.build_report(result, [])
    style = sum(e["weight"] for e in report["evidence"] if e["source"] == "style" and e["claim"] == "generated")
    assert style <= se.GROUP_CAPS["style"] + 1e-9
    assert report["assessment"]["generated"]["confidence"] in ("low", "very_low")
    assert report["assessment"]["generated"]["status"] != "conclusive"


def test_human_like_prose_points_the_other_way_and_stays_weak():
    found = ids(at.analyze(HUMAN_LIKE))
    assert found["tx-sentence-variation"]["score"] < 5 and found["tx-irregularities"]["score"] < 5
    assert "tx-phrase-density" not in found and all(e["weight"] <= 0.12 for e in found.values())


def test_spanish_text_uses_the_spanish_lists():
    result = at.analyze(SPANISH_AI * 2)
    assert result["file"]["language"] == "es" and ids(result)["tx-phrase-density"]["score"] >= 7


def test_short_text_and_unknown_language_skip_style_and_say_why():
    assert list(ids(at.analyze("A short note about nothing in particular.")).keys()) == ["tx-skipped"]
    german = "Das ist ein langer deutscher Text über nichts Besonderes. " * 40
    result = at.analyze(german)
    assert result["file"]["language"] == "unknown" and "tx-skipped" in ids(result) and "tx-phrase-density" not in ids(result)


def test_style_shift_needs_a_real_jump_between_sections():
    plain = " ".join(f"The kettle boiled again and again number {i}, nothing else to report today." for i in range(30))
    mixed = plain + " " + AI_LIKE * 2 + " " + plain
    assert "tx-style-shift" in ids(at.analyze(mixed)) and ids(at.analyze(mixed))["tx-style-shift"]["claim"] == "ai_edited"
    assert "tx-style-shift" not in ids(at.analyze(plain * 3))


def test_a_text_without_any_sign_does_not_become_a_verdict():
    report = se.build_report(at.analyze(HUMAN_LIKE), [])
    assert report["assessment"]["generated"]["band"] in ("none", "low", "insufficient", "mixed")
    assert report["assessment"]["generated"]["status"] != "conclusive"


# --- scoring integration -----------------------------------------------------------------------------

def test_group_cap_scales_every_item_of_the_group_proportionally():
    items = [{"id": f"s{i}", "claim": "generated", "score": 7, "weight": 0.2, "title": "t", "detail": "", "where": "", "source": "style"} for i in range(5)]
    report = se.build_report({"evidence": items}, [])
    weights = [e["weight"] for e in report["evidence"]]
    assert sum(weights) == pytest.approx(0.5, abs=0.01) and len(set(weights)) == 1
    assert all(e["scaled_from"] == 0.2 for e in report["evidence"])


def test_visual_observations_are_capped_as_a_group_too(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps([{"id": f"v{i}", "kind": "fabricated_references", "title": "t", "where": "p1", "score": 9, "weight": 0.35} for i in range(6)]))
    report = se.build_report({"evidence": []}, se.load_visual(str(path)))
    assert sum(e["weight"] for e in report["evidence"]) <= se.GROUP_CAPS["visual"] + 1e-9
    assert report["assessment"]["generated"]["confidence"] != "high"


def test_text_report_uses_text_wording_and_the_text_disclaimer(tmp_path):
    out_json, out_html = tmp_path / "e.json", tmp_path / "r.html"
    (tmp_path / "t.txt").write_text(AI_LIKE, encoding="utf-8")
    assert subprocess.run([sys.executable, str(SCRIPTS / "analyze_text.py"), str(tmp_path / "t.txt"), "--out", str(out_json)], capture_output=True).returncode == 0
    done = subprocess.run([sys.executable, str(SCRIPTS / "score_evidence.py"), str(out_json), "--out", str(out_html), "--lang", "en"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    html = out_html.read_text(encoding="utf-8")
    assert "Written by AI?" in html and "Mixed with or polished by AI?" in html and "Text measurements" in html
    assert "words" in html and "accuse anyone of using AI" in html and "<script" not in html
    assert "generated image" not in html and "Pixel measurements" not in html
    spanish = subprocess.run([sys.executable, str(SCRIPTS / "score_evidence.py"), str(out_json), "--out", str(tmp_path / "r_es.html")], capture_output=True, text=True)
    assert spanish.returncode == 0 and "¿Escrito por IA?" in (tmp_path / "r_es.html").read_text(encoding="utf-8")


def test_text_from_the_document_is_escaped_in_the_report():
    payload = "Dear [Insert <script>alert(1)</script>], thanks."   # the matched placeholder is quoted in the report
    report = se.build_report(at.analyze(payload), [])
    html = se.render(report, "en")
    assert "<script>alert" not in html and "&lt;script&gt;" in html


# --- hostile input -------------------------------------------------------------------------------------

def test_pathological_text_is_processed_in_bounded_time():
    nasty = ("not only " * 20000 + "x. " * 20000 + "it is not just " * 10000 + "[" * 20000 + "​" * 50000 + "\n" * 10000)
    start = time.time()
    result = at.analyze(nasty)
    assert time.time() - start < 20 and isinstance(result["evidence"], list)


def test_cli_rejects_binary_files_and_reads_stdin(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x00\x01\x02" * 100)
    bad = subprocess.run([sys.executable, str(SCRIPTS / "analyze_text.py"), str(tmp_path / "x.bin")], capture_output=True, text=True)
    assert bad.returncode != 0 and "binary" in bad.stderr and "Traceback" not in bad.stderr
    good = subprocess.run([sys.executable, str(SCRIPTS / "analyze_text.py"), "-"], input="As an AI language model, I cannot help.", capture_output=True, text=True)
    assert good.returncode == 0 and "tx-assistant-phrases" in good.stdout


def test_cli_refuses_to_overwrite_and_wrong_extension(tmp_path):
    (tmp_path / "t.txt").write_text("hello world", encoding="utf-8")
    out = tmp_path / "e.json"
    run = lambda *a: subprocess.run([sys.executable, str(SCRIPTS / "analyze_text.py"), str(tmp_path / "t.txt"), *map(str, a)], capture_output=True, text=True)
    assert run("--out", out).returncode == 0 and run("--out", out).returncode != 0
    assert run("--out", out, "--force").returncode == 0 and run("--out", tmp_path / "x.txt").returncode != 0


def test_a_large_file_is_read_only_up_to_the_cap(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("word " * (at.MAX_BYTES // 5 + 100000), encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPTS / "analyze_text.py"), str(big)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "only the first 2 MB" in done.stderr
