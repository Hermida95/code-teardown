"""The trigger-measurement script must be right about 'first action' and refuse to run without a model."""
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
import trigger_compare as tc  # noqa: E402


def write_exec(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def fake_cli(tmp_path: Path, mode: str) -> str:
    """A claude that streams the events a real one would, for the command registered in its cwd."""
    return write_exec(tmp_path / f"claude-{mode}", f'''#!{sys.executable}
import json, sys, pathlib
mode = {mode!r}
name = next(pathlib.Path(".claude/commands").glob("*.md")).stem
def ev(e): print(json.dumps({{"type": "stream_event", "event": e}}), flush=True)
if mode == "skill":
    ev({{"type": "content_block_start", "content_block": {{"type": "tool_use", "name": "Skill"}}}})
    ev({{"type": "content_block_delta", "delta": {{"type": "input_json_delta", "partial_json": json.dumps({{"skill": name}})}}}})
    ev({{"type": "content_block_stop"}})
elif mode == "other-skill":
    ev({{"type": "content_block_start", "content_block": {{"type": "tool_use", "name": "Skill"}}}})
    ev({{"type": "content_block_delta", "delta": {{"type": "input_json_delta", "partial_json": '{{"skill": "code-review"}}'}}}})
    ev({{"type": "content_block_stop"}})
elif mode == "read":
    ev({{"type": "content_block_start", "content_block": {{"type": "tool_use", "name": "Read"}}}})
    ev({{"type": "content_block_stop"}})
else:
    ev({{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "just answering"}}}})
    ev({{"type": "message_stop"}})
''')


@pytest.mark.parametrize("mode,expected", [("skill", True), ("other-skill", False), ("read", False), ("text", False)])
def test_only_this_skill_as_first_action_counts(tmp_path, mode, expected):
    cli = fake_cli(tmp_path, mode)
    assert tc.first_action_is_skill("tear this down", "code-teardown", "a description", str(tmp_path), claude=cli) is expected


def test_throwaway_projects_are_cleaned_up(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    tc.first_action_is_skill("q", "code-teardown", "d", str(base), claude=fake_cli(tmp_path, "skill"))
    assert list(base.iterdir()) == []


def test_run_set_computes_rates_per_query(tmp_path):
    cli = fake_cli(tmp_path, "skill")
    result = tc.run_set([{"query": "a", "should_trigger": True}, {"query": "b", "should_trigger": False}],
                        "code-teardown", "d", runs=2, workers=2, cwd_base=str(tmp_path), claude=cli)
    assert [(r["triggers"], r["runs"]) for r in result["results"]] == [(2, 2), (2, 2)]
    assert tc.summarize(result) == (1.0, 1.0)


def test_summary_math():
    result = {"results": [{"should_trigger": True, "trigger_rate": 1.0}, {"should_trigger": True, "trigger_rate": 0.5},
                          {"should_trigger": False, "trigger_rate": 0.0}]}
    assert tc.summarize(result) == (0.75, 0.0)


def test_preflight_accepts_a_working_cli(tmp_path):
    cli = write_exec(tmp_path / "claude", '#!/bin/sh\necho \'{"is_error": false, "terminal_reason": "completed", "result": "pong"}\'\n')
    tc.preflight(cli)


@pytest.mark.parametrize("payload", [
    {"is_error": True, "terminal_reason": "api_error", "result": "Failed to authenticate: OAuth session expired"},
    {"is_error": False, "terminal_reason": "api_error", "result": ""},
])
def test_preflight_refuses_when_the_model_is_unreachable(tmp_path, payload):
    cli = write_exec(tmp_path / "claude", f"#!/bin/sh\necho '{json.dumps(payload)}'\n")
    with pytest.raises(tc.HarnessUnavailable) as err:
        tc.preflight(cli)
    assert "not a real result" in str(err.value)


def test_preflight_refuses_garbage_and_missing_cli(tmp_path):
    with pytest.raises(tc.HarnessUnavailable):
        tc.preflight(write_exec(tmp_path / "claude", "#!/bin/sh\necho 'not json'\nexit 1\n"))
    with pytest.raises(tc.HarnessUnavailable):
        tc.preflight(str(tmp_path / "no-such-claude"))


def test_cli_exits_with_a_clear_error_instead_of_numbers(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exec(bin_dir / "claude", "#!/bin/sh\necho '{\"is_error\": true, \"terminal_reason\": \"api_error\", \"result\": \"OAuth session expired\"}'\n")
    sets = tmp_path / "s.json"
    sets.write_text("[]")
    proc = subprocess.run([sys.executable, str(ROOT / "evals" / "trigger_compare.py"), "--eval-set", str(sets)],
                          capture_output=True, text=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert proc.returncode == 3 and "OAuth session expired" in proc.stderr and "candidate" not in proc.stdout


def test_shipped_description_is_read_from_skill_md():
    assert tc.shipped_description(ROOT).startswith(("Take apart", "Critical", "Static teardown"))
