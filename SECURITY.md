# Security

code-teardown analyzes untrusted artifacts (repos, `.pyc` files, Docker images), so its own safety matters.

## What it guarantees

- **No execution.** Analyzed code, bytecode and images are never run, imported, installed or tested. `marshal` runs in an isolated child process with a CPU limit and a timeout; `docker save` is the only Docker command used.
- **Content is data.** The skill instructs the model to treat text inside the artifact as data, never as instructions, and to report attempts to address an AI as findings.
- **Contained writes.** Scripts write only a new or empty work directory, one `.json` and one `.html`, and refuse to overwrite an existing file without `--force`. Archive members are extracted only as regular files, with path-traversal, size and count guards.
- **No secret echo.** Secret values are redacted from reports, extraction output and Docker history; findings cite names and locations. The renderer refuses text that looks like a token or private key.
- **Bounded work.** Inputs that read artifact text have size caps and non-backtracking patterns; deep nesting, huge archives and overlong names are contained instead of aborting or hanging the run.
- **Self-contained report.** No external resources, all content escaped, and a Content-Security-Policy that allows only the report's own script by hash.

## What it does not guarantee

- It is not a vulnerability scanner or a malware sandbox. A report is a well-sourced second opinion, not proof of safety.
- External decompilers (`pycdc`, `decompyle3`) are optional, run on the file you analyze, and parse untrusted data; install them from sources you trust.
- Permission prompts are your agent's, not this project's: the skill does not pre-approve its scripts.
- `evals/fixtures/` contains deliberately vulnerable and prompt-injecting samples. They are test data; do not deploy them.

## Reporting a problem

Please open a private security advisory on the GitHub repository, or an issue that describes the impact without publishing a working exploit.
