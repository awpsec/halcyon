from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import secrets

TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
RECOVERY_WORDLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "eff_large_wordlist.txt"
SESSION_TOKEN_PREFIX = "sha384:"
FALLBACK_RECOVERY_WORDS = (
    "anchor", "apricot", "ash", "aster", "atlas", "aurora", "badger", "bamboo",
    "barley", "beacon", "birch", "bison", "bramble", "breeze", "brook", "canyon",
    "caper", "cedar", "chisel", "cinder", "citrus", "clover", "cobalt", "copper",
    "coral", "cricket", "current", "dahlia", "delta", "ember", "falcon", "fern",
    "fable", "fjord", "flint", "forest", "frost", "garnet", "glade", "golden",
    "harbor", "hazel", "heather", "hollow", "indigo", "iris", "ivory", "jade",
    "jasper", "juniper", "kestrel", "lagoon", "laurel", "linen", "lotus", "mango",
    "maple", "marble", "meadow", "meridian", "meteor", "mistral", "misty", "monarch",
    "moss", "nectar", "nova", "onyx", "opal", "orchid", "otter", "pearl",
    "pepper", "pine", "plume", "prairie", "quartz", "quill", "raven", "reef",
    "ripple", "river", "robin", "saffron", "sage", "sierra", "silver", "solstice",
    "sparrow", "spruce", "starling", "stone", "summit", "sunrise", "thistle", "timber",
    "topaz", "trident", "tulip", "umber", "valley", "velvet", "violet", "willow",
    "winter", "wren", "yarrow", "zephyr",
)


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
    try:
        with RECOVERY_WORDLIST_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) != 2:
                    continue
                word = parts[1].strip().lower()
                if word.isalpha():
                    words.append(word)
    except FileNotFoundError:
        words.extend(FALLBACK_RECOVERY_WORDS)
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


def hash_session_token(token: str) -> str:
    digest = hashlib.sha384(token.encode("utf-8")).hexdigest()
    return f"{SESSION_TOKEN_PREFIX}{digest}"


def is_hashed_session_token(stored_token: str | None) -> bool:
    return bool(stored_token and stored_token.startswith(SESSION_TOKEN_PREFIX))
