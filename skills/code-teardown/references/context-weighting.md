# Weighting by usage context

The same code deserves different verdicts depending on how it will be used. Ask the context in step 5, pick the row below, and record the weights in `context_weights` with a one-paragraph rationale in your own words. Weights are ordinal (`high`, `medium`, `low`); do not invent numeric scores.

## Matrix

| Axis | production_service | automation_script | library | learning |
| --- | --- | --- | --- | --- |
| separation_of_concerns | medium | low | high | high |
| error_handling | high | medium | high | medium |
| security | high | medium | medium | medium |
| testability | high | low | high | medium |
| coupling | medium | low | high | medium |
| performance | medium | low | medium | low |

Why each context looks like this

- **production_service**: it runs unattended, for other people, on real data. Failures, attacks and regressions cost money, so error handling, security and testability weigh most. Structure matters, but a service with clear failure behaviour and tests is more valuable than a prettier one without.
- **automation_script**: a script for the author or a small team, run occasionally. Short and direct is a virtue; heavy layering is overhead. It still must fail loudly and must not leak credentials or run shell commands built from untrusted input, so error handling and security stay at medium.
- **library**: other people's code depends on it. The public surface is a contract: separation, low coupling and predictable errors matter most, and tests protect every consumer. Performance is medium because callers control the workload.
- **learning**: the goal is to understand and reuse ideas. Clarity of structure (separation of concerns) dominates, and what a reader can copy matters more than operational hardening. Performance rarely matters. Frame findings around what the code teaches.
- **other**: derive the row from the closest context, say which, and adjust. For example, a data pipeline is closest to *production_service* with performance raised to `high`.

## Adjustments

Raise a weight one step (never above `high`) when the facts you have seen justify it, and say why in the rationale:

- Handles personal, financial or health data, or credentials: raise **security**.
- A clear hot path or large inputs (a loop over a database, a request handler): raise **performance**.
- Public API or many consumers visible in the code: raise **coupling** and **separation_of_concerns**.
- Safety-critical or long-running unattended: raise **error_handling**.

Lower a weight when the artifact makes it moot, for example testability of an image that ships no tests (usually `not_assessed` instead).

## How context changes a verdict

Use `depends` when the verdict flips with context, and fill both `depends_on` and `context_note`. Worked examples:

| Observation | production_service | automation_script | library | learning |
| --- | --- | --- | --- | --- |
| Module-level global config object | improvable | good | bad | improvable (explain the trade-off) |
| `except Exception: log; continue` in a batch loop | depends (what is lost?) | good if logged | bad | improvable |
| No tests | bad | depends (size and risk) | bad | improvable |
| `subprocess.run(cmd, shell=True)` with constant `cmd` | improvable | good | improvable | improvable |
| Same call, `cmd` built from input | bad | bad | bad | bad |
| Large image with build tools | improvable | depends | n/a | depends |
| Runs as root in a container | bad | depends (is it throwaway?) | n/a | improvable |

These are starting points, not rules: read the code and decide. If the verdict is the same in every context, it is not `depends`.

## Using the weights

- Put the chosen weights in `context_weights` (one entry per axis you assess).
- The report orders the "findings that matter most here" by axis weight times verdict severity, so honest weights make the summary useful.
- Say in the chat summary which context was used, and whether it was confirmed with the user or assumed.
