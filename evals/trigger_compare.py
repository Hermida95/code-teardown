#!/usr/bin/env python3
"""Compare candidate SKILL.md descriptions on how often Claude activates the skill.

Wraps the `run_eval` script of Anthropic's skill-creator skill and adds what it lacks: a
preflight check. If `claude -p` cannot reach the model (expired login, rate limit, offline),
run_eval reports every query as "not triggered", which looks like a real 0% result. Here
that is a hard stop instead.

Usage:
  python3 evals/trigger_compare.py --runner SKILL_CREATOR_DIR --candidates cands.json \
      --eval-set train.json [--eval-set test.json] [--runs-per-query 2]

cands.json maps a name to a description, e.g. {"current": "...", "shorter": "..."}.
Eval sets are lists of {"query": ..., "should_trigger": true|false}; see trigger-eval.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class HarnessUnavailable(RuntimeError):
    pass


def preflight(claude: str = "claude", timeout: int = 60) -> None:
    """Raise HarnessUnavailable unless `claude -p` really gets an answer from the model."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        proc = subprocess.run([claude, "-p", "reply with the single word: pong", "--output-format", "json"],
                              capture_output=True, text=True, timeout=timeout, env=env)
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


def run_candidate(runner: Path, repo: Path, description: str, eval_set: Path, runs: int, workers: int) -> dict:
    proc = subprocess.run([sys.executable, "-m", "scripts.run_eval", "--eval-set", str(eval_set), "--skill-path", str(repo),
                           "--description", description, "--runs-per-query", str(runs), "--num-workers", str(workers),
                           "--timeout", "90"], cwd=runner, capture_output=True, text=True)
    text = proc.stdout
    return json.loads(text[text.index("{\n"):text.rindex("}") + 1])


def summarize(result: dict) -> tuple[float, float]:
    positives = [r["trigger_rate"] for r in result["results"] if r["should_trigger"]]
    negatives = [r["trigger_rate"] for r in result["results"] if not r["should_trigger"]]
    return (sum(positives) / len(positives) if positives else 0.0, sum(negatives) / len(negatives) if negatives else 0.0)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runner", required=True, help="path of the skill-creator skill (contains scripts/run_eval.py)")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--eval-set", action="append", required=True)
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--runs-per-query", type=int, default=2)
    parser.add_argument("--num-workers", type=int, default=4)
    args = parser.parse_args(argv[1:])
    try:
        preflight()
    except HarnessUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    candidates = json.loads(Path(args.candidates).read_text())
    print(f"{'candidate':16s} {'set':14s} {'activates (should)':>20s} {'false triggers':>16s}")
    for name, description in candidates.items():
        for eval_set in args.eval_set:
            result = run_candidate(Path(args.runner), Path(args.repo), description, Path(eval_set), args.runs_per_query, args.num_workers)
            positive, negative = summarize(result)
            print(f"{name:16s} {Path(eval_set).stem:14s} {positive:>19.0%} {negative:>16.0%}", flush=True)
            if positive == 0 and negative == 0:
                print("  note: 0% everywhere is suspicious; rerun the preflight before trusting it", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
