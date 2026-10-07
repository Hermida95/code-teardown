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
| 0 | Informative: editing software, C2PA present without a source type, long gap between capture and last save |

Visual observations are capped by kind (`known_watermark` 0.6, `anatomy_text_errors` 0.35, `physics_lighting` 0.3, `natural_cues` 0.3, `texture_style` 0.25, `composition` and `other` 0.2) and can never reach 0.9, so looking at an image cannot by itself decide the score.

## Calibration

The weights are reasoned estimates, not fitted values. They should be tuned with a labelled set of images (real photos through different platforms, outputs of several generators, partial edits) and the false-positive rate on real photos should be the number to keep low. Until that exists, the report says "indications, not proof", and the bands are deliberately wide.
