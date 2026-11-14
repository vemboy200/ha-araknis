"""Tests for the translation files."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components import araknis


def test_english_has_no_key_references() -> None:
    """Test en.json is plain text.

    Home Assistant only fills in [%key:...%] references for core integrations,
    so a custom integration shows them as written.
    """
    text = (Path(araknis.__file__).parent / "translations" / "en.json").read_text()
    assert "[%key:" not in text
    json.loads(text)
