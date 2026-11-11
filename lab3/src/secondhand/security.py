"""Salted password hashing and opaque session credentials."""

import hashlib
import hmac
import secrets

ITERATIONS = 240_000


def digest(value):
    """Hash high-entropy bearer credentials before persistence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(value):
    """Use PBKDF2 with a fresh random salt for every account."""
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${derived.hex()}"


def verify_password(value, encoded):
    """Compare in constant time; malformed persisted hashes fail closed."""
    try:
        algorithm, count, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(count) <= 1_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), int(count))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token():
    """Generate an unguessable invitation or session token."""
    return secrets.token_urlsafe(32)
