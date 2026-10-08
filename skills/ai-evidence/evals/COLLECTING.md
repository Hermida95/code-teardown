# Collecting a dataset

*En español, con plan por sesiones y prompts listos: [GUIA-IMAGENES.es.md](GUIA-IMAGENES.es.md).*

A practical plan for the labelled set that [evaluate_dataset.py](README.md) needs. Roughly 300 images and two or three hours get you a first useful read. The aim is not a big set but an **honest** one: images that went through the same journeys as the ones you will check, with labels you are sure of.

## What to aim for

| Group | Folder | First useful read | Why |
| --- | --- | --- | --- |
| Real, untouched | `real/camera-original/` | 50 | Baseline: metadata intact |
| Real, through a messaging app | `real/whatsapp/` | 50 | The most common way an image reaches you |
| Real, screenshots | `real/screenshots/` | 25 | Metadata gone, resolution changed |
| Real, conventionally edited | `real/retouched/` | 25 | A filter or retouch is **not** AI; this is where false positives hide |
| Generated, as downloaded | `generated/<generator>/` | 100 across 3 or more generators | Whether the provenance records are caught |
| Generated, shared copies | `generated/<generator>-shared/` | 25 | The same kind of images after a messaging app or screenshot |
| AI-edited | `edited/<tool>/` | 50 | Real photos changed with generative fill or similar |

`python3 evals/evaluate_dataset.py <dataset> --check` tells you at any point what is missing. The report needs at least 30 images per class in its test split, which is about 100 per class overall.

## Real images

All of them must be images **you know are real**: your own photos, or photos from someone who gave you the original file.

1. Take or pick 100 of your own photos, varied: people, landscapes, food, interiors, night, text on signs, close-ups.
2. Keep half untouched in `real/camera-original/`.
3. Send the other half to yourself through the messaging app you really use and save what arrives into `real/whatsapp/` (or `real/telegram/`...). Use the chat's download, not a copy-paste.
4. Take 25 screenshots of photos on your screen into `real/screenshots/`.
5. Run 25 through the retouching you normally use (phone filters, Lightroom, Snapseed, beauty mode) into `real/retouched/`. If the tool offers an AI feature, do not use it here: this folder is for ordinary edits.

Phones now apply computational photography (HDR, night mode, portrait blur). That is real and belongs in the set; do not filter it out.

## Generated images

1. Pick at least three generators, ideally including one that writes Content Credentials and one local/open one (Midjourney, ChatGPT images, Gemini, Firefly, a Stable Diffusion front end, Flux, Ideogram...). The mix decides what the numbers can say.
2. Use varied prompts: photorealistic people, landscapes, products, illustrations, screenshots of fake interfaces, scenes with text.
3. Save each image **exactly as the tool gives it to you**. Do not open and re-save it, and do not rename it in a way that hides the generator's default name (or use `--no-filename` later). Folder: `generated/<generator>/`.
4. For a share of them, also create the version people actually pass around: send it through a messaging app or take a screenshot, and put it in `generated/<generator>-shared/`. (`--augment strip` can simulate this, but real copies are better.)
5. Check the generator's terms of use. Most allow keeping your own outputs for testing; none of this dataset should be published.

## AI-edited images

Take real photos you own and change them with a generative tool: generative fill or expand, object removal or replacement, background swap, face or hair change, AI upscaling. Save the result as the tool provides it into `edited/<tool>/`. The original goes in `real/` only if it is a different file; never put an original and its edit as the same content.

## Public datasets

Research datasets of real and AI-generated images exist, and using one saves effort. Two cautions: **check the licence**, and **check whether the files kept their metadata**. Many datasets are re-encoded (resized, JPEG, metadata removed), so they test the stripped case and say nothing about provenance records. If you use one, put it in its own `source` folder so the report separates it.

## Avoid these traps

- **A format confound.** If every real image is a JPEG and every generated image is a PNG, a tool can look good for the wrong reason. Give each class both formats or compare against the `stripped` condition (`--augment strip`).
- **Label leakage through names.** If you sorted files by hand, names like `fake_01.png` are harmless, but `ChatGPT Image ...` is a real signal that may not be there in practice. Run once with and once without `--no-filename`.
- **Near-duplicates.** Several crops of the same photo in different splits flatter the result. Keep one version.
- **Labels you are not sure of.** Leave an image out rather than label it by feel. A wrong label looks like a tool error.
- **Peeking at test.** Read the `dev` evidence table to adjust weights, then look at `test` once. Collect fresh images before the next round.

## People and privacy

Photos of people are personal data. Use your own, or ones whose subjects agreed, and do not include children's photos from anyone else. Keep the dataset on your machine: `evals/datasets/` is ignored by git, nothing in the harness uploads anything, and the report only contains file names and counts, so check the names before sharing a report.

## Collecting texts

*En español, con plan por sesiones, temas y consentimiento para voluntarios: [GUIA-TEXTOS.es.md](GUIA-TEXTOS.es.md).*

Same principle (labels you are sure of, the journeys you will meet), with different traps.

| Group | Folder | First useful read |
| --- | --- | --- |
| Human, native speakers | `real/native-.../` | 100 |
| Human, second-language writers | `real/non-native-.../` | 60 |
| Human, short and informal (notes, messages) | `real/informal/` | 40 |
| Generated, as pasted from a chat | `generated/<assistant>-pasted/` | 60 |
| Generated, as returned by an API or with the residue deleted | `generated/<assistant>-clean/` | 60 |
| Human text polished or rewritten by an assistant | `edited/...` | 50 |

1. **Humans.** Use your own old writing (before 2022 is the safest proof it is human), writing from people who gave you permission, or texts whose origin you can vouch for. Do not use other people's schoolwork, applications or private messages without their agreement, and never use this to test a particular person.
2. **Include second-language writers.** This is the most important group. Style signals flag them far more than native speakers, and a dataset without them hides the main harm. Ask volunteers to write on the same prompts as everyone else.
3. **Same topics, same lengths.** Give the assistant the same prompts the human writers had and ask for the same length. If generated texts are longer, or on different topics, the numbers measure that instead. `--check` warns about a length gap.
4. **Both ways of getting AI text.** Pasted from a chat interface (residue included) and returned clean by an API or after deleting the residue. The second kind is what a careful user hands in, and it is where the tool will usually say "not enough evidence".
5. **Several assistants and several instructions.** Include "write naturally", "write like a student" and "avoid clichés": the tool should do worst there, and you want to know by how much.
6. **Polished texts.** Take a human draft and have an assistant improve it. Label it `edited`, not `generated`.
7. **Lengths.** Mix short (under 150 words), medium and long texts. Short ones only get residue checks.
8. **Languages.** English and Spanish are covered. Include a few texts in other languages to see that the tool abstains instead of guessing.
9. **No duplicates**, and nothing you are not sure about.

Texts of other people are personal data. Keep the set on your machine (`evals/datasets/` is ignored by git), and do not publish it.

## Collecting code

The unit is a project folder, one per sample, under `<label>/<source>/`.

| Group | Folder | First useful read |
| --- | --- | --- |
| Human, formatter-heavy | `real/formatter-heavy/` | 40 |
| Human, tutorial-style (narrating comments, full docstrings) | `real/tutorial-style/` | 40 |
| Human, started from a scaffold or template | `real/template-based/` | 30 |
| Human, apps that call AI services | `real/ai-apps/` | 20 |
| Generated, pasted from a chat | `generated/chat-pasted/` | 40 |
| Generated by an agent, traces removed | `generated/agent-clean/` | 40 |
| Human projects built with an assistant | `edited/assistant-pair/` | 40 |

1. **Human code that predates assistants** is the safest "real": your own or others' repositories from before 2022, or projects whose author confirms no assistant was used. Open-source projects are fine; check their licences and keep copies local.
2. **Include the look-alikes.** The false positives that matter are people who write code the way assistants do: formatter-driven style, tutorial comments, scaffolds, projects that call OpenAI or Anthropic APIs (their comments mention "generated by the model"). A dataset of only scruffy human code hides them.
3. **Generated projects.** Ask an assistant for projects of the same kind and size as the human ones (a CLI, an API, a scraper), once with the chat's residue left in and once as a coding agent would leave it. Several assistants and several prompts.
4. **"Assisted" projects.** Human projects where an assistant took part, with the traces it leaves: its configuration files and commit trailers. They answer the "built with AI assistance?" question; keep them separate from fully generated ones.
5. **Match sizes and tasks.** If generated projects are much bigger or smaller than the human ones, the tool can look good just by size. `--check` warns above a 3x gap in median size.
6. **Optional history.** For each project you trust, export `git-log.txt` yourself with the command in [code-signals](../references/code-signals.md). Run it only on repositories you trust: git can launch programs configured in a repository. The harness never runs git.
7. **Cleaning.** Keep dependency folders out (`node_modules`, virtualenvs): the tool skips them anyway, but they bloat the dataset.
8. **Privacy.** Code can contain secrets, personal data and licensed material. Keep the dataset local (`evals/datasets/` is ignored by git), do not publish it, and do not use other people's private repositories.

## The loop

```bash
mkdir -p ~/ai-eval-data/{real/{camera-original,whatsapp,screenshots,retouched},generated,edited}
# ...copy images in...
python3 evals/evaluate_dataset.py ~/ai-eval-data --check
python3 evals/evaluate_dataset.py ~/ai-eval-data --out-dir ~/ai-eval-out --augment strip
# add --pixels (needs Pillow and NumPy) once the file-evidence numbers make sense
```

Then read `report.md` from the false-positive rate down, as described in the [README](README.md#reading-the-report).
