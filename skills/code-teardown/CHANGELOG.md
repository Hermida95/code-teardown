# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com), and the project uses [Semantic Versioning](https://semver.org).

## [Unreleased]

### Changed
- The repository is now the Second Opinion collection (`Hermida95/second-opinion-skills`) and this skill lives in `skills/code-teardown/`. The marketplace is named `second-opinion`: run `claude plugin marketplace remove code-teardown`, then `claude plugin marketplace add Hermida95/second-opinion-skills` and `claude plugin install code-teardown@second-opinion`. The per-skill `marketplace.json` was removed; the root one lists every plugin.

### Fixed
- The 0.1.0 note that automatic activation is unreliable was wrong: it came from a flawed measuring harness (skill-creator's `run_eval` runs from the home directory and scores API failures as "not triggered"). Measured properly the skill activates for 16/16 requests that should trigger it and 0/24 near-misses.

### Added
- `evals/trigger_compare.py`: measures activation by Claude's first action in a throwaway project, compares candidate descriptions, and refuses to run when `claude -p` cannot reach the model.

## [0.1.0] - 2026-10-04

First public release.

### Added
- An eight-step teardown pipeline for source repos, Python `.pyc` files and packages (wheel, zipapp, egg, sdist) and Docker images (`docker save` tar, extracted dir or image name).
- `identify_artifact.py`, `inventory.py`, `extract_pyc.py`, `inspect_docker_image.py` and `render_report.py`, all standard library only.
- An evidence-checked HTML report: every claim must cite a file and line, layer, history entry, config field or bytecode function, and is verified against the analyzed artifact. Light and dark themes, navigation and filters, English and Spanish, no external resources, hash-based Content-Security-Policy.
- Context weighting (production service, automation script, library, learning) over six axes, with confidence ceilings that depend on how the code was recovered.
- Plugin and marketplace manifests; a verified example report on `dbader/schedule`.
- An eval harness (with-skill vs without-skill, objective grader, prompt-injection fixture) and a trigger-eval set.

### Security
- Static analysis only; `marshal` isolated in a child process; archive extraction with traversal and size guards.
- Hardened against hostile artifacts: bounded regexes, contained deep nesting, scan budgets, validated image references, refusal to overwrite files without `--force`, secret redaction. See [SECURITY.md](SECURITY.md).

### Known limitations
- (Corrected in Unreleased) Automatic activation was reported as unreliable; that came from a flawed harness.
- Decompiler integration is tested with stand-ins, not real `pycdc` or `decompyle3`.
- JAR, APK, .NET and native binaries are declined (planned for v1).

[0.1.0]: https://github.com/Hermida95/second-opinion-skills/releases/tag/v0.1.0
