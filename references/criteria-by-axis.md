# Criteria by axis

How to assess each of the six axes. For every axis: what good and bad look like, where the evidence comes from, how the artifact type changes things, and the usual false positives. Weights depend on the usage context; see `context-weighting.md`.

Contents: [General method](#general-method) · [1 Separation of responsibilities](#1-separation-of-responsibilities) · [2 Error handling](#2-error-handling) · [3 Security](#3-security) · [4 Testability](#4-testability) · [5 Coupling](#5-coupling) · [6 Performance](#6-performance)

## General method

- Start from the inventory, but **read the code before judging**. A signal such as `broad_except` or `shell_true` only shows where to look.
- A good finding is specific: one claim, one or two citations, one sentence on why it matters in this context.
- Judge against the stated purpose of the artifact, not against your own taste or a framework you would have chosen.
- Prefer a few findings you can defend over many shallow ones. About three to six per axis is plenty.
- A `good` finding needs the same evidence standard as a `bad` one.
- When two readings are plausible, use `depends` and name the deciding factor. Do not hedge inside `good` or `bad`.

## 1 Separation of responsibilities

**Question:** does each unit have one clear reason to change, and are the layers (I/O, domain logic, presentation, configuration) distinguishable?

Good signs
- Modules and functions with a nameable single purpose; short functions (compare `stats.mean_length` and `longest_functions`).
- I/O at the edges, pure logic in the middle; configuration read in one place.
- Entry points (`main_guard`, console scripts) that only wire things together.

Bad signs
- "God" modules or classes (`largest_classes`, very long functions, high `most_complex_functions`).
- Business rules mixed with HTTP handling, SQL strings or printing.
- Utility modules that grow unrelated helpers.

Evidence: file:line of the mixed concerns; function `length` and `cyclomatic` from the inventory, confirmed by reading.

By artifact
- **Source**: use the import graph to see layering (`most_depended_on` should be stable, low-level modules).
- **.pyc**: judge from the function and class list only; names and grouping survive, comments and formatting do not. Keep confidence at most `low` or `medium`.
- **Docker image**: the equivalent is *one process per container*, a multi-stage build that separates build from runtime, and config injected through environment instead of baked in. Evidence: `history[i]`, `config.Entrypoint`, build tools in `filesystem.build_or_network_tools_present`.

False positives: a single-file script that is deliberately small is not a god module. A long function that is a flat table or a state machine can be fine.

## 2 Error handling

**Question:** do failures surface at the right place, with enough context, without leaving the system in a bad state?

Good signs
- Narrow `except` clauses that handle what they can and let the rest propagate.
- Resources released deterministically (`with` blocks); timeouts on network calls; retries only where idempotent.
- Errors carry context; exit codes or logs make failures visible.

Bad signs
- `bare_except`, `swallowed_exception`, broad `except Exception: pass`, returning `None` on failure so callers cannot tell.
- Validation missing at trust boundaries; asserts used for input validation.
- Cleanup missing on early return.

Evidence: the handler's file:line, plus the caller if the consequence depends on it. Read each flagged handler in context: a broad catch at a top-level loop that logs and continues can be correct.

By artifact
- **.pyc**: handler structure is visible in the disassembly (exception table), but intent is not. Say so.
- **Docker image**: no `HEALTHCHECK`, a shell-form `ENTRYPOINT` that swallows signals as PID 1, no `STOPSIGNAL` for a process that needs graceful shutdown. Whether it matters depends on the orchestrator (often it supplies health checks): use `depends`.

## 3 Security

**Question:** is the artifact built to avoid common, avoidable weaknesses? This is a design review, not a vulnerability hunt or an exploit.

Check
- Secrets: hard-coded credentials (`hardcoded_secret_candidate`), secrets in environment or build history, files like `.env` or keys in the artifact.
- Injection surfaces: `eval`/`exec`, `shell_true`, `os_system`, `sql_string_building`, unsafe deserialization, `tls_verify_disabled`.
- Trust boundaries: is input validated where it enters? Is untrusted data ever used as a path, command or query?
- Dependencies: unpinned, no lockfile, `possibly_undeclared_deps` (a lead only).
- Images: `runs_as_root`, `secret_in_env`, `secret_in_instruction`, `sensitive_file`, `deleted_secret_still_in_layer` (a secret deleted in a later layer is still in the earlier one), setuid files, `curl_pipe_shell`, `chmod_777`, outdated base OS (`base_os`).

Evidence: file:line, `layer N`, `history[i]`, `config.Field`. **Never quote the secret value.**

False positives: `eval` in a test fixture or build script is not the same as `eval` on user input; `md5` for cache keys is not a password hash. Say what the use is. Many hits of one kind in vendored code may be someone else's code.

Do not claim exploitability you have not shown. "Reaches `os.system` with a value derived from `request.args`" needs the path cited; otherwise it is a risk surface, rated `improvable` or `depends`.

## 4 Testability

**Question:** how easily can the behaviour be checked, and is it?

Good signs
- A test suite exists and mirrors the code layout (`project_signals.test_modules`, `test_functions`).
- Dependencies are passed in rather than imported inside functions; side effects are isolated; small pure functions.
- CI config present (`ci_files`); lint or type configuration (`lint_or_type_config`); type annotations (`annotated_ratio`).

Bad signs
- Logic buried in `if __name__ == "__main__"` blocks or module-level code that runs on import.
- Global state and hidden singletons; time, randomness or network used directly in logic.
- No tests in a project that clearly needs them.

Evidence: test file:line, the hard-to-test construct's file:line. A missing test suite is evidence by absence: cite the inventory (`test_modules: 0`) and say so.

By artifact
- **.pyc**: tests are normally not shipped; mark `not_assessed` with that reason unless the package includes them.
- **Docker image**: a bare image has no unit tests. Assess *reproducibility* instead if it fits (pinned versions, deterministic build), or mark `not_assessed`.

## 5 Coupling

**Question:** how much does a change in one place force changes elsewhere, and how replaceable are the dependencies?

Check
- The import graph: `most_dependencies` (high fan-out), `most_depended_on` (high fan-in, which is fine if stable), `cycles_all` vs `cycles_eager` (a cycle broken only by a lazy import is a smell, an eager one is worse).
- Reaching into another module's internals; shared mutable globals; deep inheritance.
- Hard-coded paths, URLs and environment assumptions.
- Third-party coupling: is a framework or SDK used at the core or only at the edges?
- Images: coupling to a specific base distribution, baked-in config, absolute host paths.

Evidence: the edge (`from` → `to` with line), the cycle members, the hard-coded value's file:line (without secrets).

False positives: high fan-in on a small, stable utility is healthy. A framework application is legitimately coupled to its framework.

## 6 Performance

**Question:** are there avoidable inefficiencies that matter for the intended use? Do not guess at hot paths: say what you can see.

Check
- Algorithmic: nested loops over the same collection, repeated lookups in lists, per-item queries in a loop (N+1), work repeated inside loops.
- I/O: reading whole files when streaming works, no connection reuse, blocking calls in async code.
- Caching and batching where repetition is evident.
- Images: size (`totals`, `filesystem.largest_files`), layer order (rarely-changing steps first for cache reuse), `wasted_space`, caches left behind (`apt_no_cleanup`, `pip_cache`, `filesystem.cache_and_waste_candidates`), build tools in the final image.

Evidence: file:line of the construct and, where relevant, the loop around it. Without profiling data every performance claim is at best `medium` confidence; say "probable" and why.

False positives: a quadratic loop over a ten-item list is fine. Weight by how large the input can plausibly be, per the usage context.
