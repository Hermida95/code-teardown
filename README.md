# Skills for working critically with code and AI output

Agent Skills for [Claude Code](https://claude.com/claude-code) and compatible clients. They share one idea: **a judgement is only worth what its evidence is worth**, so every skill shows what it found, where, and how sure it is, instead of handing down a verdict.

| Skill | What it does | Status |
| --- | --- | --- |
| [**code-teardown**](skills/code-teardown) | A critical, evidence-backed teardown of code you did not write: source repos, Python `.pyc` files and Docker images. Says what is good, what is bad and what depends on the use, with `file:line` citations. | v0.1.0 |
| [**ai-evidence**](skills/ai-evidence) | Weighs the evidence that an image was generated or edited with AI. Every item gets a 0-10 score (0 = not AI, 10 = AI) and a trust weight; they are combined into scored answers with a confidence level. Text and code are planned. | v0.1.0, images |

Both are static (nothing analyzed is ever run), use only the Python standard library, and write one self-contained HTML report.

## Install

As plugins (one marketplace, two plugins):

```bash
claude plugin marketplace add Hermida95/code-teardown
claude plugin install code-teardown@code-teardown
claude plugin install ai-evidence@code-teardown
```

Or copy a single skill into your skills folder (keep the folder name, it must match the skill's name):

```bash
git clone https://github.com/Hermida95/code-teardown
cp -R code-teardown/skills/ai-evidence ~/.claude/skills/ai-evidence
```

To try one from a checkout without installing: `claude --plugin-dir ./skills/ai-evidence`.

## Layout

```
skills/
  code-teardown/   SKILL.md, scripts/, references/, evals/, tests/, README.md ...
  ai-evidence/     SKILL.md, scripts/, references/, tests/, README.md ...
```

Each skill folder is self-contained and has its own README, tests, changelog and licence copy, so it can be installed or tested on its own:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
cd skills/code-teardown && ../../.venv/bin/pytest
cd ../ai-evidence && ../../.venv/bin/pytest
```

(Run the two suites separately: they use the same module names for their helpers.)

## Contributing

See each skill's `CONTRIBUTING.md`. The shared rules: evidence or silence, no execution of analyzed content, standard library only in `scripts/`, and a test that fails without every fix.

MIT licence.
