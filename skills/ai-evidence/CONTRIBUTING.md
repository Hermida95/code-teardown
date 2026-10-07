# Contributing

The project is small on purpose, and a few rules keep it trustworthy.

## Ground rules

1. **Evidence, not verdicts.** Nothing in the output may read as proof. New signals come with a score, a weight and a written way they can be wrong in [references/image-signals.md](references/image-signals.md).
2. **Weights need a reason.** If you change a weight or the aggregation, update [references/scoring.md](references/scoring.md) in the same change and say what data motivated it.
3. **Static only.** The analyzed file is read as bytes. No imaging library, no execution, no network.
4. **Standard library only** in `scripts/` (optional modules must be opt-in and degrade cleanly). `pytest` is the only development dependency.
5. **Every fix gets a test that fails without it.** Hostile inputs (truncated files, huge chunk counts, decompression bombs) belong next to the analyzer tests.
6. **Tests build their images in code** (`tests/builders.py`); do not commit real images.

## Workflow

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest
```

- Use [Conventional Commits](https://www.conventionalcommits.org) (`feat(image): ...`, `fix(score): ...`).
- Scripts must work on Python 3.11. CI runs 3.11 to 3.14.
- Keep `SKILL.md` under 500 lines; the docs tests check its links and flags.
