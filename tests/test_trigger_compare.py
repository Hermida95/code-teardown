"""The trigger-comparison script must refuse to produce numbers when the CLI cannot reach the model."""
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
import trigger_compare as tc  # noqa: E402


def fake_claude(tmp_path: Path, stdout: str, code: int = 0) -> str:
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_preflight_accepts_a_working_cli(tmp_path):
    tc.preflight(fake_claude(tmp_path, json.dumps({"is_error": False, "terminal_reason": "completed", "result": "pong"})))


@pytest.mark.parametrize("payload", [
    {"is_error": True, "terminal_reason": "api_error", "result": "Failed to authenticate: OAuth session expired"},
    {"is_error": False, "terminal_reason": "api_error", "result": ""},
])
def test_preflight_refuses_when_the_model_is_unreachable(tmp_path, payload):
    with pytest.raises(tc.HarnessUnavailable) as err:
        tc.preflight(fake_claude(tmp_path, json.dumps(payload)))
    assert "not a real result" in str(err.value)


def test_preflight_refuses_garbage_and_missing_cli(tmp_path):
    with pytest.raises(tc.HarnessUnavailable):
        tc.preflight(fake_claude(tmp_path, "not json at all", code=1))
    with pytest.raises(tc.HarnessUnavailable):
        tc.preflight(str(tmp_path / "no-such-claude"))


def test_summary_math():
    result = {"results": [{"should_trigger": True, "trigger_rate": 1.0}, {"should_trigger": True, "trigger_rate": 0.5},
                          {"should_trigger": False, "trigger_rate": 0.0}]}
    assert tc.summarize(result) == (0.75, 0.0)


def test_cli_exits_with_a_clear_error_instead_of_numbers(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir, json.dumps({"is_error": True, "terminal_reason": "api_error", "result": "OAuth session expired"}))
    cands = tmp_path / "c.json"
    cands.write_text(json.dumps({"x": "a description"}))
    sets = tmp_path / "s.json"
    sets.write_text("[]")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(ROOT / "evals" / "trigger_compare.py"), "--runner", str(tmp_path),
                           "--candidates", str(cands), "--eval-set", str(sets)], capture_output=True, text=True, env=env)
    assert proc.returncode == 3 and "OAuth session expired" in proc.stderr and "candidate" not in proc.stdout
