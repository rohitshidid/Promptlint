"""API keys, passwords and session tokens. Standard library only (hashlib, hmac, secrets).

API keys follow prompt-quality-scorer.md §12:
    pqs_live_<8-char public prefix>_<32 random bytes, base62>
Only the prefix and SHA-256(secret) are stored; the full key is shown once at creation.
"""

import base64
import hashlib
import hmac
import re
import secrets

KEY_PREFIX = "pqs_live_"
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_KEY_RE = re.compile(r"^pqs_live_([0-9A-Za-z]{8})_([0-9A-Za-z]{30,64})$")


def _base62(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(reversed(out)) or "0"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------- API keys
def new_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, secret_hash). Show full_key once; store prefix + hash."""
    prefix = "".join(secrets.choice(_B62) for _ in range(8))
    secret = _base62(secrets.token_bytes(32)).rjust(43, "0")
    return f"{KEY_PREFIX}{prefix}_{secret}", prefix, sha256_hex(secret)


def parse_api_key(key: str) -> tuple[str, str] | None:
    """(prefix, secret) for a well-formed key, else None."""
    m = _KEY_RE.match(key.strip())
    return (m.group(1), m.group(2)) if m else None


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(sha256_hex(secret), stored_hash)


def mask_key(prefix: str) -> str:
    return f"{KEY_PREFIX}{prefix}_••••"


# ---------------------------------------------------------------- passwords (scrypt)
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# A fixed hash to verify against when the email doesn't exist, so login takes the same time either way.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-" + secrets.token_hex(8))


# ---------------------------------------------------------------- sessions
def new_session_token() -> tuple[str, str]:
    """Returns (cookie_value, token_hash)."""
    token = secrets.token_urlsafe(32)
    return token, sha256_hex(token)


def prompt_digest(prompt: str, system: str | None, salt: str) -> str:
    """Salted hash stored in score_events, so prompts can't be recovered by hashing guesses."""
    msg = prompt + "\x00" + (system or "")
    return hmac.new(salt.encode(), msg.encode(), hashlib.sha256).hexdigest()


def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)


# ---------------------------------------------------------------- provider keys (encrypted at rest)
class KeyVault:
    """Encrypts users' LLM provider keys with Fernet (AES-128-CBC + HMAC-SHA256).

    The Fernet key is derived from PROVIDER_KEY_SECRET, so any long random string works. Losing or
    changing that secret makes saved provider keys unreadable (users must re-add them).
    """

    def __init__(self, secret: str):
        import base64 as _b64

        from cryptography.fernet import Fernet

        derived = hashlib.sha256(("promptlint-provider-keys:" + secret).encode()).digest()
        self._f = Fernet(_b64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


def key_hint(key: str) -> str:
    """What we show for a saved provider key: never more than the last 4 characters."""
    key = key.strip()
    return "…" + key[-4:] if len(key) >= 8 else "…"
