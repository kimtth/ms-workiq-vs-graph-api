"""Display language without any Qt dependency. English text in the code is the key; the catalog is desktop_ja."""

from __future__ import annotations

from typing import Any

from desktop_ja import JA

LANGUAGE = "en"


class Tr(str):
    """A value that is itself English text from the catalog. It is translated when it is filled into a template."""


def set_language(code: str) -> None:
    global LANGUAGE
    if code not in ("en", "ja"):
        raise ValueError("Supported languages are en and ja.")
    LANGUAGE = code


def t(text: str, **values: Any) -> str:
    """Translate an English template and fill in its {name} placeholders.

    Text with no catalog entry is shown in English. Placeholders are filled only when values are given,
    so plain text that happens to contain braces is left alone.
    """
    template = JA.get(text, text) if LANGUAGE == "ja" else text
    if not values:
        return template
    return template.format(**{key: t(value) if isinstance(value, Tr) else value for key, value in values.items()})
