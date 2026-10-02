"""Pure helpers for turning URLs into short codes."""
import hashlib
from urllib.parse import urlparse

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def make_code(url: str, length: int = 6) -> str:
    digest = hashlib.md5(url.encode("utf-8")).digest()
    number = int.from_bytes(digest, "big")
    chars = []
    for _ in range(length):
        number, index = divmod(number, len(ALPHABET))
        chars.append(ALPHABET[index])
    return "".join(chars)
