# `findings.json` schema

You write this file; `render_report.py` validates it and turns it into the HTML report. It fails closed: a problem means no report. Fix the findings and run it again.

Contents: [Top level](#top-level) · [Evidence references](#evidence-references) · [Findings](#findings) · [Validation rules](#validation-rules) · [Minimal complete example](#minimal-complete-example) · [Running the renderer](#running-the-renderer)

## Top level

| Key | Type | Notes |
| --- | --- | --- |
| `meta.title` | string | Report title |
| `meta.language` | `"en"` or `"es"` | Interface language of the report; default `en`. Write the text in the user's language |
| `meta.generated` | string, optional | Date; defaults to today |
| `meta.artifact` | `{path, kind}` | `kind` from `identify_artifact.py` |
| `meta.context` | `{id, label, asked, assumed_reason?}` | `id`: `production_service`, `automation_script`, `library`, `learning`, `other`. `asked` is true if the user confirmed it; if false, `assumed_reason` is required |
| `confidence` | `{overall, explanation, degradations[]}` | `overall`: `high`, `medium`, `low`, `very_low` |
| `summary` | `{headline, verdict}` | One-line headline and a short paragraph |
| `architecture` | `{system, components[], key_functions[]}` | Each component: `{name, role, modules[]?, evidence[]}`; each key function: `{name, role, evidence[]}`. At least one component. Evidence is mandatory |
| `inventory` | `[{label, value}]`, optional | Facts from the scripts, shown as a table |
| `context_weights` | object | `rationale` (string) plus one of `high`/`medium`/`low` for each **assessed** axis |
| `axes` | list | Exactly one entry for each of the six axes |
| `learning` | `{patterns_to_copy[], anti_patterns[]}`, optional | Each note: `{title, why, fix?, evidence[]}` |
| `limits` | list of strings | May be empty, unless obfuscation was suspected |

Axis ids: `separation_of_concerns`, `error_handling`, `security`, `testability`, `coupling`, `performance`.

An axis entry is either assessed:

```json
{"id": "security", "summary": "...", "findings": [ ... ]}
```

or not applicable:

```json
{"id": "testability", "status": "not_assessed", "reason": "The artifact ships no tests."}
```

## Evidence references

Evidence is a string or `{ "ref": "...", "quote": "..." }`. The `quote` is optional but recommended: the renderer then checks that the text really appears on the cited lines (whitespace-insensitive).

| Form | Example | Checked against |
| --- | --- | --- |
| File and line(s) | `src/app/core.py:42` or `src/app/core.py:42-57` | A file under one of the `--root` directories; the line range must exist |
| Docker layer | `layer 2` or `layer 2: etc/passwd` | `--docker-report`: the layer index exists |
| Docker history | `history[4]` | `--docker-report`: the entry exists |
| Docker config field | `config.User`, `config.Env`, `config.Entrypoint` | `--docker-report`: the field exists |
| Bytecode | `pkg/mod.cpython-314.pyc :: Store.load (line 9)` | `--extraction`: the `.pyc` and the function exist, and the line matches if given |
| Command | `cmd: docker history demo:latest` | Not checkable; counted as unverifiable in the report |

Paths are relative to a `--root`, never absolute and never containing `..`. For a decompiled or disassembled file, cite it relative to the extraction directory (for example `decompiled/pkg/mod.pyc.py:12` with `--root $WORK/pyc`). Cite the secret's **name and location**, never its value; the renderer refuses text that looks like a token or private key.

## Findings

```json
{
  "id": "bare-except-load",
  "title": "Bare except hides read failures",
  "verdict": "bad",
  "confidence": "high",
  "detail": "load() catches everything and returns None, so callers cannot tell a missing file from an empty one.",
  "why_it_matters": "A failed sync looks like a successful empty one.",
  "recommendation": "Catch OSError and let anything else propagate.",
  "evidence": [{"ref": "pkg/core.py:6", "quote": "except:"}]
}
```

- `id`: unique slug (lowercase letters, digits, hyphens).
- `verdict`: `good`, `improvable`, `bad`, `depends`. For `depends`, `depends_on` and `context_note` are required; they explain what it hinges on and what that means in the chosen context.
- `confidence`: `high`, `medium`, `low`, within the ceiling in `confidence-guide.md`.
- `detail` is required; `why_it_matters`, `recommendation` and `context_note` are optional otherwise.

## Validation rules

The renderer rejects the file when:

1. Any finding, architecture component, key function or learning note has no evidence.
2. A file reference points to a missing file, a line past the end, or a quote not found on those lines; or a path escapes the root.
3. A layer, history, config or bytecode reference does not exist in the inspection output.
4. A confidence value exceeds the ceiling for the extraction method (see `confidence-guide.md`), or any quality finding is given when no structure was recovered.
5. A `depends` verdict lacks `depends_on` or `context_note`.
6. One of the six axes is missing, or a `not_assessed` axis has no reason.
7. The context was not asked and `assumed_reason` is missing.
8. Obfuscation was suspected and `limits` is empty.
9. Any text looks like a secret value.

Errors are printed with a path such as `axes[1].findings[0].evidence[0]: pkg/core.py:999: file has only 7 line(s)`.

## Minimal complete example

An automation script with two axes not assessed. It is shown with `--no-verify` semantics in the tests, so its file references are illustrative.

```json
{
  "meta": {
    "title": "Teardown of acme-sync",
    "language": "en",
    "artifact": {"path": "acme-sync/", "kind": "source_dir"},
    "context": {"id": "automation_script", "label": "Nightly sync script", "asked": true}
  },
  "confidence": {
    "overall": "high",
    "explanation": "Real source; I read every module (9 files).",
    "degradations": []
  },
  "summary": {
    "headline": "Small, readable sync tool with one risky error path",
    "verdict": "Clear structure for a script. The main weakness is that failures are silent."
  },
  "architecture": {
    "system": "A command-line tool that copies records from an HTTP API into a local SQLite file.",
    "components": [
      {"name": "CLI", "role": "Parses arguments and runs the sync",
       "modules": ["acme_sync.cli"],
       "evidence": [{"ref": "acme_sync/cli.py:12", "quote": "def main"}]},
      {"name": "API client", "role": "Fetches pages from the remote API",
       "modules": ["acme_sync.client"],
       "evidence": ["acme_sync/client.py:5"]}
    ],
    "key_functions": [
      {"name": "acme_sync.cli.main", "role": "Entry point wiring client and store",
       "evidence": ["acme_sync/cli.py:12-40"]}
    ]
  },
  "inventory": [
    {"label": "Python files", "value": "9"},
    {"label": "Declared dependencies", "value": "2"}
  ],
  "context_weights": {
    "rationale": "A nightly script for one team: short and direct is fine, but it must fail loudly and not leak its API token.",
    "separation_of_concerns": "low",
    "error_handling": "medium",
    "security": "medium",
    "testability": "low"
  },
  "axes": [
    {"id": "separation_of_concerns", "summary": "Three small modules with one job each.",
     "findings": [
       {"id": "clear-module-roles", "title": "Each module has one job", "verdict": "good",
        "confidence": "high",
        "detail": "cli.py only wires, client.py only talks HTTP, store.py only writes SQLite.",
        "evidence": [{"ref": "acme_sync/cli.py:12", "quote": "def main"}, "acme_sync/store.py:3"]}]},
    {"id": "error_handling", "summary": "Failures are mostly swallowed.",
     "findings": [
       {"id": "swallowed-http-errors", "title": "HTTP errors are logged and ignored", "verdict": "depends",
        "confidence": "medium",
        "detail": "fetch_page() catches every exception, logs it and returns an empty list.",
        "depends_on": "whether a partial sync is acceptable to the people reading the data",
        "context_note": "For a nightly script, a partial sync with exit code 0 can go unnoticed for weeks; a non-zero exit would be safer.",
        "evidence": [{"ref": "acme_sync/client.py:22", "quote": "except Exception"}]}]},
    {"id": "security", "summary": "The token is read from the environment.",
     "findings": [
       {"id": "token-from-env", "title": "API token comes from the environment", "verdict": "good",
        "confidence": "high",
        "detail": "The token is never written into the code or the database.",
        "evidence": ["acme_sync/client.py:8"]}]},
    {"id": "testability", "summary": "One test file covers the store.",
     "findings": [
       {"id": "client-untested", "title": "The HTTP client has no tests", "verdict": "improvable",
        "confidence": "medium",
        "detail": "tests/ contains only test_store.py; fetch_page() builds its own session so it cannot be faked easily.",
        "evidence": ["tests/test_store.py:1", "acme_sync/client.py:15"]}]},
    {"id": "coupling", "status": "not_assessed",
     "reason": "Nine small files with a single import chain; nothing worth weighing for a script this size."},
    {"id": "performance", "status": "not_assessed",
     "reason": "Nightly batch with small data; no hot path visible."}
  ],
  "learning": {
    "patterns_to_copy": [
      {"title": "Secrets from the environment", "why": "Keeps credentials out of the repository and the artifact.",
       "evidence": ["acme_sync/client.py:8"]}
    ],
    "anti_patterns": [
      {"title": "Catching everything and returning an empty value", "why": "The caller cannot tell failure from no data.",
       "fix": "Catch the specific exceptions you can handle and let the rest stop the run.",
       "evidence": ["acme_sync/client.py:22"]}
    ]
  },
  "limits": ["Static analysis only; nothing was executed.", "No git history was inspected."]
}
```

## Running the renderer

```
render_report.py findings.json --out ./code-teardown-acme-sync.html \
    --root /path/to/acme-sync \
    [--root another/evidence/root] \
    [--docker-report $WORK/img/docker-report.json] \
    [--extraction $WORK/pyc/extraction.json]
```

- It prints a JSON summary on success (`evidence_checked`, `evidence_total`, `evidence_unverifiable`) and exits 0.
- On any problem it prints the list to stderr, writes nothing and exits 1.
- `--no-verify` skips file checks, only for evidence that cannot exist as local files. The report then shows a visible warning that nothing was machine-verified.
