# Confidence guide

Confidence says how far a reader can trust a statement. Declare it at two levels: **overall** (for the whole report) and **per finding**. The renderer enforces the ceilings in the table below; the rest is your judgment.

## Ceilings by how the code was recovered

| Situation | Overall ceiling | Per-finding ceiling | What to do |
| --- | --- | --- | --- |
| Real source (repo, extracted `.py`, files in an image) | high | high | Normal analysis |
| `.pyc` decompiled with an external tool (`methods_used` has `decompiler:*`) | medium | medium | Declare that comments, local names and formatting are lost |
| `.pyc` read only through `dis` (`method: dis`) | low | low | Judge structure and behaviour, not style or naming intent |
| `.pyc` of a different Python version, no decompiler (`method: header-only`) | very_low | no quality findings | Mark all axes `not_assessed` and say what is needed to do better |
| Obfuscation suspected (`obfuscation_suspected: true`) | low | low | Limit the analysis; `limits` must say how |
| Native binary, JAR, APK, .NET | out of scope | | Say so and stop |

Docker images analyzed from the archive are not capped: layers, history and config are facts. Multi-platform archives, layers that could not be read and stripped history are not caps but **must appear in `limits`**.

## What decompiled and compiled code loses

- Comments, docstrings in some versions, original formatting and line layout.
- Local variable names are often kept, but may be mangled; type annotations may be lost; `with`/`try`/comprehension forms may reappear as lower-level constructs.
- Intent. A `dis` listing shows what happens, not why. Do not write "the author wanted..." for such code. Write what the bytecode does.
- Decompiler output can be **wrong**. Treat it as a reading aid, and cross-check important claims against the `dis` listing when both exist.

If a construct is ambiguous, say so in the finding's `detail` ("the decompiled code shows X; the bytecode could also be Y") and lower the confidence.

## Per-finding confidence

- **high**: you read the cited code and its surrounding context, the claim follows directly from it, and no plausible alternative explanation changes the verdict.
- **medium**: the claim follows from the evidence, but depends on something you did not fully verify (another module, runtime configuration, a call site you did not read), or the code was decompiled.
- **low**: inferred from structure, names, signals or partial reading, or from bytecode alone. Still worth reporting, but flagged.

Lower a finding one step when: you only read a sample of the module; the evidence is a signal you did not read in context; the code looks generated, vendored or minified; the verdict hinges on runtime behaviour you cannot see.

## Overall confidence

Think weakest link, then coverage:

1. Start from the ceiling in the table.
2. Lower it if you read only part of the artifact. Say how much in `limits` ("read 18 of 64 modules: entry points, the 5 most imported, the 5 most complex").
3. Lower it if key evidence was unavailable (no tests shipped, history stripped, layers unreadable).
4. Write the `explanation` as a plain sentence a reader can act on: what the confidence rests on and what would raise it.

List every degradation in `confidence.degradations` (for example "no decompiler installed; used dis only", "layer 3 uses zstd and could not be read").

## Phrases to avoid

- "The author intended / chose to..." about decompiled or minified code.
- "This is vulnerable" without a cited path from untrusted input to the dangerous call. Use "risk surface".
- "The tests are bad / missing" when the artifact simply did not ship them.
- Performance claims stated as fact without profiling: use "probable".
- Anything about parts you did not read.

## Obfuscation and native code

- If extraction reports obfuscation (PyArmor markers, `exec` of decoded data, minified identifiers), say that structure and names may be deliberately misleading, describe only what the evidence shows, and do not judge design quality.
- If the artifact contains native extensions (`.so`, `.pyd`) or the input is a native binary, state that these parts were not analyzed.
- Never imply a complete analysis where the tooling could not provide one.
