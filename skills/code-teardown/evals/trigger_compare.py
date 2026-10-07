#!/usr/bin/env python3
"""Measure how often Claude activates the skill, and compare candidate descriptions.

For each request it registers the description as a temporary command in a throwaway project,
runs `claude -p` there and looks at Claude's FIRST action: activation means the first thing it
does is call this skill. The process is killed right after, so a run costs a single model turn.

Why not skill-creator's `run_eval`? Two traps, both hit while building this project:
  * it runs `claude -p` from your home directory and registers its command in ~/.claude/commands,
    a context very unlike a normal project (it measured ~30% where this script measures ~100%);
  * an API failure (expired login, rate limit) counts as "not triggered", so everything scores 0%.
This script runs in a throwaway directory (--cwd-base lets you put it under a real tree) and
refuses to measure at all unless `claude -p` really gets an answer from the model.

Usage:
  python3 evals/trigger_compare.py --eval-set requests.json [--eval-set other.json]
      [--candidates cands.json] [--runs-per-query 2] [--cwd-base DIR] [--save-dir out/]

Eval sets are lists of {"query": ..., "should_trigger": true|false}; see trigger-eval.json.
Candidates map a name to a description; without them the SKILL.md description is measured.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class HarnessUnavailable(RuntimeError):
    pass


def _env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}   # allow claude -p inside a Claude session


def preflight(claude: str = "claude", timeout: int = 60) -> None:
    """Raise HarnessUnavailable unless `claude -p` really gets an answer from the model."""
    try:
        proc = subprocess.run([claude, "-p", "reply with the single word: pong", "--output-format", "json"],
                              capture_output=True, text=True, timeout=timeout, env=_env())
    except FileNotFoundError:
        raise HarnessUnavailable("the claude CLI is not on PATH")
    except subprocess.TimeoutExpired:
        raise HarnessUnavailable(f"`claude -p` did not answer within {timeout}s")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HarnessUnavailable(f"`claude -p` printed no JSON (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    if data.get("is_error") or data.get("terminal_reason") not in (None, "completed"):
        raise HarnessUnavailable(f"`claude -p` cannot reach the model: {str(data.get('result'))[:200]}. "
                                 "Every query would count as 'not triggered', which is not a real result. "
                                 "Log in again (claude auth login) and retry.")


def first_action_is_skill(query: str, skill_name: str, description: str, cwd_base: str | None = None,
                          timeout: int = 90, claude: str = "claude") -> bool:
    """True if Claude's first tool call, for this request, is the skill registered under `skill_name`."""
    project = Path(tempfile.mkdtemp(prefix="trigger-", dir=cwd_base))
    unique = f"{skill_name}-skill-{uuid.uuid4().hex[:8]}"
    try:
        (project / ".claude" / "commands").mkdir(parents=True)
        indented = "\n  ".join(description.split("\n"))
        (project / ".claude" / "commands" / f"{unique}.md").write_text(
            f"---\ndescription: |\n  {indented}\n---\n\n# {skill_name}\n\nThis skill handles: {description}\n")
        proc = subprocess.Popen([claude, "-p", query, "--output-format", "stream-json", "--verbose", "--include-partial-messages"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=project, env=_env())
        buffer, tool, args, started = "", None, "", time.time()
        try:
            while time.time() - started < timeout:
                if not select.select([proc.stdout], [], [], 1.0)[0]:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "stream_event":
                        continue
                    inner = event["event"]
                    kind = inner.get("type")
                    if kind == "content_block_start" and inner["content_block"].get("type") == "tool_use":
                        tool = inner["content_block"]["name"]
                    elif kind == "content_block_delta" and tool and inner["delta"].get("type") == "input_json_delta":
                        args += inner["delta"].get("partial_json", "")
                    elif (kind == "content_block_stop" and tool) or kind == "message_stop":
                        return tool == "Skill" and unique in args
            return False
        finally:
            if proc.poll() is None:
                proc.kill()
    finally:
        shutil.rmtree(project, ignore_errors=True)


def run_set(eval_set: list[dict], skill_name: str, description: str, runs: int, workers: int,
            cwd_base: str | None, claude: str = "claude") -> dict:
    jobs = [(i, item["query"]) for i, item in enumerate(eval_set) for _ in range(runs)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda job: first_action_is_skill(job[1], skill_name, description, cwd_base, claude=claude), jobs))
    results = []
    for i, item in enumerate(eval_set):
        hits = [o for (idx, _), o in zip(jobs, outcomes) if idx == i]
        results.append({"query": item["query"], "should_trigger": item["should_trigger"], "triggers": sum(hits),
                        "runs": len(hits), "trigger_rate": sum(hits) / len(hits)})
    return {"results": results}


def summarize(result: dict) -> tuple[float, float]:
    positives = [r["trigger_rate"] for r in result["results"] if r["should_trigger"]]
    negatives = [r["trigger_rate"] for r in result["results"] if not r["should_trigger"]]
    return (sum(positives) / len(positives) if positives else 0.0, sum(negatives) / len(negatives) if negatives else 0.0)


def shipped_description(repo: Path) -> str:
    return re.search(r"^description: (.+)$", (repo / "SKILL.md").read_text(), re.M).group(1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eval-set", action="append", required=True)
    parser.add_argument("--candidates", help="JSON {name: description}; default: the SKILL.md description")
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--runs-per-query", type=int, default=2)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--cwd-base", help="create the throwaway project directories under this folder (default: system temp)")
    parser.add_argument("--save-dir", help="write the raw per-query result of every candidate/set here as JSON")
    args = parser.parse_args(argv[1:])
    try:
        preflight()
    except HarnessUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    candidates = json.loads(Path(args.candidates).read_text()) if args.candidates else {"shipped": shipped_description(Path(args.repo))}
    print(f"{'candidate':16s} {'set':14s} {'activates (should)':>20s} {'false triggers':>16s}")
    for name, description in candidates.items():
        for eval_set in args.eval_set:
            result = run_set(json.loads(Path(eval_set).read_text()), "code-teardown", description,
                             args.runs_per_query, args.num_workers, args.cwd_base)
            positive, negative = summarize(result)
            if args.save_dir:
                Path(args.save_dir).mkdir(parents=True, exist_ok=True)
                (Path(args.save_dir) / f"{name}--{Path(eval_set).stem}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
            print(f"{name:16s} {Path(eval_set).stem:14s} {positive:>19.0%} {negative:>16.0%}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
