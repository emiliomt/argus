"""
env.py — Normalise credentials loaded from the process environment.
"""

import os


def get_openai_api_key() -> str:
    """
    Return a cleaned OpenAI API key, or an empty string if unset.

    Strips whitespace and surrounding quotes — a common mistake when pasting
    keys into Railway Variables or .env files.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()
    return key


def openai_api_key_configured() -> bool:
    """True when OPENAI_API_KEY is present after normalisation."""
    return bool(get_openai_api_key())
