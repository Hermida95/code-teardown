#!/usr/bin/env python3
"""Optional pixel module for the image pipeline: measure what the pixels say.

Needs Pillow and NumPy (everything else in ai-evidence is standard library only). It measures
three things on the luma channel at native resolution and turns them into evidence items in the
same format as analyze_image.py:

  * noise: level and spatial consistency (sensor-like noise vs. a clean image, and regions whose
    noise differs from the rest, which can mark a pasted or regenerated area)
  * periodic artifacts: peaks at 1/2, 1/4, 1/8 and 1/16 of the sampling rate, left by upsamplers
    and decoders (a JPEG block grid also makes peaks at 1/8, which is accounted for)
  * error level analysis (JPEG only): regions that recompress differently from the rest

These are heuristics and they are NOT calibrated on real-world data, so every scored item is
capped at weight 0.25 and nothing here can decide a result by itself. Each measurement is always
reported, scored or not, so a reader can judge it.

Usage: analyze_pixels.py IMAGE [--out pixels.json] [--force]
Exit status 3 means Pillow or NumPy is missing (nothing is installed for you).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import io
import json
import math
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import checked_output_path, dump  # noqa: E402
from analyze_image import T, ev  # noqa: E402

try:
    import numpy as np
    from PIL import Image
except ImportError:  # the module is optional: main() explains how to get it
    np = Image = None

VERSION = "0.1.0"
PIXEL_CAP = 0.25            # scoring.py enforces the same cap on whatever it reads
MAX_PIXELS = 50_000_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MIN_SIDE = 260
BLOCK = 32
ELA_BLOCK = 16
NOISE_CROP = 2048
ELA_CROP = 1024

# thresholds: reasoned guesses checked on synthetic images only (see references/pixel-signals.md)
CLEAN_SIGMA = 0.6
NOISY_SIGMA = 2.5
OUTLIER_FACTOR = 3.0
PEAK_RATIO = 15.0
ELA_FACTOR = 3.0
MIN_UNIQUE_LUMA = 64
MIN_COLOURS = 1500      # distinct colours in a 256x256 sample; photographs have far more than graphics

# the standard JPEG luminance quantization table, used only to estimate a quality number
STD_LUMA_Q = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
              14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
              49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99]


def px(id: str, claim: str, score: float, weight: float, where, title: dict, detail: dict) -> dict:
    return ev(id, claim, score, min(weight, PIXEL_CAP), where, title, detail, source="pixels")


# --- loading ----------------------------------------------------------------------------------

def load(path: Path) -> dict:
    """Open the image defensively and return native-resolution crops. Raises ValueError with a clean message."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as im:
                fmt = im.format or "unknown"
                mode = im.mode
                width, height = im.size
                if width * height > MAX_PIXELS:
                    raise ValueError(f"image has {width * height:,} pixels; the limit is {MAX_PIXELS:,}")
                quantization = dict(getattr(im, "quantization", None) or {})
                im.load()
                rgb = im.convert("RGB")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"image is too large to analyze safely ({exc})") from exc
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ValueError(f"cannot read the image: {exc}") from exc
    return {"format": fmt, "mode": mode, "size": (width, height), "rgb": rgb, "quantization": quantization}


def centre_box(width: int, height: int, size: int, align: int = 1) -> tuple[int, int, int, int]:
    w, h = min(size, width), min(size, height)
    left, top = (width - w) // 2 // align * align, (height - h) // 2 // align * align
    return left, top, left + w, top + h


def luma_array(rgb, box: tuple[int, int, int, int]):
    return np.asarray(rgb.crop(box).convert("L"), dtype=np.float32)


def estimate_jpeg_quality(quantization: dict) -> int | None:
    table = quantization.get(0)
    if not table or len(table) != 64:
        return None
    scale = (sum(table) / 64) / (sum(STD_LUMA_Q) / 64) * 100
    quality = (200 - scale) / 2 if scale <= 100 else 5000 / scale
    return int(max(1, min(100, round(quality))))


# --- noise ------------------------------------------------------------------------------------

def laplace(x):
    """Response of the 3x3 mask used by Immerkaer's noise estimator. Output is 2 pixels smaller."""
    return (x[:-2, :-2] - 2 * x[:-2, 1:-1] + x[:-2, 2:] - 2 * x[1:-1, :-2] + 4 * x[1:-1, 1:-1]
            - 2 * x[1:-1, 2:] + x[2:, :-2] - 2 * x[2:, 1:-1] + x[2:, 2:])


def structure_grid(luma, rows: int, cols: int):
    """Edge/texture energy per 32x32 block, measured on 8x8-pooled pixels so that noise does not count as structure."""
    h, w = luma.shape
    hp, wp = h // 8, w // 8
    pooled = luma[:hp * 8, :wp * 8].reshape(hp, 8, wp, 8).mean(axis=(1, 3))
    grad = np.abs(np.diff(pooled, axis=1))[:-1, :] + np.abs(np.diff(pooled, axis=0))[:, :-1]
    step = BLOCK // 8
    r, c = min(rows, grad.shape[0] // step), min(cols, grad.shape[1] // step)
    out = np.full((rows, cols), np.nan)
    out[:r, :c] = grad[:r * step, :c * step].reshape(r, step, c, step).mean(axis=(1, 3))
    return out


def noise_grid(luma):
    """Noise sigma per 32x32 block over flat, non-clipped blocks (NaN elsewhere)."""
    lap = np.abs(laplace(luma))
    inner = luma[1:-1, 1:-1]
    h, w = lap.shape
    hh, ww = h // BLOCK * BLOCK, w // BLOCK * BLOCK
    if hh == 0 or ww == 0:
        return None

    def blocks(a, reduce=np.mean):
        return reduce(a[:hh, :ww].reshape(hh // BLOCK, BLOCK, ww // BLOCK, BLOCK), axis=(1, 3))

    # The median of |response| ignores the few edge pixels a block may contain; the mean would not.
    lap_b, mean_b = blocks(lap, np.median), blocks(inner)
    structure = structure_grid(luma[1:-1, 1:-1], hh // BLOCK, ww // BLOCK)
    valid = (mean_b > 12) & (mean_b < 243) & ~np.isnan(structure)
    if valid.sum() < 16:
        return None
    flat = valid & (structure <= np.median(structure[valid]))
    sigma = lap_b / (0.6745 * 6)   # median of |N(0, 36σ²)| is 0.6745·6σ
    return np.where(flat, sigma, np.nan)


def largest_component(mask) -> int:
    """Most marked cells in one connected region. Regions are joined across a one-cell gap, because blocks
    that were left out as textured can punch holes in an area that really is one patch."""
    grown = mask.copy()
    grown[1:, :] |= mask[:-1, :]
    grown[:-1, :] |= mask[1:, :]
    grown[:, 1:] |= mask[:, :-1]
    grown[:, :-1] |= mask[:, 1:]
    seen = np.zeros(mask.shape, dtype=bool)
    best = 0
    rows, cols = mask.shape
    for r0 in range(rows):
        for c0 in range(cols):
            if not grown[r0, c0] or seen[r0, c0]:
                continue
            stack, marked = [(r0, c0)], 0
            seen[r0, c0] = True
            while stack:
                r, c = stack.pop()
                marked += int(mask[r, c])
                for nr, nc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if 0 <= nr < rows and 0 <= nc < cols and grown[nr, nc] and not seen[nr, nc]:
                        seen[nr, nc] = True
                        stack.append((nr, nc))
            best = max(best, marked)
    return best


def region_outliers(values, factor: float, floor: float = 0.0) -> dict:
    """Find a compact group of cells that differ from the median by more than `factor` (either way)."""
    valid = ~np.isnan(values)
    if valid.sum() < 16:
        return {"fraction": 0.0, "compact": False, "count": 0}
    med = float(np.median(values[valid]))
    ratio = np.where(valid, (values + floor) / (med + floor), 1.0)
    out = valid & ((ratio > factor) | (ratio < 1 / factor))
    count = int(out.sum())
    fraction = count / int(valid.sum())
    compact = count >= 4 and largest_component(out) >= 0.6 * count
    return {"fraction": fraction, "compact": compact, "count": count}


# --- spectrum ---------------------------------------------------------------------------------

def spectrum_peaks(luma) -> dict:
    """Peak-to-neighbourhood power ratio at 1/p of the sampling rate, for p in 2, 4, 8, 16."""
    h, w = luma.shape
    n = next(s for s in (1024, 512, 256) if s + 2 <= min(h, w))
    top, left = (h - n - 2) // 2, (w - n - 2) // 2
    crop = luma[top:top + n + 2, left:left + n + 2]
    # One second difference per direction: the 2-D mask used for noise is separable and would erase a
    # pattern that varies along one axis only.
    along_x = crop[:, :-2] - 2 * crop[:, 1:-1] + crop[:, 2:]
    along_y = (crop[:-2, :] - 2 * crop[1:-1, :] + crop[2:, :]).T
    window = np.hanning(n).astype(np.float32)
    spectra = [(np.abs(np.fft.rfft(res * window[None, :], axis=1)) ** 2).mean(axis=0) for res in (along_x, along_y)]
    ratios = {}
    for p in (2, 4, 8, 16):
        k, best = n // p, 0.0
        for power in spectra:
            lo = np.concatenate([power[max(1, k - 14):k - 2], power[k + 3:k + 15]])
            if lo.size >= 4:
                best = max(best, float(power[k - 1:k + 2].max() / (np.median(lo) + 1e-9)))
        ratios[p] = best
    mid = np.mean([s[n // 8:n // 4].mean() for s in spectra])
    high = np.mean([s[3 * n // 8:n // 2].mean() for s in spectra])
    return {"ratios": ratios, "hf_ratio": float(high / (mid + 1e-9)), "window": n}


# --- error level analysis -------------------------------------------------------------------

def ela_grid(rgb, box):
    crop = rgb.crop(box)
    buffer = io.BytesIO()
    crop.save(buffer, "JPEG", quality=90)
    buffer.seek(0)
    with Image.open(buffer) as again:
        again = again.convert("RGB")
        diff = np.abs(np.asarray(crop, dtype=np.int16) - np.asarray(again, dtype=np.int16)).mean(axis=2)
    h, w = diff.shape
    hh, ww = h // ELA_BLOCK * ELA_BLOCK, w // ELA_BLOCK * ELA_BLOCK
    if hh == 0 or ww == 0:
        return None
    return diff[:hh, :ww].reshape(hh // ELA_BLOCK, ELA_BLOCK, ww // ELA_BLOCK, ELA_BLOCK).mean(axis=(1, 3))


# --- evidence ---------------------------------------------------------------------------------

def analyze(path: Path) -> dict:
    data = load(path)
    width, height = data["size"]
    rgb, fmt = data["rgb"], data["format"]
    evidence: list[dict] = []
    measurements: list[dict] = []
    limits = [T("These pixel statistics are heuristics that have not been calibrated on real-world images; each scored item is capped at weight 0.25.",
                "Estas estadísticas de píxeles son heurísticas sin calibrar con imágenes reales; cada prueba que puntúa está limitada a peso 0,25.")]

    def measure(label_en: str, label_es: str, value: str) -> None:
        measurements.append({"label": T(label_en, label_es), "value": value})

    def skipped(reason_en: str, reason_es: str) -> dict:
        evidence.append(px("px-skipped", "generated", 5, 0.0, T("whole image", "imagen completa"), T("Pixel analysis skipped", "Análisis de píxeles omitido"),
                           T(reason_en, reason_es)))
        return finish()

    def finish() -> dict:
        return {"tool": "analyze_pixels", "version": VERSION, "available": True,
                "file": {"name": path.name, "format": fmt, "width": width, "height": height},
                "evidence": evidence, "measurements": measurements, "limits": limits,
                "coverage": {"checked": [T("noise level and consistency", "nivel y consistencia del ruido"),
                                         T("periodic artifacts in the spectrum", "artefactos periódicos en el espectro")]
                             + ([T("error level analysis", "análisis de nivel de error")] if fmt == "JPEG" else [])}}

    if min(width, height) < MIN_SIDE:
        return skipped(f"The image is {width}×{height}; measuring pixel statistics needs at least {MIN_SIDE} px on each side.",
                       f"La imagen es de {width}×{height}; medir estadísticas de píxeles requiere al menos {MIN_SIDE} px por lado.")
    luma = luma_array(rgb, centre_box(width, height, NOISE_CROP))
    colours = np.asarray(rgb.crop(centre_box(width, height, NOISE_CROP)).resize((256, 256), Image.NEAREST)).reshape(-1, 3)
    greyscale = bool((colours[:, 0] == colours[:, 1]).all() and (colours[:, 1] == colours[:, 2]).all())
    few_colours = not greyscale and len(np.unique(colours, axis=0)) < MIN_COLOURS   # a black-and-white photo has few colours too
    graphic = data["mode"] in ("P", "1") or len(np.unique(luma.astype(np.uint8))) < MIN_UNIQUE_LUMA or few_colours
    if graphic:
        return skipped("The image looks like a graphic (palette, few colours or brightness levels), where photographic noise statistics do not apply.",
                       "La imagen parece un gráfico (paleta, pocos colores o niveles de brillo), donde las estadísticas de ruido fotográfico no aplican.")

    quality = estimate_jpeg_quality(data["quantization"]) if fmt == "JPEG" else None
    unreliable = quality is not None and quality < 60
    dims = f"{min(width, NOISE_CROP)}×{min(height, NOISE_CROP)}"
    where = T(f"{dims} centre crop", f"recorte central de {dims}")
    if quality is not None:
        measure("Estimated JPEG quality", "Calidad JPEG estimada", f"~{quality}")
        evidence.append(px("px-jpeg-quality", "ai_edited", 5, 0.0, T("JPEG quantization tables", "tablas de cuantización JPEG"),
                           T(f"JPEG saved at about quality {quality}", f"JPEG guardado con calidad aproximada {quality}"),
                           T("Low quality hides noise and edit traces, so noise and error-level measurements are not scored below about 60."
                             if unreliable else "Quality is high enough for the noise and error-level measurements to mean something.",
                             "Una calidad baja oculta el ruido y las huellas de edición, por eso por debajo de 60 aproximadamente las medidas de ruido y nivel de error no puntúan."
                             if unreliable else "La calidad es suficiente para que las medidas de ruido y nivel de error tengan sentido.")))

    # noise level and consistency
    grid = noise_grid(luma)
    if grid is not None:
        sigmas = grid[~np.isnan(grid)]
        level = float(np.median(sigmas))
        measure("Noise level (σ, 0-255 scale)", "Nivel de ruido (σ, escala 0-255)", f"{level:.2f}")
        score, weight = 5.0, 0.0
        title = T(f"Noise level σ ≈ {level:.2f}", f"Nivel de ruido σ ≈ {level:.2f}")
        detail = T("Within the range where noise says nothing either way.", "Dentro del rango en el que el ruido no dice nada en ningún sentido.")
        if not unreliable and level < CLEAN_SIGMA:
            score, weight = 6.5, 0.12
            title = T(f"Almost no noise (σ ≈ {level:.2f})", f"Casi sin ruido (σ ≈ {level:.2f})")
            detail = T("A photo normally carries sensor noise; this one is nearly noise-free, as many generated images are. Denoised phone photos, retouched images and upscaled images are too, so it counts very little.",
                       "Una foto suele llevar ruido de sensor; esta casi no lo tiene, como muchas imágenes generadas. Las fotos de móvil con reducción de ruido, las retocadas y las ampliadas también, así que pesa muy poco.")
        elif not unreliable and level >= NOISY_SIGMA:
            score, weight = 3.0, 0.12
            title = T(f"Sensor-like noise present (σ ≈ {level:.2f})", f"Ruido tipo sensor presente (σ ≈ {level:.2f})")
            detail = T("Fine grain of this strength is typical of camera sensors. Generators and filters can add grain, and fine textures (fabric, foliage, fur) read as noise too, so it counts very little.",
                       "Un grano fino de esta intensidad es típico de los sensores de cámara. Los generadores y los filtros pueden añadir grano, y las texturas finas (tela, vegetación, pelo) también se leen como ruido, así que pesa muy poco.")
        evidence.append(px("px-noise-level", "generated", score, weight, where, title, detail))

        outliers = region_outliers(grid, OUTLIER_FACTOR, floor=0.05)
        flagged = outliers["compact"] and 0.03 <= outliers["fraction"] <= 0.40
        measure("Blocks with unusual noise", "Bloques con ruido anómalo", f"{outliers['fraction'] * 100:.1f}%")
        evidence.append(px("px-noise-inconsistent", "ai_edited", 6.5 if flagged and not unreliable else 5,
                           0.12 if flagged and not unreliable else 0.0, where,
                           T("A compact region has different noise" if flagged else "Noise is consistent across the image",
                             "Una región compacta tiene ruido distinto" if flagged else "El ruido es coherente en toda la imagen"),
                           T(f"{outliers['count']} blocks ({outliers['fraction'] * 100:.0f}% of the measured ones) sit in one area with noise at least {OUTLIER_FACTOR:g}× above or below the median. That can mark a pasted or regenerated area, but also a smooth sky next to foliage or a strong brightness change."
                             if flagged else
                             "No compact region stands out from the median noise.",
                             f"{outliers['count']} bloques ({outliers['fraction'] * 100:.0f}% de los medidos) están en una misma zona con un ruido al menos {OUTLIER_FACTOR:g} veces mayor o menor que la mediana. Puede indicar una zona pegada o regenerada, pero también un cielo liso junto a vegetación o un cambio fuerte de brillo."
                             if flagged else
                             "Ninguna región compacta se aparta del ruido mediano.")))
    else:
        measure("Noise level", "Nivel de ruido", "n/a")

    # periodic artifacts
    peaks = spectrum_peaks(luma)
    ratios = peaks["ratios"]
    lossless = fmt in ("PNG", "WEBP", "BMP", "TIFF")
    hits = [p for p in (2, 4) if ratios[p] >= PEAK_RATIO] + ([p for p in (8, 16) if ratios[p] >= PEAK_RATIO] if lossless else [])
    grid_like = [p for p in (8, 16) if ratios[p] >= PEAK_RATIO] if not lossless else []
    for p in (2, 4, 8, 16):
        measure(f"Spectral peak at 1/{p} of the sampling rate", f"Pico espectral a 1/{p} de la frecuencia de muestreo", f"×{ratios[p]:.1f}")
    if hits:
        strongest = max(hits, key=lambda p: ratios[p])
        weight = 0.2 if strongest in (2, 4) else 0.12
        title = T(f"Periodic pattern with a {strongest}-pixel period", f"Patrón periódico con periodo de {strongest} píxeles")
        detail = T(f"The residual has a spectral peak {ratios[strongest]:.0f}× above its surroundings at 1/{strongest} of the sampling rate. Upsampling layers in generators leave patterns like this; so do resized images, sharpening and some codecs.",
                   f"El residuo tiene un pico espectral {ratios[strongest]:.0f} veces por encima de su entorno a 1/{strongest} de la frecuencia de muestreo. Las capas de ampliación de los generadores dejan patrones así; también lo hacen el cambio de tamaño, el enfoque y algunos códecs.")
        score = 7.0
    else:
        weight, score = 0.0, 5.0
        title = T("No periodic pattern in the spectrum", "Sin patrón periódico en el espectro")
        detail = T("No peak at 1/2, 1/4 (or, for lossless files, 1/8 and 1/16) stands out from its surroundings."
                   + (" A peak at 1/8 or 1/16 would be expected from the JPEG block grid and is not scored." if grid_like else ""),
                   "Ningún pico a 1/2, 1/4 (ni, en archivos sin pérdida, 1/8 y 1/16) destaca de su entorno."
                   + (" Un pico a 1/8 o 1/16 sería esperable por la cuadrícula de bloques de JPEG y no puntúa." if grid_like else ""))
    evidence.append(px("px-periodic-artifact", "generated", score, weight, T(f"{peaks['window']}×{peaks['window']} centre window", f"ventana central de {peaks['window']}×{peaks['window']}"), title, detail))
    measure("High-frequency energy ratio", "Proporción de energía de alta frecuencia", f"{peaks['hf_ratio']:.2f}")
    evidence.append(px("px-spectrum-hf", "generated", 5, 0.0, T(f"{peaks['window']}×{peaks['window']} centre window", f"ventana central de {peaks['window']}×{peaks['window']}"),
                       T(f"High-frequency energy ratio {peaks['hf_ratio']:.2f}", f"Proporción de energía de alta frecuencia {peaks['hf_ratio']:.2f}"),
                       T("Low values mean little fine detail: smoothing, upscaling, denoising or strong compression. Shown for context, never scored.",
                         "Los valores bajos indican poco detalle fino: suavizado, ampliación, reducción de ruido o compresión fuerte. Se muestra como contexto y nunca puntúa.")))

    # error level analysis
    if fmt == "JPEG" and not unreliable and (quality is None or quality <= 99):
        ela = ela_grid(rgb, centre_box(width, height, ELA_CROP, align=16))
        if ela is not None and ela.size >= 16:
            outliers = region_outliers(ela.astype(np.float64), ELA_FACTOR, floor=0.5)
            flagged = outliers["compact"] and 0.02 <= outliers["fraction"] <= 0.40
            measure("Blocks with unusual error level", "Bloques con nivel de error anómalo", f"{outliers['fraction'] * 100:.1f}%")
            evidence.append(px("px-ela", "ai_edited", 6.0 if flagged else 5, 0.12 if flagged else 0.0, T("JPEG recompressed at quality 90", "JPEG recomprimido a calidad 90"),
                               T("A region recompresses differently" if flagged else "Recompression is uniform", "Una región se recomprime distinto" if flagged else "La recompresión es uniforme"),
                               T("A compact area changes more or less than the rest when the JPEG is saved again. That can mark an edit, but textured areas, saturated colours and sharp edges also do it."
                                 if flagged else "No compact area stands out when the JPEG is saved again.",
                                 "Una zona compacta cambia más o menos que el resto al volver a guardar el JPEG. Puede indicar una edición, pero las zonas con textura, los colores saturados y los bordes marcados también lo hacen."
                                 if flagged else "Ninguna zona compacta destaca al volver a guardar el JPEG.")))
    return finish()


def main() -> None:
    parser = argparse.ArgumentParser(description="Optional pixel measurements for ai-evidence (needs Pillow and NumPy).")
    parser.add_argument("image")
    parser.add_argument("--out", help="write the JSON to this .json file instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = parser.parse_args()
    if np is None or Image is None:
        sys.stderr.write("analyze_pixels.py needs Pillow and NumPy, which are not installed. This module is optional; "
                         "to run it without touching your environment: "
                         "uv run --with pillow --with numpy python analyze_pixels.py IMAGE\n")
        raise SystemExit(3)
    path = Path(args.image).expanduser()
    if not path.is_file():
        sys.exit(f"error: {path} is not a file")
    try:
        result = analyze(path)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    if args.out:
        out = checked_output_path(args.out, ".json", args.force)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(result['evidence'])} evidence items)")
    else:
        dump(result)


if __name__ == "__main__":
    main()
