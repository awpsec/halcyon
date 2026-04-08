from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import secrets

TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
RECOVERY_WORDLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "eff_large_wordlist.txt"


def hash_secret(secret: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{derived.hex()}"


def verify_secret(secret: str, stored_hash: str | None) -> bool:
    if not stored_hash or ":" not in stored_hash:
        return False
    salt_hex, digest_hex = stored_hash.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 120_000)
    return secrets.compare_digest(derived.hex(), digest_hex)


def hash_password(password: str) -> str:
    return hash_secret(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    return verify_secret(password, stored_hash)


def normalize_recovery_phrase(phrase: str) -> str:
    return " ".join(part for part in phrase.strip().lower().split() if part)


@lru_cache(maxsize=1)
def _recovery_wordlist() -> tuple[str, ...]:
    words: list[str] = []
    with RECOVERY_WORDLIST_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            word = parts[1].strip().lower()
            if word.isalpha():
                words.append(word)
    if not words:
        raise RuntimeError("Recovery word list is empty")
    return tuple(words)


def generate_recovery_phrase(word_count: int = 6) -> str:
    count = max(1, word_count)
    words: list[str] = []
    seen: set[str] = set()
    available = _recovery_wordlist()
    while len(words) < count:
        word = secrets.choice(available)
        if word in seen:
            continue
        seen.add(word)
        words.append(word)
    return " ".join(words)


def hash_recovery_phrase(phrase: str) -> str:
    return hash_secret(normalize_recovery_phrase(phrase))


def verify_recovery_phrase(phrase: str, stored_hash: str | None) -> bool:
    return verify_secret(normalize_recovery_phrase(phrase), stored_hash)


def generate_temporary_password(length: int = 18) -> str:
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(max(12, length)))
