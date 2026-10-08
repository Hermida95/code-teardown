# Evaluating ai-evidence

> This is for checking and learning about the tool, and it does not accuse anyone. It measures how the tool agrees with labels you supply. It says nothing about any person, and it is not a way to judge anyone's work.

The skill's weights are reasoned estimates. `evaluate_dataset.py` is how they get checked against images whose origin you know, and how they can be corrected. It runs the automatic layers (file evidence, and the pixel module if you ask for it) on a labelled set and reports:

- the **false-positive rate on real images**, with a 95 % interval. This is the number to keep low: a real photo marked as AI is the harm the tool must avoid.
- the **detection rate** on generated images and on AI-edited images.
- how often the answer is **"not enough evidence"**, per label. A high rate is not a failure, it is the honest answer for images that carry nothing.
- for every piece of evidence, **how often it fires on each kind of image**, a likelihood ratio, and a **suggested weight** next to the current one.

It works on **images**, on **texts** and **code** (one kind per run; the kind is inferred from the files: `.png .jpg .jpeg .webp .gif` for images, `.txt .md .text` for texts, and a dataset that contains source files is read as code; a dataset must not mix kinds). For texts and code it matters most: style signals are the ones most likely to be wrong, and until this is run on real texts and projects they have not been measured. The model's own inspection (looking at an image, reading a text) is not part of this: it needs a model in the loop, so these numbers describe the file and pixel layers only.

```bash
python3 evals/evaluate_dataset.py ~/ai-eval-data --out-dir ~/ai-eval-out            # file evidence only, no dependencies
python3 evals/evaluate_dataset.py ~/ai-eval-data --out-dir ~/ai-eval-out2 --pixels --augment strip --lang es
# without Pillow and NumPy installed:
uv run --with pillow --with numpy python evals/evaluate_dataset.py ~/ai-eval-data --out-dir out --pixels --augment strip
```

| Flag | Meaning |
| --- | --- |
| `--check` | Only count images per label and source, and list gaps and duplicates. Runs nothing and writes nothing; use it while collecting |
| `--out-dir DIR` | Required unless `--check`. Must be new or empty; writes `report.md`, `summary.json` and `results.json` (one row per image and condition) |
| `--manifest FILE` | A CSV instead of the folder convention (below) |
| `--modality image\|text\|code` | Force the kind of dataset instead of inferring it |
| `--pixels` | Also run the optional pixel module (images only; needs Pillow and NumPy) |
| `--augment strip` | Also evaluate a cleaned copy of every file. Images are re-encoded the way a messaging app does it: no metadata, longest edge 1600 px, JPEG quality 80. Texts get the assistant residue removed (citation markers, tracking parameters, assistant phrases, placeholders). Code gets the assistant files left out, residue comments, pasted fences and README residue removed, and trailer lines taken out of the git log. Either way it shows how much the result depends on what is easiest to lose or delete |
| `--no-filename` | Drop the file-name evidence. Use it if your files were sorted or renamed in a way that could give the label away |
| `--lang en\|es` | Language of the report |
| `--max-files N` | Safety limit (default 20 000) |

Everything runs locally; nothing is uploaded.

## Texts

```
ai-eval-texts/
  real/
    native-essays/        texts you know were written by people
    non-native-essays/    people writing in a second language (with their permission)
    emails-and-notes/     short, informal writing
  generated/
    chatgpt-pasted/       as copied out of the chat, residue included
    api-clean/            as returned by an API, with no interface residue
  edited/
    human-polished-by-ai/ human drafts rewritten with an assistant
```

```bash
python3 evals/evaluate_dataset.py ~/ai-eval-texts --check
python3 evals/evaluate_dataset.py ~/ai-eval-texts --out-dir ~/ai-eval-texts-out --augment strip
```

Beyond what the image report shows, the text report adds:

- **Rows with at least medium confidence.** A text that only shows style gets "clear signs" at best with *low* confidence, because style items share one capped budget. The plain detection and false-positive rows count that as flagged; the "at least medium confidence" rows count only the firm cases. For texts, read the second pair.
- **By text length** and **by language.** Style is not scored under 150 words or outside English and Spanish, so results differ a lot by bucket.
- **Where each item fires on real texts, by source.** This is the fairness check. If `tx-phrase-density` or `tx-em-dash` fires far more on `non-native-essays` than on `native-essays`, that item should lose weight, whatever its likelihood ratio says.
- **Length and topic confounds** are checked by `--check`: if generated texts are much longer than the human ones, a tool can look good just by length.

The cleaned copies show how much of the detection rests on residue, which anyone can delete, and how much on style, which is the weaker and less fair part.

## Code

The unit is a **project folder** (or a single source file), laid out as `<label>/<source>/<project>`:

```
ai-eval-code/
  real/
    formatter-heavy/     human projects written with black, prettier, gofmt...
    tutorial-style/      human code with narrating comments and full docstrings
    template-based/      human projects started from a scaffold or cookiecutter
    ai-apps/             human projects that call AI services
  generated/
    chat-pasted/         projects or files as pasted from a chat, residue included
    agent-clean/         output of a coding agent with its traces removed
  edited/                human projects built with an assistant (the label is "assisted")
    assistant-pair/
```

```bash
python3 evals/evaluate_dataset.py ~/ai-eval-code --check
python3 evals/evaluate_dataset.py ~/ai-eval-code --out-dir ~/ai-eval-code-out --augment strip
```

- **History is optional and exported by you.** The harness never runs git (a repository's own configuration can make git launch programs). To evaluate commit trailers and history shape, export a log into each project as `git-log.txt` with the command in [code-signals](../references/code-signals.md) (no author names or e-mails). `--check` tells you how many projects have one.
- The report has the same blocks as for texts: firm (medium confidence or better) rates, **false positives by source** for the human projects, the table of **where each item fires on real projects by source**, and breakdowns **by code size** and **by main language**.
- **The fairness check here is formatter-heavy, tutorial-style, template-based and AI-app projects written by people.** If `cd-docstring-uniformity`, `cd-narrating-comments` or `cd-emoji-in-code` fire a lot on those, that item should lose weight whatever its likelihood ratio says.
- The cleaned copies show how much the result rests on assistant files, trailers and residue comments, which anyone can delete, and how much on style.
- Everything stays local. Projects can contain secrets: the report holds only paths, counts and evidence ids, never code.

## Building the dataset

The step-by-step plan, with quantities and the traps to avoid, is in [COLLECTING.md](COLLECTING.md). In short:

Folder convention, where the second level (optional) is the **source** and is used to break the false-positive rate down:

```
ai-eval-data/
  real/
    camera-original/   photos straight from a camera or phone, metadata intact
    whatsapp/          the same kind of photos after being sent through a messaging app
    instagram/         ...after a social network
    screenshots/
  generated/
    midjourney/
    sdxl/
    chatgpt/           the file exactly as downloaded, not re-saved
  edited/
    photoshop-genfill/ real photos changed with a generative tool
```

Or a `manifest.csv` with the columns `path,label[,source,split]` (labels: `real`, `generated`, `edited`; paths relative to the dataset folder and inside it).

What makes the numbers worth reading:

1. **Real images need the same journeys as the ones you will check.** If every real photo is an untouched original, the false-positive rate will look perfect and mean nothing. Include images that went through messaging apps, social networks, screenshots and editing.
2. **Generated images from several generators, saved as downloaded.** Re-saving them yourself erases the metadata and measures the wrong thing. Add the shared versions separately, or let `--augment strip` make them.
3. **Enough of each.** With fewer than 30 test images per class the report says so and withholds suggested weights. For a first useful read aim for 100+ per class; the intervals shrink slowly (with 0 errors in 100 real images, the upper end is still about 4 %).
4. **No duplicates across labels.** The report warns when the same content appears twice, and identical content always lands in the same split.
5. **Mind the licences and people.** Use images you have the right to use. Photos of other people are personal data: keep the dataset local, never commit it (`evals/datasets/` is ignored by git), and do not publish it.
6. **Do not label by guessing.** An image you only *think* is AI-made belongs out of the set. Wrong labels look like tool errors.

## Reading the report

- **Split.** Images are split by content hash into `dev` (70 %) and `test` (30 %), or by the `split` column if you give one. Suggested weights come from `dev`; the headline numbers come from `test`. This is deliberate, so the weights are not graded on the images that produced them.
- **Likelihood ratio (LR).** How many times more often the item fires on the positives than on the negatives, with add-one smoothing. An item whose `direction` is flagged (it points to AI but fires more on real images, or the reverse) is wrong as designed and needs a look, not just a new weight.
- **Suggested weight.** A rule of thumb: `min(0.97, |ln LR| / ln 100)`, so LR 10 gives 0.5 and LR 100 gives 0.97. Pixel items are never suggested above their cap of 0.25. It is a starting point for a human decision, not an automatic update.
- **AUC.** Probability that a random positive scores above a random negative, with "not enough evidence" counted as a neutral 5. It rewards separation without choosing a threshold.
- **Precision** depends on how many real and generated images you included, so it does not carry over to real life. Compare detection and false-positive rates instead.

## Changing weights without fooling yourself

1. Run on a dataset and read `evidence_on_dev`.
2. Change weights in `scripts/analyze_image.py` (and `references/scoring.md`, which must say why) for the items with enough data.
3. Run again and check `test`. **Once.** If you keep adjusting while looking at `test`, it stops being a test.
4. For the next round, collect new images (or move some into a fresh `test`) before measuring again.

A change is worth keeping when the false-positive rate's upper bound does not go up and the detection rate does. For texts, add a second condition: the false-positive rate on second-language writers must not go up.
