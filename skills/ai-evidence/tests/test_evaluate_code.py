import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders as b
from test_analyze_code import documented_module, human_module, narrating_module

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))
import evaluate_dataset as ev  # noqa: E402

SCRIPT = EVALS / "evaluate_dataset.py"
N = 40
TRAILERS = "".join(f"\x1e{i:040x}\x1f2025-03-0{i % 9 + 1}T10:00:00+00:00\x1ffeat: part {i}\x1fBody.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\nCo-Authored-By: Claude <noreply@anthropic.com>\x1d\n\n5\t0\ta.py\n" for i in range(8))


def run(*args, expect_ok=True):
    done = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=600)
    if expect_ok:
        assert done.returncode == 0, done.stderr
    return done


def load(out):
    return json.loads((out / "summary.json").read_text()), json.loads((out / "results.json").read_text())


def project(root, label, source, name, files):
    base = root / label / source / name
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return base


def human_project(i):
    return {"src/main.py": human_module(60 + i) + f"\n# local note {i}\n", "README.md": f"# Project {i}\n\nA small tool.\n"}


def pasted_project(i):
    return {"src/main.py": narrating_module(50 + i) + "\n# ... rest of the code remains the same\n", "README.md": f"# P{i}\n\nSee 【4:{i}†source】.\n", "CLAUDE.md": f"rules {i}"}


def clean_ai_project(i):
    return {"src/main.py": narrating_module(55 + i) + f"\n# variant {i}\n", "README.md": f"# Tool {i}\n"}


def assisted_project(i):
    return {"src/main.py": human_module(60 + i) + f"\n# assisted {i}\n", "CLAUDE.md": f"rules {i}", "git-log.txt": TRAILERS}


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "code"
    groups = [("real", "formatter-heavy", human_project), ("real", "tutorial-style", lambda i: {"src/main.py": documented_module() + f"\n# tut {i}\n"}),
              ("generated", "pasted", pasted_project), ("generated", "api-clean", clean_ai_project), ("edited", "assistant", assisted_project)]
    for label, source, make in groups:
        for i in range(N):
            project(root, label, source, f"p{i:03d}", make(i))
    return root


def test_code_datasets_are_recognised_even_though_each_project_has_a_readme(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, results = load(out)
    assert summary["modality"] == "code" and all(r["format"] == "code" for r in results)
    assert {r["label"] for r in results} == {"real", "generated", "edited"} and len(results) == 5 * N
    report = (out / "report.md").read_text()
    assert "not a way to judge anyone's work" in report and "By code size" in report and "By main language" in report
    assert "with traces of an assistant" in report and "human projects written with formatters" in report


def test_residue_is_detected_and_style_alone_is_flagged_but_not_firmly(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, results = load(out)
    test = [r for r in results if r["split"] == "test"]
    flagged = lambda r, c="generated": r["assessment"][c]["band"] in ("strong", "very_strong")
    assert not any(flagged(r) for r in test if r["label"] == "real")
    pasted = [r for r in test if "/pasted/" in r["path"]]
    assert pasted and all(flagged(r) for r in pasted)
    api_clean = [r for r in test if "/api-clean/" in r["path"]]
    assert all(r["assessment"]["generated"]["confidence"] != "medium" for r in api_clean)       # style stays under its cap
    assisted = [r for r in test if "/assistant/" in r["path"]]
    assert assisted and all(flagged(r, "ai_edited") for r in assisted)
    metrics = summary["conditions"]["original"]["claims"]["ai_edited"]["test"]["metrics"]
    assert metrics["false_positive_rate"]["k"] == 0 and metrics["detection_rate"]["rate"] == 1.0


def test_git_logs_are_read_from_the_project_folder_and_git_is_never_run(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    _, results = load(out)
    assisted = [r for r in results if "/assistant/" in r["path"]]
    assert all(r["has_log"] for r in assisted) and not any(r["has_log"] for r in results if "/pasted/" in r["path"])
    assert all(any(f["id"] == "cd-commit-trailers" for f in r["fired"]) for r in assisted)
    source = SCRIPT.read_text(encoding="utf-8")
    assert "subprocess" not in source and "os.system" not in source


def test_fairness_table_and_breakdowns(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    summary, _ = load(out)
    block = summary["conditions"]["original"]
    table = {e["id"]: e for e in block["evidence_on_dev"]}
    assert set(table["cd-docstring-uniformity"]["fires_on_real_by_source"]) == {"formatter-heavy", "tutorial-style"}
    assert table["cd-docstring-uniformity"]["fires_on_real_by_source"]["tutorial-style"][0] > 0          # the tutorial-style humans trip it: that is what the table is for
    assert table["cd-ai-tool-files"]["fires_on_real_by_source"]["formatter-heavy"][0] == 0
    assert {"under 200 lines", "200-1000 lines", "1000+ lines"} == set(block["by_size"]) and "Python" in block["by_language"]
    assert "by_length" not in block


def test_cleaning_removes_the_assistant_files_trailers_and_residue_comments(tmp_path, dataset):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in dataset.rglob("*") if p.is_file()}
    out = tmp_path / "out"
    run(dataset, "--out-dir", out, "--augment", "strip")
    summary, results = load(out)
    cleaned = [r for r in results if r["condition"] == "stripped"]
    gone = {"cd-ai-tool-files", "cd-commit-trailers", "cd-llm-placeholders", "cd-readme-citation-markers"}
    assert cleaned and not any(f["id"] in gone for r in cleaned for f in r["fired"])
    original = summary["conditions"]["original"]["claims"]["ai_edited"]["test"]["metrics"]["detection_rate"]
    stripped = summary["conditions"]["stripped"]["claims"]["ai_edited"]["test"]["metrics"]["detection_rate"]
    assert stripped["k"] < original["k"]
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in dataset.rglob("*") if p.is_file()}   # the dataset itself is untouched


def test_strip_code_copy_unit(tmp_path):
    src = project(tmp_path, "real", "x", "p", {"a.py": "```python\nx = 1\n# ... rest of the code remains the same\ny = 2\n```\n", "README.md": "Hi. As an AI language model, I cannot. Keep."})
    out = tmp_path / "copy"
    out.mkdir()
    ev.strip_code_copy(src, out)
    text = (out / "a.py").read_text()
    assert "```" not in text and "rest of the code" not in text and "x = 1" in text and "y = 2" in text
    assert "AI language model" not in (out / "README.md").read_text()
    assert ev.strip_trailers("keep\nCo-Authored-By: Claude <x@y>\nalso keep") == "keep\nalso keep"


def test_single_file_samples_and_manifest_with_folders(tmp_path, dataset):
    (dataset / "generated" / "snippets").mkdir()
    for i in range(3):
        (dataset / "generated" / "snippets" / f"s{i}.py").write_text(narrating_module(20 + i))
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    _, results = load(out)
    assert any(r["path"].endswith("s0.py") and r["source"] == "snippets" for r in results)
    manifest = tmp_path / "m.csv"
    with manifest.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "label", "source", "split"])
        for i in range(3):
            writer.writerow([f"real/formatter-heavy/p{i:03d}", "human", "mine", "test"])
            writer.writerow([f"generated/pasted/p{i:03d}", "ai_written", "gpt", "dev"])
        writer.writerow(["../outside", "real", "", ""])
        writer.writerow(["real/formatter-heavy/p000/src/main.py", "bogus", "", ""])
    out2 = tmp_path / "out2"
    run(dataset, "--modality", "code", "--manifest", manifest, "--out-dir", out2)
    _, results2 = load(out2)
    assert {(r["label"], r["split"], r["source"]) for r in results2} == {("real", "test", "mine"), ("generated", "dev", "gpt")}
    report = (out2 / "report.md").read_text()
    assert "not a project folder or source file inside the dataset" in report and "unknown label" in report


def test_the_harness_reads_code_but_never_runs_it_and_never_follows_links_out(tmp_path, dataset):
    marker, secret = tmp_path / "RAN", tmp_path / "outside"
    secret.mkdir()
    (secret / "leak.py").write_text("x = 1\n" * 300)
    base = project(dataset, "real", "formatter-heavy", "trap", {"a.py": f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n" + human_module(40)})
    (base / "link").symlink_to(secret)
    (base / "blob.py").write_bytes(b"\x00\x01" * 500)
    (dataset / "real" / "formatter-heavy" / "linked").symlink_to(secret)
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    _, results = load(out)
    assert not marker.exists()
    assert not any(r["path"].endswith("linked") for r in results)
    trap = next(r for r in results if r["path"].endswith("trap"))
    assert trap["files"] >= 1 and "error" not in trap


def test_hostile_project_and_folder_names_cannot_inject_into_the_report(tmp_path, dataset):
    nasty = "x|y`z<script>alert(1)</script>"
    for i in range(14):
        project(dataset, "real", nasty, f"a\nb|{i}[x](evil)", human_project(i))
    out = tmp_path / "out"
    run(dataset, "--out-dir", out)
    report = (out / "report.md").read_text()
    assert "<script>" not in report and "[x](evil)" not in report and "\n# " not in report.replace("\n# ai-evidence", "").replace("\n# Evalua", "")


def test_check_for_code_reports_sizes_logs_duplicates_and_sources(tmp_path, dataset):
    project(dataset, "generated", "pasted", "dup", human_project(0))                 # same content as real/formatter-heavy/p000
    for i in range(120):
        project(dataset, "generated", "big", f"b{i}", {"src/big.py": human_module(400 + i)})
    out = run(dataset, "--check").stdout
    assert "labelled code samples" in out and "median non-blank lines, real" in out and "median non-blank lines, generated" in out
    assert "duplicate content with different labels" in out
    assert "differ more than 3x in size" in out and "no git-log.txt" in out.replace("have no git-log.txt", "no git-log.txt")


def test_check_says_when_no_project_has_a_log(tmp_path):
    root = tmp_path / "plain"
    for label in ("real", "generated"):
        project(root, label, "one", "p", {"a.py": human_module(80)})
    out = run(root, "--check").stdout
    assert "no sample has a git-log.txt" in out and "single source" in out.replace("a single source", "single source")


def test_pixels_do_not_apply_to_code_and_images_cannot_be_mixed_with_it(tmp_path, dataset):
    done = run(dataset, "--out-dir", tmp_path / "o1", "--pixels", expect_ok=False)
    assert done.returncode != 0 and "images only" in done.stderr
    (dataset / "real" / "formatter-heavy" / "photo.png").write_bytes(b.png())
    done = run(dataset, "--out-dir", tmp_path / "o2", expect_ok=False)
    assert done.returncode != 0 and "mixes images and code" in done.stderr


def test_spanish_report_and_unknown_modality_flag(tmp_path, dataset):
    out = tmp_path / "out"
    run(dataset, "--out-dir", out, "--lang", "es")
    report = (out / "report.md").read_text()
    assert "No dice nada sobre ninguna persona" in report and "con rastros de un asistente" in report and "Según el tamaño del código" in report
    assert run(dataset, "--out-dir", tmp_path / "o3", "--modality", "audio", expect_ok=False).returncode != 0
