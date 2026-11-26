"""Passwords kept outside the repository: ~/.config/precal/app_secrets.json, e.g. {"ups_admin": "...",
"laser_force_connect": "..."}. Override the location with $PRECAL_SECRETS."""
import json
import os

PATH = os.environ.get("PRECAL_SECRETS", os.path.expanduser("~/.config/precal/app_secrets.json"))


def get(key):
    """The secret stored under key, or None when the file or the key is missing."""
    try:
        with open(PATH, encoding="utf-8") as f:
            value = json.load(f).get(key)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def matches(key, candidate):
    """True only when a secret is configured for key and candidate equals it."""
    secret = get(key)
    return secret is not None and candidate is not None and candidate == secret
