# Signals extracted from the file

Everything here comes from `analyze_image.py`, which reads the file as bytes. For each signal: what it is, the score and weight it gets, and how it can mislead. Order is from most to least reliable.

## 1. Generator records (weight 0.95-0.97, score 10)

| id | What was found |
| --- | --- |
| `sd-parameters` | The `Steps: .., Sampler: .., CFG scale: .., Seed: ..` block of Automatic1111 / Forge, in a PNG text chunk, JPEG comment or EXIF `UserComment` |
| `comfyui-graph` | A ComfyUI node graph (`class_type`, `inputs`) in a PNG text chunk |
| `midjourney-job` | A prompt with `--ar` / `--v` plus a `Job ID: <uuid>` in the description |
| `swarmui-params`, `invokeai-metadata`, `fooocus-params`, `novelai-software` | The respective tool's own block |

Can mislead: someone can paste such a block into a real photo, but it takes deliberate effort. It almost never appears by accident. Deleting it is trivial, so its absence says nothing.

## 2. Declared source type (weight 0.9-0.95)

| id | What was found |
| --- | --- |
| `c2pa-trained-media`, `xmp-trained-media` | IPTC DigitalSourceType `trainedAlgorithmicMedia`: the tool declares the image fully AI-made (score 10, weight 0.95) |
| `c2pa-composite`, `xmp-composite` | `compositeWithTrainedAlgorithmicMedia`: part of a real image was made by a model (claim `ai_edited`, score 9, weight 0.9) |

Used by OpenAI (ChatGPT/DALL·E), Adobe Firefly, Google, Microsoft, Meta and others. Can mislead:
- **C2PA signatures are not verified here.** Presence and the declared type are read from the manifest bytes; validate with `c2patool` or contentcredentials.org/verify before relying on it.
- A tool can also write the label by default on a real photo that was only retouched with AI, or a person can add it. It describes the tool's claim, not the whole history.
- These labels are stripped by many platforms and screenshots.

## 3. Naming a generator (weight 0.6-0.85)

`exif-software-generator`, `xmp-creator-tool` (0.85, score 9.5): the `Software` or `CreatorTool` field names an AI product. `xmp-history-generator` (0.7, score 8.5, claim `ai_edited`): a step of the XMP edit history was done by an AI tool. `generator-mention` and `c2pa-generator-named` (0.6, score 8.5): a free-text field or a C2PA claim generator names one. Can mislead: a caption like "not made with Midjourney" matches too, and a C2PA manifest naming Adobe may belong to an ordinary edit. Hence the lower weight for free text.

## 4. Pointing to a real capture (score 1-2.5)

| id | Weight | What was found |
| --- | --- | --- |
| `c2pa-capture` | 0.7 | The manifest declares `digitalCapture` / `computationalCapture` (cameras, recent phones) |
| `exif-camera` | 0.5 | Make and model plus at least three capture fields (exposure, aperture, ISO, focal length, lens, original date) |
| `xmp-capture` | 0.4 | XMP declares a capture |
| `exif-camera-partial` | 0.25 | Make and model only |

Can mislead: EXIF is trivially copied or forged, and an AI-edited real photo keeps its camera data. These lower the "generated" score but say little about `ai_edited`.

## 5. Weak structural hints

- `generator-dimensions` (score 6.5, weight 0.2): width and height equal a default generator size (1024×1024, 1024×1792, 832×1216, ...). Only counted when there is no camera data. Crops and exports can match.
- `no-metadata` (score 6, weight 0.1): the file has no EXIF, XMP, text or provenance data. WhatsApp, Instagram, X and most screenshots strip metadata from real photos, so this is nearly noise. It exists so the report can say it looked.
- `filename-hint` (score 9, weight 0.4-0.5): the name follows a generator's download pattern (`ChatGPT Image ...`, `Gemini_Generated_Image_...`, `DALL·E ...`, `00001-123456789-...`). Names are easily changed, but keeping the default one is a real hint.

## 6. Informative only (weight 0)

`c2pa-present` (a manifest exists but no source type was found), `exif-software-editor` and `xmp-editor` (Photoshop, Lightroom, GIMP, Canva, ...), `exif-modified-later` (saved long after capture). They show the history of the file without implying AI.

## What this version does not look at

- **Pixels.** Noise patterns, error level analysis, frequency artifacts, double-compression and copy-move detection need an imaging library; they are planned as an optional module.
- **Invisible watermarks** such as Google's SynthID or Meta's Stable Signature: only the vendor's own detector can read them.
- **C2PA validity**: signature chain, tampering and revocation.
- **Containers beyond PNG, JPEG, WebP and GIF**: HEIC and AVIF get only a generic scan for XMP and C2PA bytes.
