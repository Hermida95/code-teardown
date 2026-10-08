"""Synthetic test images with known pixel properties (needs NumPy and Pillow). No real images are stored."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image


def smooth_field(n: int, seed: int, cutoff: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spectrum = np.fft.fft2(rng.standard_normal((n, n)))
    fx, fy = np.fft.fftfreq(n)[:, None], np.fft.fftfreq(n)[None, :]
    spectrum *= np.exp(-(fx ** 2 + fy ** 2) / (2 * cutoff ** 2))
    field = np.real(np.fft.ifft2(spectrum))
    return (field - field.min()) / (field.max() - field.min())


def scene(n: int = 768, seed: int = 1) -> np.ndarray:
    """A smooth, textured, photo-like luminance field in 40..210."""
    return 40 + (smooth_field(n, seed, 0.03) * 0.7 + smooth_field(n, seed + 7, 0.12) * 0.3) * 170


def save(pixels: np.ndarray, path, **kwargs) -> None:
    grey = np.clip(np.round(pixels), 0, 255).astype(np.uint8)
    Image.fromarray(grey).convert("RGB").save(path, **kwargs)


def jpeg_roundtrip(pixels: np.ndarray, quality: int) -> np.ndarray:
    buffer = io.BytesIO()
    save(pixels, buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return np.asarray(Image.open(buffer).convert("L"), dtype=np.float32)


def noisy(sigma: float, n: int = 768, seed: int = 1) -> np.ndarray:
    return scene(n, seed) + np.random.default_rng(seed + 100).normal(0, sigma, (n, n))
