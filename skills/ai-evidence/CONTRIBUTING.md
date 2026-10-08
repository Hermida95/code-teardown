# Contributing

The project is small on purpose, and a few rules keep it trustworthy.

## Ground rules

1. **Evidence, not verdicts.** Nothing in the output may read as proof. New signals come with a score, a weight and a written way they can be wrong in [references/image-signals.md](references/image-signals.md).
2. **Weights need a reason.** If you change a weight or the aggregation, update [references/scoring.md](references/scoring.md) in the same change and say what data motivated it, ideally a run of [the evaluation harness](evals/README.md) (numbers on a held-out test split, never on the images the change was fitted to). Do not commit datasets.
3. **Static only.** The analyzed file is read as bytes; only the optional pixel module decodes it (with Pillow, under size limits). No execution, no network.
4. **Standard library only** in `scripts/` (optional modules must be opt-in and degrade cleanly; today only `analyze_pixels.py` uses Pillow and NumPy). `pytest` is the only required development dependency; install `pillow numpy` as well to run the pixel tests, which are skipped without them.
5. **Every fix gets a test that fails without it.** Hostile inputs (truncated files, huge chunk counts, decompression bombs) belong next to the analyzer tests.
6. **Tests build their images in code** (`tests/builders.py`, `tests/pixel_builders.py`); do not commit real images. A pixel heuristic is only justified by what it measures on a synthetic image with a known property, and its limits go in [references/pixel-signals.md](references/pixel-signals.md).

## Workflow

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest
```

- Use [Conventional Commits](https://www.conventionalcommits.org) (`feat(image): ...`, `fix(score): ...`).
- Scripts must work on Python 3.11. CI runs 3.11 to 3.14.
- Keep `SKILL.md` under 500 lines; the docs tests check its links and flags.
