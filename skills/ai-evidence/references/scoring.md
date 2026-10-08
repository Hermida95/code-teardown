# How the scores are combined

## The two numbers on every item

| Field | Range | Meaning |
| --- | --- | --- |
| `score` | 0-10 | Where the item points. 0 = clearly not AI, 5 = neutral, 10 = clearly AI |
| `weight` | 0-1 | How far it can be trusted. 0 = shown but not counted, 0.9+ = conclusive |

The two are independent on purpose. A filename like `ChatGPT Image ....png` points strongly to AI (score 9) but is easy to fake or inherit (weight 0.5). A complete camera EXIF record points to "not AI" (score 1) with a medium weight (0.5), because generators do not write it but anyone can copy it.

## Two questions, scored separately

- `generated`: was the image made by a model from scratch?
- `ai_edited`: was a real image changed by a model (generative fill, face swap, inpainting)?

Each item belongs to one claim. A C2PA composite record is `ai_edited`; a Stable Diffusion parameter block is `generated`. Editing software in the metadata (Photoshop, Lightroom) is recorded with weight 0: it says the file was edited, not that AI was used.

## Combining

For one claim, with items `(score_i, weight_i)`:

1. **Informative items** (weight 0) are shown and never counted.
2. **No counted items** → `insufficient`, no score.
3. **Conclusive items** (weight ≥ 0.9). If some point to AI (score ≥ 6) and some away from it (score ≤ 4), the status is `conflict`: they are *not* averaged. Otherwise, the score is the weighted mean of the conclusive items only and the confidence is `high`.
4. **Otherwise**, the score is the weighted mean of all counted items, `Σ(score·weight) / Σweight`.

**Confidence** comes from the total weight, not from the score: `high` if an item is conclusive, `medium` at Σweight ≥ 1.2, `low` at ≥ 0.4, below that `very_low`. With `very_low` the band is "insufficient" whatever the mean says, so a single weak item can never colour the result.

## Group caps

Weak signals of one kind are correlated: five stock-phrase hits and a few em dashes are one habit, not five independent proofs. So, per question, the total weight of a group is scaled down to its cap: `style` 0.5, `pixels` 0.5, the model's observations (`visual`) 1.0. Each item's weight is reduced in proportion, and the report shows the original weight next to the scaled one. The effect is that style signals alone can never raise confidence above "low", and the model's own impressions can never reach "high".

## Bands

| Score | Band | Colour |
| --- | --- | --- |
| 0 - 1.9 | No signs of AI | green |
| 2 - 3.9 | Few signs | light green |
| 4 - 5.9 | Mixed signs | amber |
| 6 - 7.9 | Clear signs | orange |
| 8 - 10 | Very likely AI | red |
| - | Not enough evidence | grey |
| - | Conflicting evidence | purple |

Colour is never the only carrier: every band has a text label and the number is always shown.

## Weights used by the extractor

| Weight | Kind of evidence |
| --- | --- |
| 0.95-0.97 | A generator's own record: Stable Diffusion / ComfyUI / Midjourney / SwarmUI / InvokeAI / Fooocus parameters; C2PA or IPTC `trainedAlgorithmicMedia` |
| 0.85-0.9 | `Software` / `CreatorTool` naming a generator; IPTC or C2PA composite with AI parts |
| 0.6-0.7 | A generator named in free text or a C2PA claim generator; a camera capture declared in a C2PA manifest |
| 0.4-0.5 | Complete camera EXIF; a file name that matches a generator's download pattern |
| 0.2-0.25 | Typical generator dimensions; make and model only |
| 0.1 | No metadata at all |
| 0.8-0.85 | Text residue: `tx-citation-markers` (0.85), `tx-assistant-phrases` (0.8) |
| 0.6-0.9 | Code residue: commit trailers (0.9, for "assisted"), assistant config files (0.8), placeholders, fences, comment declarations (0.6-0.7) |
| 0.1-0.2 | Text and code style items (capped together at 0.5) |
| 0 | Informative: editing software, C2PA present without a source type, long gap between capture and last save |

Pixel measurements (the optional module, see [pixel-signals.md](pixel-signals.md)) are capped at 0.25 and are uncalibrated heuristics.

Visual observations are capped by kind (`known_watermark` 0.6, `anatomy_text_errors` 0.35, `physics_lighting` 0.3, `natural_cues` 0.3, `texture_style` 0.25, `composition` and `other` 0.2) and can never reach 0.9, so looking at an image cannot by itself decide the score.

## Calibration

The weights are reasoned estimates, not fitted values. [evals/evaluate_dataset.py](../evals/README.md) measures them on a labelled set of images: false-positive rate on real photos (the number to keep low), detection rate, abstention rate, and how each piece of evidence behaves, with a suggested weight beside the current one. Suggestions come from a dev split and the headline numbers from a held-out test split. Until it has been run on a dataset that covers real images from the channels you care about, the report says "indications, not proof", and the bands are deliberately wide.
