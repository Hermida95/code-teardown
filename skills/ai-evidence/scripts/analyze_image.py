#!/usr/bin/env python3
"""Step 1 of the image pipeline: extract provenance evidence from an image file.

Static and standard library only. The image is parsed as bytes: it is never opened with an
imaging library, never executed, and no network request is made. Every finding is an
*evidence item* with a score (0 = not AI, 10 = AI) and a weight (0 = informative only,
1 = conclusive), so that score_evidence.py can combine them.

Usage: analyze_image.py IMAGE [--out evidence.json] [--force]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}). "
             "Try python3.12 or python3.11, or: uv run --python 3.12 <script>")

import argparse
import datetime
import hashlib
import json
import re
import struct
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import checked_output_path, clean_quote, dump  # noqa: E402

VERSION = "0.1.0"
MAX_READ = 64 * 1024 * 1024
MAX_TEXT = 256 * 1024
MAX_CHUNKS = 20000
MAX_IFD_ENTRIES = 500
MAX_VALUE_BYTES = 65536

CONCLUSIVE = 0.9

# Dimensions that image generators emit by default (either orientation). Real cameras and
# phones almost never produce them, but crops and exports can, so this is weak evidence.
GENERATOR_SIZES = {
    (512, 512), (768, 768), (1024, 1024), (1536, 1536), (2048, 2048),
    (512, 768), (768, 1152), (1024, 1536), (1024, 1792), (832, 1216), (896, 1152),
    (1344, 768), (1216, 704), (1344, 896), (1408, 768), (1664, 928),
}

# name, pattern. Matched case-insensitively against software tags, claim generators and free text.
GENERATORS = [
    ("OpenAI (DALL·E / ChatGPT)", r"dall[-·\s]?e|chatgpt|openai|gpt-image"),
    ("Midjourney", r"midjourney"),
    ("Adobe Firefly", r"firefly"),
    ("Stable Diffusion", r"stable[ _-]?diffusion|stability\.ai|\bsdxl\b|dreamstudio"),
    ("Google (Imagen / Gemini)", r"\bimagen\b|\bgemini\b|made with google ai"),
    ("Flux (Black Forest Labs)", r"black[ -]?forest|\bflux\b"),
    ("Leonardo.Ai", r"leonardo\.?ai"),
    ("Ideogram", r"ideogram"),
    ("NovelAI", r"novelai"),
    ("ComfyUI", r"comfyui"),
    ("Automatic1111 / Forge", r"automatic1111|stable-diffusion-webui|sd[- ]?forge"),
    ("Fooocus", r"fooocus"),
    ("InvokeAI", r"invokeai"),
    ("Microsoft (Designer / Bing Image Creator)", r"bing image creator|microsoft designer"),
    ("Meta AI", r"imagined with ai|\bmeta ai\b"),
    ("Runway / Kling / Sora", r"\brunway(ml)?\b|\bkling\b|\bsora\b"),
    ("Recraft", r"recraft"),
    ("Craiyon", r"craiyon"),
    ("Canva Magic Media", r"magic media"),
    ("Lexica / Playground / NightCafe / Krea", r"lexica|playground ?ai|nightcafe|krea\.ai"),
]
# Names that are also ordinary words or names (a runway, a zodiac sign, a person called Sora). They only
# count in fields a tool writes itself (Software, CreatorTool, C2PA claim generator), never in captions.
AMBIGUOUS = {"Google (Imagen / Gemini)", "Flux (Black Forest Labs)", "Adobe Firefly", "Runway / Kling / Sora", "Meta AI",
             "Canva Magic Media", "Lexica / Playground / NightCafe / Krea", "Microsoft (Designer / Bing Image Creator)"}
GENERATOR_RES = [(name, re.compile(pattern, re.I)) for name, pattern in GENERATORS]

EDITORS = re.compile(r"photoshop|lightroom|gimp|affinity|pixelmator|snapseed|photopea|capture one|luminar|"
                     r"canva|picsart|facetune|adobe|paint\.net|krita", re.I)

FILENAME_HINTS = [
    (re.compile(r"^chatgpt image", re.I), "ChatGPT", 0.5),
    (re.compile(r"^gemini[_ -]generated[_ -]image", re.I), "Gemini", 0.5),
    (re.compile(r"^dall[-·\s]?e", re.I), "DALL·E", 0.5),
    (re.compile(r"midjourney|^mj[_-]", re.I), "Midjourney", 0.4),
    (re.compile(r"^leonardo[_-]", re.I), "Leonardo.Ai", 0.4),
    (re.compile(r"ideogram", re.I), "Ideogram", 0.4),
    (re.compile(r"firefly", re.I), "Adobe Firefly", 0.4),
    (re.compile(r"^comfyui[_-]\d+", re.I), "ComfyUI", 0.5),
    (re.compile(r"^\d{5}-\d{6,10}-"), "Automatic1111 (output naming)", 0.4),
]

EDIT_FIELDS = {0x010F: "make", 0x0110: "model", 0x0131: "software", 0x0132: "datetime", 0x013B: "artist",
               0x010E: "description", 0x8298: "copyright"}
EXIF_FIELDS = {0x829A: "exposure_time", 0x829D: "f_number", 0x8827: "iso", 0x9003: "datetime_original",
               0x9004: "datetime_digitized", 0x920A: "focal_length", 0xA434: "lens_model", 0xA433: "lens_make",
               0x9286: "user_comment", 0xA405: "focal_length_35mm"}
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

XMP_JPEG = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_JPEG_EXT = b"http://ns.adobe.com/xmp/extension/\x00"


def T(en: str, es: str) -> dict:
    return {"en": en, "es": es}


def ev(id: str, claim: str, score: float, weight: float, where: str, title: dict, detail: dict,
       quote: str | None = None, source: str = "metadata") -> dict:
    item = {"id": id, "claim": claim, "source": source, "score": score, "weight": weight,
            "where": where, "title": title, "detail": detail}
    if quote:
        item["quote"] = clean_quote(quote)
    return item


# --- container parsing ---------------------------------------------------------------

def safe_inflate(data: bytes, limit: int = MAX_TEXT) -> bytes:
    try:
        return zlib.decompressobj().decompress(data, limit)
    except zlib.error:
        return b""


class Parsed:
    """What the container parsers found, independent of the image format."""

    def __init__(self) -> None:
        self.format = "unknown"
        self.width: int | None = None
        self.height: int | None = None
        self.texts: list[tuple[str, str, str]] = []   # (where, key, text)
        self.exif: bytes | None = None
        self.xmp: list[str] = []
        self.c2pa: list[bytes] = []
        self.segments: list[str] = []
        self.notes: list[str] = []


def parse_png(data: bytes, p: Parsed) -> None:
    p.format = "png"
    pos, count = 8, 0
    while pos + 8 <= len(data) and count < MAX_CHUNKS:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        if length > len(data):
            p.notes.append("png chunk length exceeds the file; parsing stopped")
            break
        body = data[pos + 8:pos + 8 + length]
        name = kind.decode("latin-1", "replace")
        count += 1
        if name == "IHDR" and len(body) >= 8:
            p.width, p.height = struct.unpack(">II", body[:8])
        elif name == "tEXt":
            key, _, value = body.partition(b"\x00")
            p.texts.append((f"PNG tEXt '{key.decode('latin-1')[:40]}'", key.decode("latin-1")[:80],
                            value[:MAX_TEXT].decode("latin-1", "replace")))
        elif name == "zTXt":
            key, _, rest = body.partition(b"\x00")
            p.texts.append((f"PNG zTXt '{key.decode('latin-1')[:40]}'", key.decode("latin-1")[:80],
                            safe_inflate(rest[1:]).decode("latin-1", "replace")))
        elif name == "iTXt":
            key, _, rest = body.partition(b"\x00")
            if len(rest) >= 2:
                compressed, rest = rest[0], rest[2:]
                _, _, rest = rest.partition(b"\x00")      # language tag
                _, _, text = rest.partition(b"\x00")      # translated keyword
                raw = safe_inflate(text) if compressed else text[:MAX_TEXT]
                text_value = raw.decode("utf-8", "replace")
                label = key.decode("utf-8", "replace")[:80]
                if label == "XML:com.adobe.xmp":
                    p.xmp.append(text_value)
                else:
                    p.texts.append((f"PNG iTXt '{label[:40]}'", label, text_value))
        elif name == "eXIf":
            p.exif = body[:MAX_TEXT]
        elif name == "caBX":
            p.c2pa.append(body[:8 * 1024 * 1024])
            p.segments.append("caBX (C2PA)")
        if name not in ("IDAT",) and name not in p.segments:
            p.segments.append(name)
        pos += 12 + length


def parse_jpeg(data: bytes, p: Parsed) -> None:
    p.format = "jpeg"
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if length < 2:
            break
        seg = data[pos + 4:pos + 2 + length]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and len(seg) >= 5:
            p.height, p.width = struct.unpack(">HH", seg[1:5])
        elif marker == 0xE1 and seg.startswith(b"Exif\x00\x00"):
            p.exif = seg[6:]
            p.segments.append("APP1 Exif")
        elif marker == 0xE1 and seg.startswith(XMP_JPEG):
            p.xmp.append(seg[len(XMP_JPEG):MAX_TEXT].decode("utf-8", "replace"))
            p.segments.append("APP1 XMP")
        elif marker == 0xE1 and seg.startswith(XMP_JPEG_EXT):
            p.segments.append("APP1 XMP extension")
        elif marker == 0xEB and seg[:2] == b"JP":
            p.c2pa.append(seg)
            if "APP11 JUMBF (C2PA)" not in p.segments:
                p.segments.append("APP11 JUMBF (C2PA)")
        elif marker == 0xED and seg.startswith(b"Photoshop 3.0"):
            p.segments.append("APP13 Photoshop/IPTC")
        elif marker == 0xFE:
            text = seg[:MAX_TEXT].decode("latin-1", "replace")
            p.texts.append(("JPEG comment", "comment", text))
            p.segments.append("COM")
        pos += 2 + length


def parse_webp(data: bytes, p: Parsed) -> None:
    p.format = "webp"
    pos, count = 12, 0
    while pos + 8 <= len(data) and count < MAX_CHUNKS:
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        name = fourcc.decode("latin-1", "replace")
        count += 1
        if name == "VP8X" and len(body) >= 10:
            p.width = 1 + int.from_bytes(body[4:7], "little")
            p.height = 1 + int.from_bytes(body[7:10], "little")
        elif p.width is not None and name in ("VP8 ", "VP8L"):
            pass    # the VP8X canvas size is the one that counts
        elif name == "VP8 " and len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a":
            p.width = int.from_bytes(body[6:8], "little") & 0x3FFF
            p.height = int.from_bytes(body[8:10], "little") & 0x3FFF
        elif name == "VP8L" and len(body) >= 5 and body[0] == 0x2F:
            bits = int.from_bytes(body[1:5], "little")
            p.width, p.height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        elif name == "EXIF":
            p.exif = body[6:] if body.startswith(b"Exif\x00\x00") else body
            p.exif = p.exif[:MAX_TEXT]
        elif name == "XMP ":
            p.xmp.append(body[:MAX_TEXT].decode("utf-8", "replace"))
        elif name == "C2PA":
            p.c2pa.append(body[:8 * 1024 * 1024])
        if name not in p.segments and name.strip() in ("VP8X", "EXIF", "XMP", "C2PA", "ICCP", "ANIM"):
            p.segments.append(name.strip())
        pos += 8 + size + (size & 1)


def parse_container(data: bytes) -> Parsed:
    p = Parsed()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        parse_png(data, p)
    elif data.startswith(b"\xff\xd8\xff"):
        parse_jpeg(data, p)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        parse_webp(data, p)
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        p.format = "gif"
        p.width, p.height = struct.unpack("<HH", data[6:10])
    elif data[4:8] == b"ftyp":
        brand = data[8:12].decode("latin-1", "replace")
        p.format = "avif" if brand.startswith("avi") else "heif" if brand[:3] in ("hei", "mif", "msf") else "isobmff"
        p.notes.append("ISO-BMFF container: only the generic byte scan was applied")
    else:
        p.notes.append("unrecognized image format: only the generic byte scan was applied")

    # Generic byte scan: containers we do not parse (or metadata appended after the image data).
    if not p.xmp:
        start = data.find(b"<x:xmpmeta")
        if start != -1:
            end = data.find(b"</x:xmpmeta>", start, start + MAX_TEXT)
            if end != -1:
                p.xmp.append(data[start:end + 12].decode("utf-8", "replace"))
    if not p.c2pa and b"jumb" in data and b"c2pa" in data:
        index = data.find(b"c2pa")
        p.c2pa.append(data[max(0, index - 4096):index + 1024 * 1024])
        p.segments.append("JUMBF/C2PA (generic scan)")
    return p


# --- EXIF ------------------------------------------------------------------------------

def parse_tiff(buf: bytes) -> dict:
    """Read the handful of EXIF tags that matter here. Bounds-checked; never raises."""
    if len(buf) < 8 or buf[:2] not in (b"II", b"MM"):
        return {}
    e = "<" if buf[:2] == b"II" else ">"
    if struct.unpack_from(e + "H", buf, 2)[0] != 42:
        return {}
    out: dict = {}
    seen: set[int] = set()

    def value(typ: int, count: int, raw: bytes):
        if typ == 2:
            return raw.split(b"\x00")[0].decode("latin-1", "replace").strip()
        if typ == 3:
            return list(struct.unpack(e + f"{len(raw) // 2}H", raw[: len(raw) // 2 * 2]))
        if typ in (4, 9):
            return list(struct.unpack(e + f"{len(raw) // 4}{'I' if typ == 4 else 'i'}", raw[: len(raw) // 4 * 4]))
        if typ in (5, 10):
            fmt = "II" if typ == 5 else "ii"
            pairs = [struct.unpack(e + fmt, raw[i:i + 8]) for i in range(0, len(raw) - 7, 8)]
            return [n / d if d else 0.0 for n, d in pairs]
        return raw

    def read_ifd(offset: int, names: dict, group: str) -> None:
        if offset in seen or offset < 8 or offset + 2 > len(buf):
            return
        seen.add(offset)
        entries = min(struct.unpack_from(e + "H", buf, offset)[0], MAX_IFD_ENTRIES)
        for i in range(entries):
            at = offset + 2 + i * 12
            if at + 12 > len(buf):
                break
            tag, typ, count = struct.unpack_from(e + "HHI", buf, at)
            size = TYPE_SIZE.get(typ)
            if not size:
                continue
            total = size * count
            if total > MAX_VALUE_BYTES:
                continue
            where = at + 8 if total <= 4 else struct.unpack_from(e + "I", buf, at + 8)[0]
            if where + total > len(buf):
                continue
            val = value(typ, count, buf[where:where + total])
            if tag == 0x8769 and group == "ifd0" and isinstance(val, list) and val:
                read_ifd(val[0], EXIF_FIELDS, "exif")
            elif tag == 0x8825 and group == "ifd0":
                out["gps"] = True
            elif tag in names:
                out[names[tag]] = val

    read_ifd(struct.unpack_from(e + "I", buf, 4)[0], EDIT_FIELDS, "ifd0")
    return out


def decode_user_comment(raw) -> str:
    if not isinstance(raw, bytes):
        return str(raw)
    prefix, body = raw[:8], raw[8:]
    if prefix.startswith(b"UNICODE"):
        candidates = []
        for enc in ("utf-16", "utf-16-be", "utf-16-le"):
            try:
                text = body.decode(enc).replace("\x00", "")
            except UnicodeDecodeError:
                continue
            candidates.append((sum(32 <= ord(c) < 127 for c in text), text))
            if enc == "utf-16" and body[:2] in (b"\xff\xfe", b"\xfe\xff"):
                break
        if candidates:
            return max(candidates, key=lambda c: c[0])[1]
    return body.decode("utf-8", "replace").replace("\x00", "")


def exif_datetime(text) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(text).strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


# --- evidence ----------------------------------------------------------------------------

def generator_names(text: str, tool_field: bool = True) -> list[str]:
    """Generators named in text. `tool_field` is False for free text such as captions and comments."""
    return [name for name, pattern in GENERATOR_RES
            if (tool_field or name not in AMBIGUOUS) and pattern.search(text)]


def text_signatures(texts: list[tuple[str, str, str]]) -> list[dict]:
    """Evidence from free-text metadata fields (PNG text chunks, EXIF comments, JPEG comments)."""
    found: list[dict] = []
    used: set[str] = set()

    def add(item: dict) -> None:
        if item["id"] not in used:
            used.add(item["id"])
            found.append(item)

    for where, key, text in texts:
        if re.search(r"\bSteps:\s*\d+", text) and re.search(r"\b(Sampler|CFG scale|Seed):", text):
            add(ev("sd-parameters", "generated", 10, 0.97, where,
                   T("Stable Diffusion generation parameters", "Parámetros de generación de Stable Diffusion"),
                   T("The metadata holds the typical 'Steps / Sampler / CFG scale / Seed' block written by Stable Diffusion front ends.",
                     "Los metadatos contienen el bloque típico 'Steps / Sampler / CFG scale / Seed' que escriben las interfaces de Stable Diffusion."),
                   quote=text[:160]))
        if '"class_type"' in text and ('"inputs"' in text or "KSampler" in text):
            add(ev("comfyui-graph", "generated", 10, 0.97, where,
                   T("ComfyUI workflow embedded", "Flujo de trabajo de ComfyUI incrustado"),
                   T("The file carries a ComfyUI node graph (class_type / inputs).",
                     "El archivo lleva un grafo de nodos de ComfyUI (class_type / inputs)."),
                   quote=text[:160]))
        if "sui_image_params" in text:
            add(ev("swarmui-params", "generated", 10, 0.97, where,
                   T("SwarmUI generation parameters", "Parámetros de generación de SwarmUI"),
                   T("The file carries SwarmUI's 'sui_image_params' block.", "El archivo lleva el bloque 'sui_image_params' de SwarmUI."),
                   quote=text[:160]))
        if key.lower() in ("invokeai_metadata", "sd-metadata", "invokeai_graph") or "invokeai" in text.lower()[:400]:
            add(ev("invokeai-metadata", "generated", 10, 0.97, where,
                   T("InvokeAI metadata", "Metadatos de InvokeAI"),
                   T("The file carries InvokeAI generation metadata.", "El archivo lleva metadatos de generación de InvokeAI."),
                   quote=text[:160]))
        if "fooocus_scheme" in text or "Fooocus V2" in text:
            add(ev("fooocus-params", "generated", 10, 0.97, where,
                   T("Fooocus generation parameters", "Parámetros de generación de Fooocus"),
                   T("The file carries Fooocus generation metadata.", "El archivo lleva metadatos de generación de Fooocus."),
                   quote=text[:160]))
        if re.search(r"Job ID:\s*[0-9a-f]{8}-[0-9a-f]{4}-", text, re.I) and re.search(r"--(ar|v|stylize|style|s)\s", text):
            add(ev("midjourney-job", "generated", 10, 0.97, where,
                   T("Midjourney job record", "Registro de trabajo de Midjourney"),
                   T("The description holds a prompt with Midjourney parameters and a 'Job ID'.",
                     "La descripción contiene un prompt con parámetros de Midjourney y un 'Job ID'."),
                   quote=text[:160]))
        if key == "Software" and re.fullmatch(r"\s*NovelAI\s*", text, re.I):
            add(ev("novelai-software", "generated", 10, 0.95, where,
                   T("Software tag says NovelAI", "La etiqueta Software indica NovelAI"),
                   T("The PNG 'Software' field names NovelAI, an image generator.", "El campo 'Software' del PNG nombra a NovelAI, un generador de imágenes."),
                   quote=text))
    if not found:
        for where, key, text in texts:
            names = generator_names(text, tool_field=False)
            if names:
                add(ev("generator-mention", "generated", 8.5, 0.6, where,
                       T(f"Metadata mentions {', '.join(names)}", f"Los metadatos mencionan {', '.join(names)}"),
                       T("A free-text field names an AI generator. It could be a human caption, so it is strong but not conclusive.",
                         "Un campo de texto libre nombra un generador de IA. Podría ser una descripción humana, así que es fuerte pero no concluyente."),
                       quote=text[:160]))
                break
    return found


def c2pa_evidence(blobs: list[bytes]) -> list[dict]:
    out: list[dict] = []
    if not blobs:
        return out
    low = b"\n".join(blobs).lower()
    where = "C2PA / Content Credentials manifest"
    names = generator_names(low.decode("latin-1", "replace"))
    tail_en = "The signature was NOT verified here; validate it with c2patool or contentcredentials.org/verify."
    tail_es = "La firma NO se ha verificado aquí; valídala con c2patool o contentcredentials.org/verify."
    trained = re.search(rb"(?<![a-z])trainedalgorithmicmedia", low)
    composite = re.search(rb"compositewithtrainedalgorithmicmedia", low)
    out.append(ev("c2pa-present", "generated", 5, 0.0, where,
                  T("Content Credentials (C2PA) manifest present", "Manifiesto de Content Credentials (C2PA) presente"),
                  T("The file embeds a provenance manifest. " + tail_en, "El archivo incrusta un manifiesto de procedencia. " + tail_es)))
    if trained:
        who = f" (generator: {', '.join(names)})" if names else ""
        who_es = f" (generador: {', '.join(names)})" if names else ""
        out.append(ev("c2pa-trained-media", "generated", 10, 0.95, where,
                      T("C2PA declares AI-generated media" + who, "C2PA declara contenido generado por IA" + who_es),
                      T("The manifest holds the IPTC source type 'trainedAlgorithmicMedia'. " + tail_en,
                        "El manifiesto contiene el tipo de fuente IPTC 'trainedAlgorithmicMedia'. " + tail_es)))
    if composite:
        out.append(ev("c2pa-composite", "ai_edited", 9, 0.9, where,
                      T("C2PA declares a composite with AI-generated parts", "C2PA declara un compuesto con partes generadas por IA"),
                      T("The manifest holds 'compositeWithTrainedAlgorithmicMedia': part of the image was made or changed by a model. " + tail_en,
                        "El manifiesto contiene 'compositeWithTrainedAlgorithmicMedia': parte de la imagen la hizo o cambió un modelo. " + tail_es)))
    if not trained and not composite:
        if re.search(rb"(?<![a-z])digitalcapture|computationalcapture", low):
            out.append(ev("c2pa-capture", "generated", 1, 0.7, where,
                          T("C2PA declares a camera capture", "C2PA declara una captura de cámara"),
                          T("The manifest declares 'digitalCapture' or 'computationalCapture'. " + tail_en,
                            "El manifiesto declara 'digitalCapture' o 'computationalCapture'. " + tail_es)))
        elif names:
            out.append(ev("c2pa-generator-named", "generated", 8.5, 0.6, where,
                          T(f"C2PA manifest names {', '.join(names)}", f"El manifiesto C2PA nombra a {', '.join(names)}"),
                          T("A generator is named but no AI source type was found; the manifest may belong to an edit. " + tail_en,
                            "Se nombra un generador pero no se encontró el tipo de fuente IA; el manifiesto puede ser de una edición. " + tail_es)))
    return out


def xmp_evidence(xmps: list[str]) -> list[dict]:
    out: list[dict] = []
    if not xmps:
        return out
    text = "\n".join(xmps)[:MAX_TEXT]
    low = text.lower()
    where = "XMP packet"
    if re.search(r"(?<![a-z])trainedalgorithmicmedia", low):
        out.append(ev("xmp-trained-media", "generated", 10, 0.95, where,
                      T("XMP/IPTC declares AI-generated media", "XMP/IPTC declara contenido generado por IA"),
                      T("The IPTC DigitalSourceType is 'trainedAlgorithmicMedia'. It is self-declared by the tool, and anyone can edit or remove it.",
                        "El DigitalSourceType de IPTC es 'trainedAlgorithmicMedia'. Lo declara la propia herramienta y cualquiera puede editarlo o quitarlo.")))
    elif "compositewithtrainedalgorithmicmedia" in low:
        out.append(ev("xmp-composite", "ai_edited", 9, 0.9, where,
                      T("XMP/IPTC declares AI-edited content", "XMP/IPTC declara contenido editado con IA"),
                      T("The IPTC DigitalSourceType is 'compositeWithTrainedAlgorithmicMedia'.",
                        "El DigitalSourceType de IPTC es 'compositeWithTrainedAlgorithmicMedia'.")))
    elif re.search(r"digitalsourcetype/(digitalcapture|computationalcapture)", low):
        out.append(ev("xmp-capture", "generated", 2, 0.4, where,
                      T("XMP/IPTC declares a camera capture", "XMP/IPTC declara una captura de cámara"),
                      T("The DigitalSourceType says capture. It is self-declared and easy to forge.",
                        "El DigitalSourceType indica captura. Es una declaración propia y fácil de falsificar.")))
    tool = re.search(r"xmp:CreatorTool(?:>|=\")([^<\"]{1,200})", text)
    if tool:
        names = generator_names(tool.group(1))
        if names:
            out.append(ev("xmp-creator-tool", "generated", 9.5, 0.85, where,
                          T(f"XMP creator tool names {', '.join(names)}", f"La herramienta creadora en XMP nombra a {', '.join(names)}"),
                          T("The 'xmp:CreatorTool' field names an AI generator.", "El campo 'xmp:CreatorTool' nombra a un generador de IA."),
                          quote=tool.group(1)))
        elif EDITORS.search(tool.group(1)):
            out.append(ev("xmp-editor", "ai_edited", 5, 0.0, where,
                          T("The image went through an editing program", "La imagen pasó por un programa de edición"),
                          T("'xmp:CreatorTool' names an editor. That alone does not mean AI tools were used.",
                            "'xmp:CreatorTool' nombra un editor. Eso solo no implica que se usaran herramientas de IA."),
                          quote=tool.group(1)))
    agents = re.findall(r"stEvt:softwareAgent(?:>|=\")([^<\"]{1,120})", text)
    for agent in agents[:5]:
        names = generator_names(agent)
        if names:
            out.append(ev("xmp-history-generator", "ai_edited", 8.5, 0.7, where,
                          T(f"XMP edit history names {', '.join(names)}", f"El historial de edición XMP nombra a {', '.join(names)}"),
                          T("A step of the edit history was done by an AI tool.", "Un paso del historial de edición lo hizo una herramienta de IA."),
                          quote=agent))
            break
    return out


def exif_evidence(exif: dict) -> list[dict]:
    out: list[dict] = []
    if not exif:
        return out
    where = "EXIF"
    software = exif.get("software")
    if software:
        names = generator_names(software)
        if names:
            out.append(ev("exif-software-generator", "generated", 9.5, 0.85, where,
                          T(f"EXIF Software names {', '.join(names)}", f"El campo Software de EXIF nombra a {', '.join(names)}"),
                          T("The Software tag names an AI generator.", "La etiqueta Software nombra a un generador de IA."), quote=software))
        elif EDITORS.search(software):
            out.append(ev("exif-software-editor", "ai_edited", 5, 0.0, where,
                          T("The image went through an editing program", "La imagen pasó por un programa de edición"),
                          T("The Software tag names an editor. That alone does not mean AI tools were used.",
                            "La etiqueta Software nombra un editor. Eso solo no implica que se usaran herramientas de IA."), quote=software))
    capture = [k for k in ("exposure_time", "f_number", "iso", "focal_length", "lens_model", "datetime_original") if exif.get(k)]
    if exif.get("make") and exif.get("model") and len(capture) >= 3:
        label = f"{exif['make']} {exif['model']}".strip()
        out.append(ev("exif-camera", "generated", 1, 0.5, where,
                      T("Complete camera capture data", "Datos de captura de cámara completos"),
                      T(f"Make/model plus {len(capture)} capture fields ({', '.join(capture)}). Generators do not write this, but EXIF can be copied or forged.",
                        f"Marca/modelo y {len(capture)} campos de captura ({', '.join(capture)}). Los generadores no lo escriben, pero el EXIF se puede copiar o falsificar."),
                      quote=label))
    elif exif.get("make") and exif.get("model"):
        out.append(ev("exif-camera-partial", "generated", 2.5, 0.25, where,
                      T("Camera make and model only", "Solo marca y modelo de cámara"),
                      T("Make and model are present without exposure data. Weaker than a full capture record.",
                        "Hay marca y modelo sin datos de exposición. Más débil que un registro de captura completo."),
                      quote=f"{exif['make']} {exif['model']}"))
    taken, changed = exif_datetime(exif.get("datetime_original")), exif_datetime(exif.get("datetime"))
    if taken and changed and abs((changed - taken).total_seconds()) > 86400:
        out.append(ev("exif-modified-later", "ai_edited", 5, 0.0, where,
                      T("Modified long after it was captured", "Modificada mucho después de capturarse"),
                      T(f"Captured {taken:%Y-%m-%d}, last modified {changed:%Y-%m-%d}. It was re-saved, which says nothing about AI by itself.",
                        f"Capturada el {taken:%Y-%m-%d}, modificada por última vez el {changed:%Y-%m-%d}. Se volvió a guardar, lo que por sí solo no dice nada sobre IA.")))
    return out


def structure_evidence(p: Parsed, name: str, has_camera: bool, has_any_metadata: bool) -> list[dict]:
    out: list[dict] = []
    if p.width and p.height and not has_camera:
        if (p.width, p.height) in GENERATOR_SIZES or (p.height, p.width) in GENERATOR_SIZES:
            out.append(ev("generator-dimensions", "generated", 6.5, 0.2, f"{p.width}×{p.height}",
                          T("Dimensions typical of image generators", "Dimensiones típicas de generadores de imágenes"),
                          T(f"{p.width}×{p.height} is a default output size of common generators; cameras rarely produce it. Crops and exports can match too.",
                            f"{p.width}×{p.height} es un tamaño de salida habitual de los generadores; las cámaras rara vez lo producen. Recortes y exportaciones también pueden coincidir."),
                          source="structure"))
    if not has_any_metadata and p.format in ("png", "jpeg", "webp"):
        out.append(ev("no-metadata", "generated", 6, 0.1, T("whole file", "archivo completo"),
                      T("No metadata at all", "Sin ningún metadato"),
                      T("No EXIF, XMP, text or provenance data. Messaging apps, social networks and screenshots strip metadata from real photos too, so this barely counts.",
                        "No hay EXIF, XMP, texto ni datos de procedencia. Las apps de mensajería, las redes sociales y las capturas también quitan los metadatos de fotos reales, así que casi no pesa."),
                      source="structure"))
    stem = Path(name).name
    for pattern, label, weight in FILENAME_HINTS:
        if pattern.search(stem):
            out.append(ev("filename-hint", "generated", 9, weight, T("file name", "nombre de archivo"),
                          T(f"File name follows the {label} download pattern", f"El nombre del archivo sigue el patrón de descarga de {label}"),
                          T("File names are easy to change, but keeping a generator's default name is a real hint.",
                            "Los nombres de archivo se cambian fácilmente, pero conservar el nombre por defecto de un generador es un indicio real."),
                          quote=stem, source="filename"))
            break
    return out


def analyze(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(MAX_READ)
    truncated = size > MAX_READ
    parsed = parse_container(data)
    exif = parse_tiff(parsed.exif) if parsed.exif else {}

    texts = list(parsed.texts)
    if exif.get("user_comment") is not None:
        texts.append(("EXIF UserComment", "user_comment", decode_user_comment(exif["user_comment"])))
    for key in ("description", "artist", "copyright"):
        if exif.get(key):
            texts.append((f"EXIF {key}", key, str(exif[key])))
    if exif.get("software"):
        texts.append(("EXIF Software", "Software", str(exif["software"])))

    evidence: list[dict] = []
    evidence += text_signatures(texts)
    evidence += c2pa_evidence(parsed.c2pa)
    evidence += xmp_evidence(parsed.xmp)
    evidence += exif_evidence(exif)

    has_camera = any(e["id"] in ("exif-camera", "exif-camera-partial") for e in evidence)
    has_any_metadata = bool(exif or parsed.xmp or parsed.c2pa or parsed.texts)
    evidence += structure_evidence(parsed, path.name, has_camera, has_any_metadata)

    # The same generator can be reported by several fields; keep the first item per id.
    seen: set[str] = set()
    unique = []
    for item in evidence:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    summary_exif = {k: (v if isinstance(v, (str, int, float, bool)) else str(v)[:80])
                    for k, v in exif.items() if k != "user_comment"}
    limits = [
        T("Pixel-level analysis (noise, compression, frequency artifacts) is not part of this version.",
          "El análisis a nivel de píxel (ruido, compresión, artefactos de frecuencia) no forma parte de esta versión."),
        T("Invisible watermarks such as SynthID cannot be read without the vendor's own detector.",
          "Las marcas de agua invisibles como SynthID no se pueden leer sin el detector del propio proveedor."),
        T("C2PA signatures are detected but not cryptographically verified.",
          "Las firmas C2PA se detectan pero no se verifican criptográficamente."),
        T("Missing metadata proves nothing: most platforms remove it.",
          "La ausencia de metadatos no prueba nada: la mayoría de plataformas los eliminan."),
    ]
    if truncated:
        mb = MAX_READ // (1024 * 1024)
        limits.append(T(f"File is larger than {mb} MB; only the first {mb} MB were read.",
                        f"El archivo supera {mb} MB; solo se leyeron los primeros {mb} MB."))
    limits += [T(note, note) for note in parsed.notes]
    return {
        "tool": "analyze_image", "version": VERSION,
        "file": {"name": path.name, "size_bytes": size, "sha256": hashlib.sha256(data).hexdigest(),
                 "format": parsed.format, "width": parsed.width, "height": parsed.height},
        "metadata_summary": {"exif": summary_exif, "xmp_present": bool(parsed.xmp), "c2pa_present": bool(parsed.c2pa),
                             "text_fields": [w for w, _, _ in parsed.texts][:20], "segments": parsed.segments[:30]},
        "evidence": unique,
        "coverage": {
            "checked": [T("container structure", "estructura del contenedor"), "EXIF", "XMP / IPTC",
                        T("C2PA presence and declared source type", "presencia de C2PA y tipo de fuente declarado"),
                        T("generator signatures in text fields", "firmas de generadores en campos de texto"),
                        T("typical generator dimensions", "dimensiones típicas de generadores"),
                        T("file name", "nombre de archivo")],
            "not_checked": [T("pixel-level forensics", "análisis forense a nivel de píxel"),
                            T("invisible watermarks (SynthID and similar)", "marcas de agua invisibles (SynthID y similares)"),
                            T("C2PA signature validity", "validez de la firma C2PA"),
                            T("the image itself, by eye (done by the model, see SKILL.md step 2)",
                              "la imagen en sí, a ojo (lo hace el modelo, ver paso 2 de SKILL.md)")],
        },
        "limits": limits,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract AI-provenance evidence from an image (static, stdlib only).")
    parser.add_argument("image")
    parser.add_argument("--out", help="write the JSON to this .json file instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = parser.parse_args()
    path = Path(args.image).expanduser()
    if not path.is_file():
        sys.exit(f"error: {path} is not a file")
    result = analyze(path)
    if args.out:
        out = checked_output_path(args.out, ".json", args.force)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(result['evidence'])} evidence items)")
    else:
        dump(result)


if __name__ == "__main__":
    main()
