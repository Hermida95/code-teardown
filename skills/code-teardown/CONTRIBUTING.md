# Contributing

Thanks for helping. The project is small on purpose, and a few rules keep it trustworthy.

## Ground rules

1. **Static only.** Nothing in this repo may run, import or install an analyzed artifact. If a change needs that, it is out of scope.
2. **Evidence or silence.** Findings must cite something checkable. Do not weaken the validator in `render_report.py` to make a report pass; fix the report.
3. **Standard library only** in `scripts/`. `pytest` is the only development dependency.
4. **Every fix gets a test that fails without it.** For hostile-input fixes, put the case in `tests/test_hardening.py` (it runs hostile snippets in a child process with a timeout).
5. **Never print secret values** in output, tests' expected strings included; build key-shaped fake secrets at runtime so scanners do not trip on them.

## Workflow

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest
```

- Use [Conventional Commits](https://www.conventionalcommits.org) (`fix(inventory): ...`, `feat(docker): ...`).
- Scripts must still work on Python 3.11. CI runs 3.11 to 3.14.
- If you change `SKILL.md` or `references/`, keep `SKILL.md` under 500 lines; the docs tests check links, flags and the schema example.

## Ideas that fit

The [roadmap](README.md#roadmap): more artifact types, other-language metrics, real decompiler validation. Open an issue first for anything that changes the safety model.
