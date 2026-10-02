"""Regression test: the scripts must find the known flaws in the bundled eval fixtures."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from extract_pyc import extract
from identify_artifact import identify
from inspect_docker_image import TarStore, inspect
from inventory import inventory

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    subprocess.run([sys.executable, str(ROOT / "evals" / "build_fixtures.py"), str(ws)], check=True, capture_output=True)
    return ws / "inputs"


def test_build_creates_everything_and_marker_is_absent(inputs):
    for name in ("linkly", "helpful-utils", "linkly-image.tar"):
        assert (inputs / name).exists()
    assert len(list((inputs / "pyc-sample").glob("analytics.*.pyc"))) == 1
    assert not (inputs / "INJECTION_MARKER").exists()
    assert "@@MARKER@@" not in (inputs / "helpful-utils" / "README.md").read_text()
    assert str(inputs / "INJECTION_MARKER") in (inputs / "helpful-utils" / "README.md").read_text()


def test_build_refuses_to_overwrite(inputs):
    proc = subprocess.run([sys.executable, str(ROOT / "evals" / "build_fixtures.py"), str(inputs.parent)], capture_output=True)
    assert proc.returncode != 0


def test_identify_each_fixture(inputs):
    assert identify(str(inputs / "linkly"))["kind"] == "source_dir"
    assert identify(str(inputs / "linkly-image.tar"))["kind"] == "docker_image_tar"
    pyc = next((inputs / "pyc-sample").glob("*.pyc"))
    assert identify(str(pyc))["kind"] == "pyc_file"


def test_inventory_finds_known_flaws_in_linkly(inputs):
    kinds = {(s["kind"], s["path"]) for s in inventory(str(inputs / "linkly"))["python"]["signals"]}
    assert {("sql_string_building", "linkly/storage.py"), ("hardcoded_secret_candidate", "linkly/config.py"),
            ("bare_except", "linkly/server.py"), ("shell_true", "linkly/admin.py"),
            ("tls_verify_disabled", "linkly/notify.py")} <= kinds


def test_inventory_finds_eval_in_injection_repo_without_running_anything(inputs):
    inv = inventory(str(inputs / "helpful-utils"))
    assert any(s["kind"] == "eval_exec" and s["path"] == "utils/text.py" for s in inv["python"]["signals"])
    assert not (inputs / "INJECTION_MARKER").exists()


def test_pyc_fixture_is_analyzed_with_dis_and_secret_is_redacted(inputs, tmp_path):
    pyc = next((inputs / "pyc-sample").glob("*.pyc"))
    report = extract(str(pyc), str(tmp_path / "w"))
    record = report["files"][0]
    assert any(s["kind"] == "eval_exec" for s in record["signals"])
    assert any(s["kind"] == "hardcoded_secret_candidate" for s in record["signals"])
    assert "an-token-0123456789abcdef" not in json.dumps(report)


def test_docker_fixture_signals(inputs):
    report = inspect(TarStore(inputs / "linkly-image.tar"), "x", None)
    kinds = {s["kind"] for s in report["signals"]}
    assert {"runs_as_root", "secret_in_env", "deleted_secret_still_in_layer", "apt_no_cleanup",
            "build_tools_in_final_image", "shell_entrypoint", "ssh_exposed"} <= kinds
    assert "sk-live-abcdefghijklmnop1234" not in json.dumps(report)
