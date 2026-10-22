"""Qt widgets that keep their English text and translate it for display. The language itself is in desktop_text."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox as _QCheckBox
from PySide6.QtWidgets import QLabel as _QLabel
from PySide6.QtWidgets import QMessageBox, QWidget
from PySide6.QtWidgets import QPushButton as _QPushButton

from desktop_text import Tr, set_language, t

__all__ = ["QCheckBox", "QLabel", "QPushButton", "Tr", "retranslate", "set_language", "show", "t"]


class Translated:
    """Mixin for widgets that keep their English source text and re-translate when the language changes."""

    _source: str | tuple[str, dict[str, Any]] = ""
    _tip: str = ""

    def _render(self) -> str:
        source = self._source
        return t(source[0], **source[1]) if isinstance(source, tuple) else t(source)

    def setText(self, text: str) -> None:
        self._source = text
        super().setText(self._render())  # type: ignore[misc]

    def set_message(self, text: str, **values: Any) -> None:
        """Show an English template with values. It is translated again if the language changes."""
        self._source = (text, values)
        super().setText(self._render())  # type: ignore[misc]

    def setToolTip(self, text: str) -> None:
        self._tip = text
        super().setToolTip(t(text))  # type: ignore[misc]

    def retranslate(self) -> None:
        super().setText(self._render())  # type: ignore[misc]
        if self._tip:
            super().setToolTip(t(self._tip))  # type: ignore[misc]


class QLabel(Translated, _QLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setText(text)


class QPushButton(Translated, _QPushButton):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setText(text)


class QCheckBox(Translated, _QCheckBox):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setText(text)


def retranslate(root: QWidget) -> None:
    for widget in root.findChildren(QWidget):
        if isinstance(widget, Translated):
            widget.retranslate()


def show(kind: str, parent: QWidget | None, title: str, text: str, **values: Any) -> Any:
    """A message box with translated title and text. kind is critical, warning, or question."""
    return getattr(QMessageBox, kind)(parent, t(title), t(text, **values))
