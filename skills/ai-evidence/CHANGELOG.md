# Changelog

## 0.1.0 (unreleased)

- Image pipeline: `analyze_image.py` (provenance and metadata evidence, stdlib only) and `score_evidence.py` (validation, weighting, HTML report).
- Evidence model: score 0-10 and weight 0-1 per item; two assessments (generated, edited with AI) with band and confidence; conclusive items decide and contradictions are reported as conflicts.
- Signals: Stable Diffusion, ComfyUI, Midjourney, SwarmUI, InvokeAI, Fooocus and NovelAI records; C2PA and IPTC/XMP source types; EXIF software and camera data; generator dimensions; file name patterns.
- Model visual inspection with weight caps per kind.
- Optional pixel module (`analyze_pixels.py`, needs Pillow and NumPy): noise level and consistency, periodic upsampling artifacts, JPEG error level analysis. Heuristics checked on synthetic images only; every scored item is capped at weight 0.25 and refused for small images, graphics and low-quality JPEGs. Merged with `score_evidence.py --pixels`.
