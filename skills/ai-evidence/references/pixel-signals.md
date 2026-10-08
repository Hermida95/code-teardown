# Pixel measurements (optional module)

`analyze_pixels.py` looks at the pixels themselves. It needs Pillow and NumPy, which the rest of the skill does not, so it is optional: without them the skill works as before and the report says pixel forensics were not run.

**Read this first.** These measurements are heuristics. The thresholds were chosen by reasoning and checked on *synthetic* images built to have a known property (added noise, a pasted patch, a periodic pattern). They have **not** been calibrated on real photos or real generator output. That is why every scored item is capped at weight 0.25 (the cap is enforced twice, in the analyzer and in `score_evidence.py`), why no pixel item can decide a result, and why each measurement is shown even when it is not scored. Pixel evidence nudges a result; it never carries it.

## What is measured

Everything runs on the luma channel at native resolution, on a centre crop of up to 2048 px (noise), 1024 px (ELA) or 1024 px (spectrum). Nothing is resized, because resizing destroys exactly what is being measured.

| id | Claim | What it measures | Scored when | Score / weight |
| --- | --- | --- | --- | --- |
| `px-noise-level` | generated | Noise σ on a 0-255 scale, from the median absolute response of a 3×3 Laplacian-type mask over flat 32×32 blocks (Immerkaer's estimator, with a median instead of a mean) | σ < 0.6 (almost noise-free) or σ ≥ 2.5 (sensor-like) | 6.5 / 0.12, or 3 / 0.12 |
| `px-noise-inconsistent` | ai_edited | Whether a **compact** group of blocks has noise at least 3× above or below the median | 3-40 % of blocks, in one connected region (joined across one-cell gaps) | 6.5 / 0.12 |
| `px-periodic-artifact` | generated | Power at 1/2, 1/4, 1/8 and 1/16 of the sampling rate against the neighbouring frequencies, per axis | A peak ≥ 15× its surroundings at 1/2 or 1/4 (weight 0.2), or at 1/8 or 1/16 in lossless files (0.12) | 7 / 0.12-0.2 |
| `px-spectrum-hf` | generated | Ratio of high- to mid-frequency energy of the residual | Never; context only | 5 / 0 |
| `px-ela` | ai_edited | JPEG only. Saves the crop again at quality 90 and looks for a compact region whose error level differs by 3× from the median | 2-40 % of blocks, in one region | 6 / 0.12 |
| `px-jpeg-quality` | ai_edited | Quality estimated from the quantization table | Never; context, and it switches the other tests off below ~60 | 5 / 0 |
| `px-skipped` | generated | Why nothing was measured | Never | 5 / 0 |

## When it refuses to measure

- **Under 260 px** on either side: the statistics are meaningless.
- **Graphics**: palette images, fewer than 64 brightness levels, or fewer than 1500 distinct colours in a colour image (illustrations, icons, screenshots). A black-and-white photo is recognised and measured.
- **Low-quality JPEG** (estimated quality below 60): compression has erased the noise and the edit traces, so noise and error-level items are reported but not scored.
- **Too large**: above 50 million pixels or 64 MB, the file is refused before decoding (Pillow's decompression-bomb guard is turned into an error).

## How each one can mislead

- **Noise level.** Fine textures (fabric, foliage, fur, grass) read as noise, so a textured real or generated image shows a high σ. Phone photos with noise reduction, retouched photos and upscaled images are nearly noise-free, as many generated images are. Generators and "film grain" filters add noise. This is why it weighs 0.12.
- **Inconsistent noise.** Noise really does change with brightness and with lens vignetting, and a clear sky next to foliage is a legitimate difference. A pasted or regenerated region is only one cause. JPEG compression at moderate quality flattens differences until they fall below the 3× factor, so a missed splice at quality 92 is expected.
- **Periodic artifacts.** Upsampling layers in generators leave peaks at the pixel-pair or 4-pixel scale, and an 8×8 pattern from a VAE decoder is also known. But resizing, sharpening, screen-door patterns on photographed screens and some codecs leave peaks too. A peak at 1/8 or 1/16 in a JPEG is the **block grid** and is deliberately not scored. A lossless file that was once a JPEG will show it as well, and that case cannot be told apart here.
- **ELA.** Error level analysis is the most over-trusted technique in image forensics. Textured regions, saturated colours and sharp edges recompress differently by themselves, and a re-saved JPEG changes everything. It is included at low weight and as a pointer for a human to look at the region.

## What is not here

- **Copy-move detection**, **double-compression histograms** and **camera-model fingerprints (PRNU)**: heavier methods that are planned only if a labelled set makes them worth it.
- **Learned detectors.** Neural AI-image classifiers are a separate, fast-moving field and are not bundled.

## Running it

```
analyze_pixels.py <image> --out $WORK/pixels.json
```

Exit status 3 means Pillow or NumPy is missing. To run it without changing the environment:

```
uv run --with pillow --with numpy python ${CLAUDE_SKILL_DIR}/scripts/analyze_pixels.py <image> --out $WORK/pixels.json
```

Installing packages is the user's decision: ask before running `pip install`.
