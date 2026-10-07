"""Build tiny synthetic image files with chosen metadata. No real images are stored in the repo."""
from __future__ import annotations

import struct
import zlib


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width: int = 64, height: int = 64, *, text: dict | None = None, ztxt: dict | None = None,
        itxt: dict | None = None, xmp: str | None = None, c2pa: bytes | None = None, exif: bytes | None = None) -> bytes:
    out = b"\x89PNG\r\n\x1a\n"
    out += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    for key, value in (text or {}).items():
        out += png_chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1"))
    for key, value in (ztxt or {}).items():
        out += png_chunk(b"zTXt", key.encode("latin-1") + b"\x00\x00" + zlib.compress(value.encode("latin-1")))
    for key, value in (itxt or {}).items():
        out += png_chunk(b"iTXt", key.encode() + b"\x00\x00\x00\x00\x00" + value.encode())
    if xmp:
        out += png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + xmp.encode())
    if exif:
        out += png_chunk(b"eXIf", exif)
    if c2pa:
        out += png_chunk(b"caBX", c2pa)
    out += png_chunk(b"IDAT", zlib.compress(b"\x00" * (1 + width * 3)))
    return out + png_chunk(b"IEND", b"")


def jpeg_segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def jpeg(width: int = 64, height: int = 64, *, exif: bytes | None = None, xmp: str | None = None,
         app11: bytes | None = None, comment: str | None = None) -> bytes:
    out = b"\xff\xd8"
    if exif:
        out += jpeg_segment(0xE1, b"Exif\x00\x00" + exif)
    if xmp:
        out += jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode())
    if app11:
        out += jpeg_segment(0xEB, app11)
    if comment:
        out += jpeg_segment(0xFE, comment.encode("latin-1"))
    out += jpeg_segment(0xC0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")
    return out + jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x00" * 8 + b"\xff\xd9"


def tiff(ifd0: dict | None = None, exif: dict | None = None, big_endian: bool = False) -> bytes:
    """ifd0/exif map tag -> str | int(SHORT) | (num, den) RATIONAL | bytes(UNDEFINED)."""
    e = ">" if big_endian else "<"
    header = (b"MM\x00*" if big_endian else b"II*\x00") + struct.pack(e + "I", 8)
    entries0 = dict(ifd0 or {})
    if exif:
        entries0[0x8769] = ("ptr", None)

    def encode(entries: dict, base: int, pointer: int | None) -> tuple[bytes, bytes]:
        table, data = b"", b""
        data_start = base + 2 + len(entries) * 12 + 4
        for tag in sorted(entries):
            val = entries[tag]
            if isinstance(val, tuple) and val[0] == "ptr":
                table += struct.pack(e + "HHII", tag, 4, 1, pointer or 0)
                continue
            if isinstance(val, str):
                raw, typ, count = val.encode("latin-1") + b"\x00", 2, len(val) + 1
            elif isinstance(val, int):
                table += struct.pack(e + "HHIHH", tag, 3, 1, val, 0)
                continue
            elif isinstance(val, tuple):
                raw, typ, count = struct.pack(e + "II", *val), 5, 1
            else:
                raw, typ, count = val, 7, len(val)
            if len(raw) <= 4:
                table += struct.pack(e + "HHI", tag, typ, count) + raw.ljust(4, b"\x00")
            else:
                table += struct.pack(e + "HHII", tag, typ, count, data_start + len(data))
                data += raw
        return struct.pack(e + "H", len(entries)) + table + struct.pack(e + "I", 0), data

    ifd0_len = 2 + len(entries0) * 12 + 4
    _, data0 = encode(entries0, 8, 0)
    exif_offset = 8 + ifd0_len + len(data0)
    block0, data0 = encode(entries0, 8, exif_offset)
    out = header + block0 + data0
    if exif:
        block1, data1 = encode(exif, exif_offset, None)
        out += block1 + data1
    return out


def webp(width: int = 64, height: int = 64, *, xmp: str | None = None) -> bytes:
    def chunk(name: bytes, body: bytes) -> bytes:
        return name + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")
    vp8x = chunk(b"VP8X", bytes([0x04]) + b"\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))
    body = b"WEBP" + vp8x + (chunk(b"XMP ", xmp.encode()) if xmp else b"") + chunk(b"VP8L", b"\x2f\x00\x00\x00\x00")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def jumbf(*strings: str) -> bytes:
    """A stand-in for a C2PA JUMBF box: the strings sit in the bytes like they do in the CBOR payload."""
    payload = b"jumb\x00\x00\x00\x00c2pa\x00" + b"\x00".join(s.encode() for s in strings)
    return b"JP\x00\x00\x00\x00\x00\x01" + payload


IPTC = "http://cv.iptc.org/newscodes/digitalsourcetype/"
