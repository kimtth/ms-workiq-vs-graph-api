"""PySide6 debugger GUI. Run with: uv run python desktop.py"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from string import Template
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import (
    QLabel as QtLabel,
)

from desktop_i18n import (
    QCheckBox,
    QLabel,
    QPushButton,
    Tr,
    retranslate,
    set_language,
    show,
    t,
)
from desktop_identity import GUID, account_profiles, discover_accounts, load_profiles
from desktop_model import (
    SCENARIOS,
    Inputs,
    RunState,
    operations_for,
)
from desktop_reference import build_flow, workiq_sources
from desktop_services import LiveServices, forget_saved_signin

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / ".desktop-settings.json"
ACCOUNTS = ROOT / ".desktop-accounts.json"
COLORS = {
    "background": "#f5f5f5", "surface": "#ffffff", "text": "#242424",
    "muted": "#616161", "border": "#dedede", "blue": "#0f6cbd",
    "header": "#115ea3", "selection": "#e8f1fa", "graph": "#0078d4",
    "workiq": "#8764b8", "model": "#107c10", "failed": "#c42b1c",
    "pending": "#707070",
}
STYLE = Template("""
QWidget { font-family: "Segoe UI"; font-size: 10pt; color: $text; }
QMainWindow, QWidget#page { background: $background; }
QScrollArea { border: none; background: $background; }
QFrame#card { background: $surface; border: 1px solid $border; }
QFrame#card QLabel, QFrame#card QCheckBox { background: transparent; border: none; }
QFrame#header { background: $header; }
QFrame#header QLabel { color: $surface; background: transparent; }
QLabel#titlebar { background: $surface; font-weight: 600; border-bottom: 1px solid $border; }
QLabel[muted="true"] { color: $muted; }
QLabel[heading="true"] { font-weight: 600; font-size: 11pt; }
QFrame#line { background: $border; max-height: 1px; min-height: 1px; border: none; }
QPushButton {
    background: $surface; border: 1px solid $border; border-radius: 2px; padding: 6px 12px;
}
QPushButton:hover { background: $selection; }
QPushButton:disabled { color: #a0a0a0; background: $background; }
QPushButton#primary, QPushButton#execute { background: $blue; color: $surface; border: 1px solid $blue; padding: 7px 18px; }
QPushButton#primary:hover, QPushButton#execute:hover { background: $header; }
QPushButton#primary:disabled { background: $border; border-color: $border; color: $surface; }
QPushButton[scenario="true"] { border: none; background: transparent; padding: 9px 14px; }
QPushButton[scenario="true"]:hover { background: $selection; }
QPushButton[scenario="true"]:checked { background: $selection; color: $blue; }
QPushButton[segment="true"] { padding: 6px 18px; color: $muted; }
QPushButton[segment="true"]:checked { background: $selection; color: $blue; border-color: $blue; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid $muted; background: $surface; border-radius: 2px; }
QCheckBox::indicator:hover { border-color: $blue; }
QCheckBox::indicator:checked { background: $blue; border-color: $blue; }
QCheckBox::indicator:disabled { border-color: $border; background: $background; }
QLineEdit, QComboBox, QPlainTextEdit {
    background: $surface; border: 1px solid $border; padding: 4px; selection-background-color: $blue;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid $blue; }
QTabWidget QPlainTextEdit { border: none; padding: 6px; }
QTabWidget::pane { border: 1px solid $border; background: $surface; }
QTabBar::tab { padding: 6px 18px; background: $background; border: 1px solid $border; border-bottom: none; }
QTabBar::tab:selected { background: $surface; color: $blue; }
QStatusBar { background: $surface; color: $muted; }
QStatusBar QLabel { color: $muted; padding: 3px 8px; }
QSplitter::handle { background: $border; }
""").substitute(COLORS)


def exception_text(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        return "\n".join(exception_text(item) if isinstance(item, Exception) else str(item) for item in error.exceptions)
    return str(error) or type(error).__name__


def read_settings() -> dict[str, str]:
    result = {
        "client_id": os.environ.get("ENTRA_CLIENT_ID", ""),
        "tenant_id": os.environ.get("ENTRA_TENANT_ID", ""),
        "account": os.environ.get("M365_ACCOUNT", ""),
        "copilot_model": os.environ.get("COPILOT_MODEL", ""),
        "copilot_data_consent": "false",
        "remember_signin": "true",
        "japanese": "true",
    }
    if SETTINGS.exists():
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or any(not isinstance(v, str) for v in saved.values()):
            raise ValueError("Desktop connection settings must be a JSON object with string values.")
        result.update({key: saved[key] for key in result if key in saved})
    return result


class FlowView(QWidget):
    """Debugger-style timeline: application messages, curved branches, operations, and arguments."""

    selected = Signal(int)
    moved = Signal(int)
    link_clicked = Signal(QUrl)
    TEXT_X = 62
    PAD = 12
    GAP = 4
    MAX_ARG_CHARS = 700
    WRAP = int(Qt.TextFlag.TextWordWrap) | int(Qt.TextFlag.TextWrapAnywhere)

    def __init__(self) -> None:
        super().__init__()
        self.fonts = {
            "ui": QFont("Segoe UI", 9), "small": QFont("Segoe UI", 8),
            "mono": QFont("Consolas", 9), "bold": QFont("Segoe UI", 9, QFont.Weight.Bold),
            "monobold": QFont("Consolas", 9, QFont.Weight.Bold),
        }
        self.rows: list[dict[str, Any]] = []
        self.doc_links: dict[int, list[QtLabel]] = {}
        self.selection = -1
        self.cache: tuple[int, list[tuple[int, int]], list[list[tuple[str, str, str, int]]]] | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(360)

    def text_height(self, font: str, text: str, width: int) -> int:
        return QFontMetrics(self.fonts[font]).boundingRect(QRect(0, 0, max(width, 60), 100000), self.WRAP, text).height()

    def blocks(self, entry: dict[str, Any]) -> list[tuple[str, str, str]]:
        """Text blocks of a row as (font, color, text). Nothing is shortened except very long arguments."""
        if entry.get("section"):
            accent = COLORS["workiq"] if entry["service"] == "WorkIQ" else COLORS["graph"]
            return [("bold", accent, entry["title"]), ("small", COLORS["muted"], entry["detail"])] if entry["detail"] else [("bold", accent, entry["title"])]
        color = COLORS["failed"] if entry["state"] == "failed" else {
            "Graph": COLORS["graph"], "WorkIQ": COLORS["workiq"], "GitHub Copilot": COLORS["model"],
        }.get(entry.get("service", ""), COLORS["pending"])
        if entry.get("equivalent"):
            color = COLORS["pending"]
        status = t("reference  |  not executed") if entry.get("equivalent") else t(entry["state"]) + (f"  |  {entry['ms']} ms" if "ms" in entry else "")
        scope = entry.get("scope") or entry.get("service", "")
        message = entry.get("message") or t({
            "planned": "Continue after the previous step.",
            "running": "Waiting for the service...",
            "done": "Response returned to the app.",
            "failed": "Call failed. Check Output > Logs.",
            "reference": "Not executed. Shown for comparison.",
        }[entry["state"]])
        args = json.dumps(entry.get("args", {}), ensure_ascii=False)
        if len(args) > self.MAX_ARG_CHARS:
            args = args[:self.MAX_ARG_CHARS] + t("... (+{more} more characters)", more=len(args) - self.MAX_ARG_CHARS)
        return [
            ("monobold", COLORS["text"], entry.get("label", "")),
            ("small", color, f"{status}  |  {scope}"),
            ("ui", COLORS["muted"], message),
            ("mono", COLORS["muted"], args),
        ] + [("small", COLORS["blue"], t("Official API docs")) for _url in entry.get("docs", [])]

    def measure(self) -> tuple[list[tuple[int, int]], list[list[tuple[str, str, str, int]]]]:
        """Top and height of every row, and each text block with its measured height."""
        if self.cache is not None and self.cache[0] == self.width():
            return self.cache[1], self.cache[2]
        spans, measured, y = [], [], 8
        for entry in self.rows:
            left = 14 if entry.get("section") else self.TEXT_X
            width = self.width() - left - self.PAD
            row = [(font, color, text, self.text_height(font, text, width)) for font, color, text in self.blocks(entry)]
            height = 16 + sum(b[3] for b in row) + self.GAP * (len(row) - 1)
            height = max(height, 36 if entry.get("section") else 64)
            spans.append((y, height))
            measured.append(row)
            y += height
        self.cache = (self.width(), spans, measured)
        return spans, measured

    def set_rows(self, rows: list[dict[str, Any]], selection: int) -> None:
        if rows is self.rows:
            self.selection = selection
            self.update()
            return
        for links in self.doc_links.values():
            for link in links:
                link.hide()
                link.deleteLater()
        self.doc_links.clear()
        self.rows = rows
        self.selection = selection
        self.cache = None
        for index, entry in enumerate(rows):
            for url in entry.get("docs", []):
                link = QtLabel(self)
                link.setText(f'<a href="{escape(url, quote=True)}">{t("Official API docs")}</a>')
                link.setFont(self.fonts["small"])
                link.setToolTip(url)
                link.setOpenExternalLinks(False)
                link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
                link.linkActivated.connect(lambda target: self.link_clicked.emit(QUrl(target)))
                self.doc_links.setdefault(index, []).append(link)
                link.show()
        self.fit_height()
        self.update()

    def fit_height(self) -> None:
        spans, measured = self.measure()
        wanted = (spans[-1][0] + spans[-1][1] if spans else 8) + 12
        if wanted != self.minimumHeight():
            self.setMinimumHeight(wanted)
        for index, links in self.doc_links.items():
            row = measured[index]
            top = spans[index][0] + 8 + sum(block[3] + self.GAP for block in row[:-len(links)])
            for link, block in zip(links, row[-len(links):], strict=True):
                link.setGeometry(self.TEXT_X, top, max(60, self.width() - self.TEXT_X - self.PAD), block[3])
                top += block[3] + self.GAP

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.cache = None
        self.fit_height()

    def row_center(self, index: int) -> int:
        top, height = self.measure()[0][index]
        return top + height // 2

    def row_at(self, y: float) -> int:
        for index, (top, height) in enumerate(self.measure()[0]):
            if top <= y < top + height and not self.rows[index].get("section"):
                return index
        return -1

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        self.selected.emit(self.row_at(event.position().y()))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.moved.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(COLORS["surface"]))
        width = self.width()
        spine, branch = 20, 42
        spans, measured = self.measure()
        for i, (entry, (y, h), row) in enumerate(zip(self.rows, spans, measured, strict=True)):
            left = 14 if entry.get("section") else self.TEXT_X
            if entry.get("section"):
                accent = COLORS["workiq"] if entry["service"] == "WorkIQ" else COLORS["graph"]
                p.fillRect(QRectF(0, y, width, h), QColor("#f3eefa" if entry["service"] == "WorkIQ" else "#eef5fc"))
                p.fillRect(QRectF(0, y, 4, h), QColor(accent))
            else:
                if entry.get("equivalent"):
                    p.fillRect(QRectF(0, y, width, h), QColor("#fafafa"))
                if i == self.selection:
                    p.fillRect(QRectF(0, y, width, h), QColor(COLORS["selection"]))
                color = COLORS["pending"] if entry.get("equivalent") else (
                    COLORS["failed"] if entry["state"] == "failed" else {
                        "Graph": COLORS["graph"], "WorkIQ": COLORS["workiq"], "GitHub Copilot": COLORS["model"],
                    }.get(entry.get("service", ""), COLORS["pending"]))
                p.setPen(QPen(QColor(COLORS["border"]), 2))
                p.drawLine(spine, y, spine, y + h)
                curve = QPainterPath(QPointF(spine, y + 12))
                curve.cubicTo(branch, y + 14, branch, y + h * .35, branch, y + h / 2)
                curve.cubicTo(branch, y + h * .65, branch, y + h - 14, spine, y + h - 12)
                pen = QPen(QColor(color), 2)
                if entry["state"] == "planned" or entry.get("equivalent"):
                    pen.setStyle(Qt.PenStyle.CustomDashLine)
                    pen.setDashPattern([2, 2])
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(curve)
                p.setPen(QPen(QColor(color), 1))
                for x, cy, radius, fill in ((spine, y + 12, 4, COLORS["surface"]), (branch, y + h / 2, 5, color), (spine, y + h - 12, 3, COLORS["pending"])):
                    p.setBrush(QColor(fill))
                    p.drawEllipse(QPointF(x, cy), radius, radius)
            top = y + 8
            painted = row[:-len(entry["docs"])] if entry.get("docs") else row
            for font, color_text, text, height in painted:
                p.setFont(self.fonts[font])
                p.setPen(QColor(color_text))
                p.drawText(QRectF(left, top, width - left - self.PAD, height), self.WRAP | int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), text)
                top += height + self.GAP
        p.end()


class DesktopApp(QMainWindow):
    def __init__(self, settings: dict[str, str] | None = None) -> None:
        super().__init__()
        self.settings = settings if settings is not None else read_settings()
        set_language("ja" if self.settings.get("japanese", "true") == "true" else "en")
        self.setWindowTitle(t("WorkIQ Graph Debugger"))
        self.resize(1420, 980)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(STYLE)
        self.profiles = load_profiles(ACCOUNTS) if settings is None else []
        self.updating_identity = False
        self.scenario = "briefing"
        self.states: dict[tuple[str, str], RunState] = {}
        self.values_by_scenario: dict[str, Inputs] = {}
        self.events: queue.Queue[tuple[tuple[str, str], dict[str, Any]]] = queue.Queue()
        self.busy = False
        self.signed_in: set[str] = set()
        self.selected_step = -1
        self.active_rows: list[dict[str, Any]] = []
        self.service: LiveServices | None = None
        self.worker: threading.Thread | None = None
        self.started = 0.0
        self.active_key: tuple[str, str] | None = None
        self.controls: list[QWidget] = []
        self.dialog: QDialog | None = None
        self.confirm: QDialog | None = None

        self.status_label = QLabel("Ready. Select a scenario, choose Graph or WorkIQ, then run.")
        self.statusBar().addWidget(self.status_label, 1)
        # The language switch sits in the status bar so it is reachable on both screens.
        self.japanese = QCheckBox("Japanese UI and answers (日本語)")
        self.japanese.setChecked(self.settings.get("japanese", "true") == "true")
        self.japanese.setToolTip("Show every message in Japanese and ask for the whole answer in Japanese. Names and source links stay as they are.")
        self.controls.append(self.japanese)
        self.statusBar().addPermanentWidget(self.japanese)
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self._build_signin()
        self._build_workspace()
        self.pages.addWidget(self.signin_page)
        self.pages.addWidget(self.workspace)
        self.pages.setCurrentWidget(self.signin_page)
        self.populate_accounts()
        self.refresh()
        self.japanese.toggled.connect(self.language_changed)

        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.shortcuts = [QShortcut(QKeySequence("F5"), self, activated=self.run)]
        for index, scenario in enumerate(SCENARIOS, 1):
            self.shortcuts.append(QShortcut(QKeySequence(f"Ctrl+{index}"), self, activated=lambda name=scenario: self.choose_scenario(name)))
        if settings is None:
            QTimer.singleShot(0, self.start_discovery)
            QTimer.singleShot(0, self.restore_signin)

    @property
    def mode(self) -> str:
        return "graph" if self.mode_buttons["graph"].isChecked() else "workiq"

    def set_mode(self, value: str) -> None:
        self.mode_buttons[value].setChecked(True)

    @property
    def key(self) -> tuple[str, str]:
        return self.scenario, self.mode

    @property
    def state(self) -> RunState:
        return self.states.setdefault(self.key, RunState())

    @property
    def signin_visible(self) -> bool:
        return self.pages.currentWidget() is self.signin_page

    @property
    def status_text(self) -> str:
        return self.status_label.text()

    def set_status(self, text: str, **values: Any) -> None:
        """Show an English template. It is translated for display and again if the language changes."""
        self.status_label.set_message(text, **values)

    def set_signin(self, target: str, text: str, **values: Any) -> None:
        self.signin_labels[target].set_message(text, **values)

    def _button(self, text: str, command: Callable[[], Any], name: str = "", control: bool = True) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(lambda _checked=False: command())
        if name:
            button.setObjectName(name)
        if control:
            self.controls.append(button)
        return button

    @staticmethod
    def _label(text: str, muted: bool = False, heading: bool = False, center: bool = False, wrap: bool = True) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(wrap)
        label.setProperty("muted", muted)
        label.setProperty("heading", heading)
        if center:
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    @staticmethod
    def _line() -> QFrame:
        line = QFrame()
        line.setObjectName("line")
        return line

    @staticmethod
    def _row(left: int = 12, top: int = 8, right: int = 12, bottom: int = 8) -> tuple[QWidget, QHBoxLayout]:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(left, top, right, bottom)
        return widget, layout

    @staticmethod
    def _output(wrap: bool = True) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setFont(QFont("Consolas", 9))
        edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap)
        return edit

    def _answer_view(self) -> QTextBrowser:
        view = QTextBrowser()
        view.setOpenLinks(False)
        view.setFont(QFont("Segoe UI", 10))
        view.anchorClicked.connect(self.open_link)
        return view

    @staticmethod
    def open_link(url: QUrl) -> None:
        # Answers come from a model. Open only web links, never local files or other schemes.
        if url.scheme() in ("http", "https"):
            webbrowser.open(url.toString())

    def _build_workspace(self) -> None:
        self.workspace = QWidget()
        self.workspace.setObjectName("page")
        root = QVBoxLayout(self.workspace)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(38)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 16, 0)
        header_layout.addWidget(QLabel("Debugger  |  WorkIQ and Microsoft Graph"))
        header_layout.addStretch()
        self.account_label = QLabel("Not connected")
        header_layout.addWidget(self.account_label)
        root.addWidget(header)

        shortcuts, shortcut_layout = self._row(8, 4, 8, 4)
        self.scenario_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for i, (name, (title, _, _)) in enumerate(SCENARIOS.items(), 1):
            button = self._button("", lambda value=name: self.choose_scenario(value))
            button.set_message("{number}  {title}", number=i, title=Tr(title))
            button.setCheckable(True)
            button.setProperty("scenario", True)
            group.addButton(button)
            shortcut_layout.addWidget(button)
            self.scenario_buttons[name] = button
        shortcut_layout.addStretch()
        shortcut_layout.addWidget(self._button("Sign-in screen", self.show_signin))
        shortcut_layout.addWidget(self._button("Connection...", self.connection_dialog))
        root.addWidget(shortcuts)
        root.addWidget(self._line())

        command, command_layout = self._row()
        command_layout.addWidget(QLabel("Execution path"))
        self.mode_buttons: dict[str, QPushButton] = {}
        modes = QButtonGroup(self)
        for text, value in (("Graph", "graph"), ("WorkIQ", "workiq")):
            button = self._button(text, self.switch_mode)
            button.setCheckable(True)
            button.setProperty("segment", True)
            modes.addButton(button)
            command_layout.addWidget(button)
            self.mode_buttons[value] = button
        self.mode_buttons["workiq"].setChecked(True)
        self.run_button = self._button("Run  F5", self.run, "primary")
        self.write_button = self._button("Review writes...", self.write_dialog, control=False)
        command_layout.addSpacing(12)
        command_layout.addWidget(self.run_button)
        command_layout.addWidget(self.write_button)
        command_layout.addWidget(self._button("Clear output", self.clear))
        command_layout.addStretch()
        self.metrics_label = self._label("", muted=True, wrap=False)
        command_layout.addWidget(self.metrics_label)
        root.addWidget(command)

        inputs, input_layout = self._row(12, 0, 12, 10)
        self.topic = QLineEdit("Contoso")
        self.start = QLineEdit(date.today().isoformat())
        self.end = QLineEdit((date.today() + timedelta(days=7)).isoformat())
        input_layout.addWidget(QLabel("Customer or topic"))
        input_layout.addWidget(self.topic, 1)
        input_layout.addWidget(QLabel("Start"))
        input_layout.addWidget(self.start)
        input_layout.addWidget(QLabel("End (exclusive)"))
        input_layout.addWidget(self.end)
        input_layout.addWidget(self._button("Apply", self.apply_inputs))
        for edit in (self.topic, self.start, self.end):
            self.controls.append(edit)
            edit.textChanged.connect(self.inputs_changed)
        root.addWidget(inputs)
        root.addWidget(self._line())

        side_by_side = QSplitter(Qt.Orientation.Horizontal)
        debugger = QWidget()
        debugger_layout = QVBoxLayout(debugger)
        debugger_layout.setContentsMargins(0, 0, 0, 0)
        debugger_layout.setSpacing(0)
        flow_header, flow_header_layout = self._row(14, 7, 14, 4)
        flow_title = QLabel("Execution flow")
        flow_title.setProperty("heading", True)
        flow_header_layout.addWidget(flow_title)
        flow_header_layout.addStretch()
        self.show_workiq_writes = QCheckBox("Show planned WorkIQ writes")
        self.show_workiq_writes.setToolTip("Show write calls that wait for confirmation. Calls already attempted always stay visible.")
        self.show_workiq_writes.toggled.connect(self.write_visibility_changed)
        flow_header_layout.addWidget(self.show_workiq_writes)
        debugger_layout.addWidget(flow_header)
        self.plan_label = self._label("", muted=True)
        self.plan_label.setContentsMargins(14, 0, 14, 8)
        debugger_layout.addWidget(self.plan_label)
        self.flow = FlowView()
        self.flow.selected.connect(self.select_call)
        self.flow.moved.connect(self.move_selection)
        self.flow.link_clicked.connect(self.open_link)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.flow)
        debugger_layout.addWidget(self.scroll, 1)

        output = QWidget()
        output.setMinimumWidth(340)
        output_layout = QVBoxLayout(output)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(0)
        output_header, output_header_layout = self._row(12, 5, 12, 5)
        output_title = QLabel("Output")
        output_title.setProperty("heading", True)
        output_header_layout.addWidget(output_title)
        output_header_layout.addStretch()
        output_header_layout.addWidget(self._button("Copy current output", self.copy_output, control=False))
        output_layout.addWidget(output_header)
        self.tabs = QTabWidget()
        self.outputs: dict[str, QPlainTextEdit | QTextBrowser] = {}
        for name in ("Answer", "Logs", "Evidence"):
            self.outputs[name] = self._answer_view() if name == "Answer" else self._output(wrap=name != "Logs")
            self.tabs.addTab(self.outputs[name], t(name))
        output_layout.addWidget(self.tabs, 1)
        # Output is on the left: it holds the result. The flow on the right explains how it was produced.
        side_by_side.addWidget(output)
        side_by_side.addWidget(debugger)
        side_by_side.setSizes([640, 780])
        side_by_side.setStretchFactor(0, 1)
        side_by_side.setStretchFactor(1, 1)
        side_by_side.setChildrenCollapsible(False)
        root.addWidget(side_by_side, 1)

    def _build_signin(self) -> None:
        self.signin_page = QScrollArea()
        self.signin_page.setWidgetResizable(True)
        page = QWidget()
        page.setObjectName("page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        titlebar = QLabel("WorkIQ Graph Debugger")
        titlebar.setObjectName("titlebar")
        titlebar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        titlebar.setFixedHeight(44)
        outer.addWidget(titlebar)
        outer.addStretch(1)
        center = QHBoxLayout()
        center.addStretch()
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(570)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(6)
        center.addWidget(card)
        center.addStretch()
        outer.addLayout(center)
        outer.addStretch(1)

        title = self._label("Sign in", heading=True, center=True)
        title.setStyleSheet("font-size: 19pt; font-weight: 600;")
        layout.addWidget(title)
        layout.addWidget(self._label("Choose an available account or use Microsoft's account picker.\n"
                                     "The app signs in only to WorkIQ. It requests no Microsoft Graph permissions.", muted=True, center=True))
        layout.addSpacing(8)
        layout.addWidget(self._label("Microsoft 365 account (optional)", muted=True))
        self.account_picker = QComboBox()
        self.account_picker.setEditable(True)
        self.account_picker.activated.connect(self.select_available_account)
        layout.addWidget(self.account_picker)
        layout.addWidget(self._label("Directory (tenant) ID - leave blank to discover", muted=True))
        self.tenant_picker = QComboBox()
        self.tenant_picker.setEditable(True)
        layout.addWidget(self.tenant_picker)
        layout.addSpacing(6)
        self.controls.extend([self.account_picker, self.tenant_picker])
        self.login_button = self._button("Sign in with Microsoft 365", self.sign_in, "primary")
        layout.addWidget(self.login_button)
        self.another_account_button = self._button("Use another account...", lambda: self.sign_in(force=True))
        layout.addWidget(self.another_account_button)
        self.signin_labels = {
            "workiq": self._label("Not connected. Requires WorkIQ tenant enablement and consent.", muted=True),
            "graph": self._label("Not used. No Graph permission is requested.", muted=True),
            "copilot": self._label("Not connected. Uses your GitHub Copilot subscription.", muted=True),
        }
        for target, text in (("workiq", "WorkIQ"), ("graph", "Microsoft Graph")):
            heading = QLabel(text)
            heading.setStyleSheet("font-weight: 600;")
            layout.addWidget(heading)
            layout.addWidget(self.signin_labels[target])
        layout.addWidget(self._button("Connect GitHub Copilot", self.sign_in_copilot))
        layout.addWidget(self.signin_labels["copilot"])
        self.consent = QCheckBox("Allow sending M365 text to GitHub Copilot")
        self.consent.setToolTip("Retrieved Microsoft 365 text is sent to GitHub Copilot to write the Graph summary.")
        self.consent.setChecked(self.settings.get("copilot_data_consent") == "true")
        self.consent.toggled.connect(self.save_consent)
        self.controls.append(self.consent)
        layout.addWidget(self.consent)
        self.remember = QCheckBox("Remember sign-in on this PC (encrypted)")
        self.remember.setToolTip("Keeps the sign-in in a Windows-encrypted file so the account popup is not needed next time.")
        self.remember.setChecked(self.settings.get("remember_signin") == "true")
        self.remember.toggled.connect(self.save_remember)
        self.controls.append(self.remember)
        layout.addWidget(self.remember)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setObjectName("primary")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(lambda _checked=False: self.continue_to_debugger())
        layout.addSpacing(6)
        layout.addWidget(self.continue_button)
        layout.addWidget(self._label("Sign-in opens your browser. No password is entered here.\n"
                                     "Graph is not used: its calls appear as reference rows in WorkIQ mode.", muted=True, center=True))
        links = QHBoxLayout()
        links.addWidget(self._button("Connection...", self.connection_dialog), 1)
        links.addStretch()
        self.clear_button = self._button("Clear sign-in", lambda: self.clear_signin(forget=True))
        links.addWidget(self.clear_button, 1)
        layout.addLayout(links)
        self.signin_page.setWidget(page)
        self.account_picker.setEditText(self.settings.get("account", ""))
        self.tenant_picker.setEditText(self.settings.get("tenant_id", ""))
        self.login_fingerprint = self.identity_fingerprint()
        self.account_picker.editTextChanged.connect(self.login_edited)
        self.tenant_picker.editTextChanged.connect(self.login_edited)

    def identity_fingerprint(self) -> tuple[str, str]:
        return self.tenant_picker.currentText().strip(), self.account_picker.currentText().strip()

    def set_identity(self, account: str, tenant: str) -> None:
        self.updating_identity = True
        try:
            self.account_picker.setEditText(account)
            self.tenant_picker.setEditText(tenant)
        finally:
            self.updating_identity = False
        self.login_fingerprint = self.identity_fingerprint()

    def populate_accounts(self) -> None:
        account, tenant = self.account_picker.currentText(), self.tenant_picker.currentText()
        accounts = list(dict.fromkeys(profile["account"] for profile in self.profiles))
        tenants = list(dict.fromkeys(profile["tenant_id"] for profile in self.profiles))
        if not account:
            matching = [p for p in self.profiles if not tenant or p["tenant_id"] == tenant]
            account = matching[0]["account"] if matching else account
        if not tenant:
            matching = [p for p in self.profiles if p["account"].casefold() == account.casefold()]
            tenant = matching[0]["tenant_id"] if matching else tenant
        self.updating_identity = True
        try:
            for combo, values, text in ((self.account_picker, accounts, account), (self.tenant_picker, tenants, tenant)):
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(values)
                combo.setEditText(text)
                combo.blockSignals(False)
        finally:
            self.updating_identity = False
        self.login_fingerprint = self.identity_fingerprint()

    def select_available_account(self, _index: int = 0) -> None:
        matching = [p for p in self.profiles if p["account"].casefold() == self.account_picker.currentText().casefold()]
        if matching:
            self.tenant_picker.setEditText(matching[0]["tenant_id"])
            self.set_status("Available account from {source}. Microsoft sign-in is still required.", source=matching[0]["source"])

    def start_discovery(self) -> None:
        key = self.key

        def discover() -> None:
            try:
                self.events.put((key, {"type": "profiles", "data": discover_accounts()}))
            except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
                self.events.put((key, {"type": "notice", "message": t("Account discovery failed: {error}. You can still use Microsoft's account picker.", error=error)}))
        threading.Thread(target=discover, name="AccountDiscovery", daemon=True).start()

    def save_consent(self, *_args: Any) -> None:
        self.settings["copilot_data_consent"] = "true" if self.consent.isChecked() else "false"
        if self.service is not None:
            self.service.settings.update(self.settings)
        try:
            SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError as error:
            self.set_status("Cannot save Copilot data-sharing permission: {error}", error=error)

    def sign_in_copilot(self) -> None:
        if self.busy:
            return
        self.set_signin("copilot", "Checking GitHub Copilot sign-in...")
        self.start_worker(lambda service: service.connect_copilot(), auth_target="copilot")

    def login_edited(self, *_args: Any) -> None:
        if self.updating_identity:
            return
        fingerprint = self.identity_fingerprint()
        if fingerprint != self.login_fingerprint:
            self.clear_signin()
            self.login_fingerprint = fingerprint

    def clear_signin(self, forget: bool = False) -> None:
        """Reset the screen. `forget` also deletes the sign-in saved on this PC."""
        if self.busy:
            return
        self.signed_in.clear()
        self.service = None
        self.states.clear()
        self.account_label.setText("Not connected")
        self.set_signin("workiq", "Not connected. Sign in to continue.")
        self.set_signin("copilot", "Not connected. Sign in to continue.")
        self.set_signin("graph", "Not used. No Graph permission is requested.")
        self.continue_button.setEnabled(False)
        removed = forget_saved_signin() if forget else False
        self.set_status(
            "Saved sign-in deleted from this PC. Your Microsoft browser session is unchanged." if removed
            else "Local sign-in cleared. Your Microsoft browser session is unchanged."
        )
        self.refresh()

    def save_remember(self, *_args: Any) -> None:
        self.settings["remember_signin"] = "true" if self.remember.isChecked() else "false"
        self.service = None
        if not self.remember.isChecked():
            forget_saved_signin()
            self.set_status("Sign-in will not be kept on this PC. The saved copy was deleted.")
        try:
            SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError as error:
            self.set_status("Cannot save the sign-in preference: {error}", error=error)

    def show_signin(self) -> None:
        if self.busy:
            return
        self.pages.setCurrentWidget(self.signin_page)
        self.continue_button.setEnabled(bool(self.signed_in))

    def continue_to_debugger(self) -> None:
        if self.busy or not self.signed_in:
            return
        if self.mode not in self.signed_in:
            self.set_mode("graph" if "graph" in self.signed_in else "workiq")
        self.pages.setCurrentWidget(self.workspace)
        self.set_status("Ready. Select a scenario and run. Writes need a separate review.")
        self.refresh()

    def sign_in(self, force: bool = False) -> None:
        if self.busy:
            return
        settings = {
            **self.settings, "tenant_id": self.tenant_picker.currentText().strip(),
            "account": self.account_picker.currentText().strip(),
            "copilot_data_consent": "true" if self.consent.isChecked() else "false",
            "remember_signin": "true" if self.remember.isChecked() else "false",
        }
        if settings["tenant_id"] and not GUID.fullmatch(settings["tenant_id"]):
            self.set_signin("workiq", "Enter a valid tenant ID, or leave it blank to discover it.")
            self.set_status("The tenant ID must be a GUID.")
            return
        try:
            SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except OSError as error:
            self.set_signin("workiq", "Cannot save connection settings: {error}", error=error)
            return
        if settings != self.settings:
            self.service = None
            self.signed_in.clear()
        self.settings = settings
        self.set_mode("workiq")
        self.set_signin("workiq", "Choosing another account in your browser..." if force else "Signing in. A saved sign-in is reused without a popup...")
        self.set_signin("graph", "Not used. No Graph permission is requested.")
        scenario = self.scenario
        self.start_worker(lambda service: service.sign_in(scenario, force=force), auth_target="workiq")

    def restore_signin(self) -> None:
        """At startup, reuse saved Microsoft and GitHub Copilot sign-ins quietly. This never opens a browser."""
        if self.busy:
            return
        self.set_signin("workiq", "Looking for a saved sign-in...")
        self.set_signin("copilot", "Checking GitHub Copilot sign-in...")
        scenario = self.scenario

        def restore(service: LiveServices) -> None:
            service.sign_in(scenario, restore=True)
            service.connect_copilot(restore=True)
        self.start_worker(restore, auth_target="workiq")

    def get_inputs(self) -> Inputs:
        return Inputs.from_dates(self.scenario, self.topic.text(), self.start.text(), self.end.text(), self.japanese.isChecked())

    def inputs_changed(self, *_args: Any) -> None:
        if self.busy:
            return
        self.states.clear()
        self.values_by_scenario.clear()
        self.selected_step = -1
        self.refresh()

    def apply_inputs(self) -> bool:
        try:
            values = self.get_inputs()
        except ValueError as error:
            self.set_status("Input error: {error}. Dates must use YYYY-MM-DD.", error=error)
            show("critical", self, "Check inputs", "Input error: {error}. Dates must use YYYY-MM-DD.", error=error)
            return False
        if self.values_by_scenario.get(self.scenario) != values:
            for mode in ("graph", "workiq"):
                self.states.pop((self.scenario, mode), None)
            self.values_by_scenario[self.scenario] = values
        self.refresh()
        return True

    def choose_scenario(self, scenario: str) -> None:
        if self.busy or QApplication.activeModalWidget() is not None:
            self.refresh()
            return
        self.scenario = scenario
        self.selected_step = -1
        self.set_status("{title} selected. The end date is exclusive.", title=Tr(SCENARIOS[scenario][0]))
        self.refresh()

    def switch_mode(self) -> None:
        self.selected_step = -1
        self.refresh()

    def write_visibility_changed(self) -> None:
        self.selected_step = -1
        self.refresh()

    def refresh(self) -> None:
        state = self.state
        for name, button in self.scenario_buttons.items():
            button.setChecked(name == self.scenario)
        try:
            values = self.get_inputs()
            self.active_rows = build_flow(values, self.mode, state.steps, state.result or None,
                                          show_workiq_writes=self.show_workiq_writes.isChecked())
        except ValueError:
            self.active_rows = []
        self.plan_label.set_message(
            "LIVE  |  Solid branches are calls this app made. Dotted Graph rows are a reference and are never executed." if state.steps and self.mode == "workiq"
            else "LIVE  |  Solid branches show attempted calls" if state.steps
            else "PLANNED  |  Dashed branches have not run"
        )
        self.metrics_label.set_message("{calls} M365 calls   |   {seconds:.1f} s", calls=state.calls, seconds=state.elapsed)
        self.write_button.setEnabled(not self.busy and self.scenario != "briefing" and state.ready and not state.write_attempted)
        self.outputs["Answer"].setMarkdown(state.answer or t("*No answer yet.*"))
        evidence = state.evidence
        if self.mode == "workiq" and state.result:
            evidence = {"cited_sources_inferred_from_links": workiq_sources(state.result), "workiq_result": state.result}
        self.outputs["Evidence"].setPlainText(json.dumps(evidence, indent=2, ensure_ascii=False) if evidence is not None else t("No evidence retrieved."))
        self.outputs["Logs"].setPlainText("\n".join(state.logs))
        logs = self.outputs["Logs"].verticalScrollBar()
        logs.setValue(logs.maximum())
        self.draw_flow()

    def draw_flow(self) -> None:
        self.flow.set_rows(self.active_rows, self.selected_step)

    def select_call(self, index: int) -> None:
        self.selected_step = index
        self.draw_flow()

    def move_selection(self, delta: int) -> None:
        calls = [i for i, row in enumerate(self.active_rows) if not row.get("section")]
        if not calls:
            return
        later = [i for i in calls if (i > self.selected_step if delta > 0 else i < self.selected_step)]
        self.selected_step = (later[0] if delta > 0 else later[-1]) if later else (calls[0] if self.selected_step < 0 else self.selected_step)
        self.draw_flow()
        self.scroll.ensureVisible(0, self.flow.row_center(self.selected_step), 10, 60)

    def set_busy(self, value: bool) -> None:
        self.busy = value
        for control in self.controls:
            control.setEnabled(not value)
        self.write_button.setEnabled(not value and self.scenario != "briefing" and self.state.ready and not self.state.write_attempted)
        self.continue_button.setEnabled(not value and bool(self.signed_in))

    def start_worker(self, task: Callable[[LiveServices], None], write: bool = False, auth_target: str | None = None) -> None:
        key = self.key
        self.state.error = ""
        self.active_key = key
        self.started = time.monotonic()
        self.set_busy(True)
        self.tabs.setCurrentWidget(self.outputs["Logs"])
        self.set_status("Running. Sign-in may open in your browser. WorkIQ can take several minutes.")
        if self.service is None:
            self.service = LiveServices(lambda event: self.events.put((key, event)), dict(self.settings))
        self.service.emit = lambda event: self.events.put((key, event))
        service = self.service

        def worker() -> None:
            try:
                task(service)
            except Exception as error:
                logger.exception("Microsoft 365 workflow failed")
                detail = exception_text(error)
                if write:
                    detail += t("\nEarlier successful writes are not rolled back. Check Microsoft 365 before repeating.")
                self.events.put((key, {"type": "error", "message": detail, "auth_target": auth_target}))
            finally:
                self.events.put((key, {"type": "finished", "seconds": time.monotonic() - self.started, "auth_target": auth_target}))

        self.worker = threading.Thread(target=worker, name="Microsoft365Workflow", daemon=True)
        self.worker.start()

    def run(self) -> None:
        if self.busy or self.signin_visible or QApplication.activeModalWidget() is not None or not self.apply_inputs():
            return
        mode = self.mode
        if mode == "graph":
            self.set_status("The Graph path is not available in this environment. Switch to WorkIQ to see the equivalent Graph calls.")
            return
        self.states[self.key] = RunState()
        values = self.get_inputs()
        self.refresh()
        self.start_worker(lambda service: service.read(mode, values))

    def poll(self) -> None:
        changed = False
        while True:
            try:
                key, event = self.events.get_nowait()
            except queue.Empty:
                break
            if event["type"] == "profiles":
                self.profiles = account_profiles([*self.profiles, *event["data"]])
                if not self.busy:
                    self.populate_accounts()
                self.set_status("Available accounts populated. Microsoft sign-in is still required." if self.profiles
                                else "No cached accounts found. Use Microsoft's account picker; the tenant can be blank.")
                continue
            state = self.states.setdefault(key, RunState())
            state.apply(event)
            stamp = datetime.now().strftime("%H:%M:%S")
            kind = event["type"]
            if kind == "step":
                line = f"{event['service']}  {event['label']}\n           {json.dumps(event['args'], ensure_ascii=False)}"
            elif kind == "step_done":
                line = t("OK  Call {id}  {status}  {ms} ms", id=event["id"], status=event["status"], ms=event["ms"])
            elif kind in ("error", "step_error"):
                line = t("ERROR  {message}", message=event["message"])
                if kind == "error" and self.signin_visible:
                    self.set_signin(event.get("auth_target") or key[1], "Sign-in failed: {message}", message=event["message"][:220])
            elif kind == "write":
                line = t("WRITE CONFIRMED  {path}", path=event["path"])
            elif kind == "account":
                self.account_label.setText(event["message"])
                if profile := event.get("profile"):
                    previous = (self.settings.get("account"), self.settings.get("tenant_id"))
                    if previous[0] and (
                        previous[0].casefold() != profile["account"].casefold()
                        or (previous[1] or "").casefold() != profile["tenant_id"].casefold()
                    ):
                        self.signed_in.clear()
                        self.states.clear()
                        state = self.states.setdefault(key, RunState())
                        self.set_signin("workiq", "Not connected. Sign in with the selected account.")
                    self.signed_in.add(key[1])
                    self.settings.update(account=profile["account"], tenant_id=profile["tenant_id"])
                    self.set_identity(profile["account"], profile["tenant_id"])
                    self.profiles = account_profiles([*self.profiles, profile])
                    self.populate_accounts()
                    try:
                        SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
                        ACCOUNTS.write_text(json.dumps(self.profiles, indent=2), encoding="utf-8")
                    except OSError as error:
                        state.logs.append(f"[{stamp}] " + t("Cannot remember account metadata: {error}", error=error))
                line = t("Signed in as {account}", account=event["message"])
            elif kind == "copilot_missing":
                self.set_signin("copilot", "Not connected. Uses your GitHub Copilot subscription.")
                line = event["message"]
            elif kind == "copilot_connected":
                self.set_signin("copilot", event["message"])
                line = event["message"]
            elif kind == "signed_in":
                self.signed_in.add(event["mode"])
                self.set_signin(event["mode"], "Connected. Access token acquired. Service access still depends on tenant policy.")
                line = t("{mode} sign-in confirmed.", mode=event["mode"])
            elif kind == "finished":
                state.elapsed += event["seconds"]
                self.set_busy(False)
                self.active_key = None
                self.populate_accounts()
                self.set_status("Failed. See Output > Logs." if state.error else "Complete. Review the answer and evidence.")
                if self.signin_visible:
                    if state.error:
                        self.set_status("Sign-in failed. See the connection status above.")
                    elif "workiq" in self.signed_in:
                        self.set_status("Sign-in complete. Continue to the debugger.")
                    else:
                        self.set_status("Not signed in. Choose Sign in with Microsoft 365.")
                        self.set_signin("workiq", "Not connected. Requires WorkIQ tenant enablement and consent.")
                line = t("Finished in {seconds:.1f} seconds. {calls} M365 calls in this trace.", seconds=event["seconds"], calls=state.calls)
            elif kind in ("read_complete", "write_complete"):
                line = t("Read workflow complete.") if kind == "read_complete" else t("All requested writes confirmed.")
                if kind == "read_complete" and key == self.key:
                    self.tabs.setCurrentWidget(self.outputs["Answer"])
            elif kind in ("evidence", "result"):
                line = t("Retrieved evidence is available.") if kind == "evidence" else t("Answer is available.")
                if kind == "result" and key[1] == "workiq":
                    state.evidence = event["data"]
            else:
                line = event.get("message", kind)
            state.logs.append(f"[{stamp}] {line}")
            changed = changed or key == self.key
        if changed:
            self.refresh()
        if self.busy and self.active_key == self.key:
            elapsed = self.state.elapsed + time.monotonic() - self.started
            self.metrics_label.set_message("{calls} M365 calls   |   {seconds:.1f} s", calls=self.state.calls, seconds=elapsed)

    def clear(self) -> None:
        if self.busy:
            return
        self.states[self.key] = RunState()
        self.selected_step = -1
        self.set_status("Output cleared. The flow now shows the plan, not an executed run.")
        self.refresh()

    def copy_output(self) -> None:
        current = self.tabs.currentWidget()
        # Copy the Markdown source for Answer, not its rendered text.
        QApplication.clipboard().setText(self.state.answer if current is self.outputs["Answer"] else current.toPlainText())
        self.set_status("Output copied. It may contain workplace data. Share only within your organization's rules.")

    def _dialog(self, parent: QWidget, title: str, width: int, height: int) -> tuple[QDialog, QVBoxLayout]:
        dialog = QDialog(parent)
        dialog.setWindowTitle(t(title))
        dialog.resize(width, height)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        return dialog, layout

    def connection_dialog(self) -> QDialog | None:
        if self.busy:
            return None
        dialog, layout = self._dialog(self, "Microsoft 365 connection", 650, 480)
        heading = QLabel("Delegated Microsoft 365 sign-in")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        layout.addWidget(self._label("The app signs in as you. It requests only the WorkIQ permission.\n"
                                     "The browser completes sign-in. The sign-in can be kept on this PC, encrypted.", muted=True))
        fields: dict[str, QLineEdit] = {}
        for key, label in (
            ("client_id", "Sign-in client ID override (optional; blank uses the built-in public client)"),
            ("tenant_id", "Directory (tenant) ID (blank discovers it during sign-in)"),
            ("account", "Microsoft 365 account hint (optional)"),
            ("copilot_model", "GitHub Copilot model ID (blank uses Copilot's default)"),
        ):
            layout.addWidget(QLabel(label))
            fields[key] = QLineEdit(self.settings.get(key, ""))
            layout.addWidget(fields[key])
        layout.addStretch()

        def save() -> None:
            settings = {**self.settings, **{key: edit.text().strip() for key, edit in fields.items()}}
            try:
                SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            except (OSError, ValueError) as error:
                show("critical", dialog, "Connection settings", "{error}", error=error)
                return
            self.settings = settings
            self.service = None
            self.signed_in.clear()
            self.set_identity(settings["account"], settings["tenant_id"])
            self.account_label.setText("Not connected")
            self.states.clear()
            self.set_status("Connection saved. Run a scenario to sign in.")
            dialog.close()
            self.refresh()

        def accept_license() -> None:
            webbrowser.open("https://github.com/microsoft/work-iq")
            answer = show(
                "question", dialog, "WorkIQ license",
                "Have you read and accepted the WorkIQ license agreement?\n\nIf you confirm, the app will send your acceptance to WorkIQ now.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if any(edit.text().strip() != self.settings.get(key, "") for key, edit in fields.items()):
                show("critical", dialog, "Save connection first", "Save connection settings, then reopen this dialog to accept the license.")
                return
            dialog.close()
            if self.mode != "workiq":
                self.set_mode("workiq")
                self.switch_mode()
            scenario = self.scenario

            def accept(service: LiveServices) -> None:
                import asyncio

                token = service.token("workiq", scenario)

                async def call() -> None:
                    async with service.work_session(token) as session:
                        await service.work_call(session, "accept_eula", {"eulaUrl": "https://github.com/microsoft/work-iq"})
                asyncio.run(call())
                service.emit({"type": "notice", "message": t("License acceptance request completed. Run the scenario next.")})
            self.start_worker(accept)

        actions = QHBoxLayout()
        license_button = QPushButton("WorkIQ license...")
        license_button.clicked.connect(lambda _checked=False: accept_license())
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda _checked=False: dialog.close())
        save_button = QPushButton("Save")
        save_button.setObjectName("primary")
        save_button.clicked.connect(lambda _checked=False: save())
        actions.addWidget(license_button)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save_button)
        layout.addLayout(actions)
        self.dialog = dialog
        dialog.open()
        return dialog

    def write_dialog(self) -> QDialog | None:
        if self.busy or not self.state.ready or self.state.write_attempted or self.scenario == "briefing":
            return None
        values = self.get_inputs()
        mode = self.mode
        state = self.state
        dialog, layout = self._dialog(self, "Review real Microsoft 365 writes", 740, 660)
        heading = QLabel("Review and edit before confirmation")
        heading.setProperty("heading", True)
        layout.addWidget(heading)
        fields: dict[str, QLineEdit | QPlainTextEdit] = {}

        def field(key: str, label: str, value: str = "", multiline: bool = False) -> None:
            layout.addWidget(QLabel(label))
            widget: QLineEdit | QPlainTextEdit
            if multiline:
                widget = QPlainTextEdit(value)
                widget.setFixedHeight(150 if key == "body" else 90)
            else:
                widget = QLineEdit(value)
            layout.addWidget(widget)
            fields[key] = widget

        if self.scenario == "recap":
            field("recipient", "Recipient email")
            field("subject", "Subject", t("{customer} - meeting recap", customer=values.customer))
            field("body", "Recap text", state.answer, multiline=True)
        else:
            field("planId", "Planner plan ID")
            field("bucketId", "Planner bucket ID")
            field("titles", "Task titles, one per line (up to 10)", "\n".join(state.result.get("actions", [])[:10]), multiline=True)
            field("subject", "Personal follow-up event subject", t("{customer} follow-up", customer=values.customer))
            field("start", "Start in local time (YYYY-MM-DD HH:MM)")
            field("end", "End in local time (YYYY-MM-DD HH:MM)")
        layout.addWidget(self._label("Tasks use the selected plan and bucket. No owner or deadline is invented.\n"
                                     "The event has no attendees. Successful writes cannot be rolled back.", muted=True))
        layout.addStretch()

        def review() -> None:
            action: dict[str, Any] = {
                key: widget.toPlainText().strip() if isinstance(widget, QPlainTextEdit) else widget.text().strip()
                for key, widget in fields.items()
            }
            action["kind"] = values.scenario
            try:
                if values.scenario == "actions":
                    action["titles"] = [line.strip() for line in action["titles"].splitlines() if line.strip()]
                    for key in ("start", "end"):
                        action[key] = datetime.strptime(action[key], "%Y-%m-%d %H:%M").astimezone().isoformat()
                operations = operations_for(action, mode)
            except (ValueError, KeyError) as error:
                show("critical", dialog, "Check write inputs", "{error}", error=error)
                return
            self.confirm_dialog(dialog, mode, values, action, operations)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda _checked=False: dialog.close())
        review_button = QPushButton("Review exact payload...")
        review_button.setObjectName("review")
        review_button.clicked.connect(lambda _checked=False: review())
        buttons.addWidget(cancel)
        buttons.addWidget(review_button)
        layout.addLayout(buttons)
        dialog.fields = fields  # type: ignore[attr-defined]
        self.dialog = dialog
        dialog.open()
        return dialog

    def confirm_dialog(self, editor: QDialog, mode: str, values: Inputs, action: dict[str, Any], operations: list[dict[str, Any]]) -> QDialog:
        dialog, layout = self._dialog(editor, "Confirm immediate writes", 740, 580)
        heading = self._label("")
        if action["kind"] == "recap":
            heading.set_message("Send one real email to {recipient} via {mode} now?", recipient=action["recipient"], mode=mode)
        else:
            heading.set_message("Create {count} real Planner tasks and one personal event via {mode} now?", count=len(action["titles"]), mode=mode)
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)
        viewer = self._output()
        viewer.setPlainText(json.dumps(operations, indent=2, ensure_ascii=False))
        layout.addWidget(viewer, 1)
        layout.addWidget(self._label("No automatic retries. On a timeout, check Microsoft 365 before repeating.\n"
                                     "Using both Graph and WorkIQ to write will create duplicate mail, tasks, or events.", muted=True))

        def execute() -> None:
            if self.busy or self.state.write_attempted:
                return
            self.state.write_attempted = True
            dialog.close()
            editor.close()
            self.start_worker(lambda service: service.write(mode, values, action), write=True)

        buttons = QHBoxLayout()
        buttons.addStretch()
        back = QPushButton("Back")
        back.clicked.connect(lambda _checked=False: dialog.close())
        confirm = QPushButton("Confirm and execute")
        confirm.setObjectName("execute")
        confirm.clicked.connect(lambda _checked=False: execute())
        buttons.addWidget(back)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)
        self.confirm = dialog
        dialog.open()
        return dialog

    def language_changed(self, *_args: Any) -> None:
        """Switch every on-screen message between Japanese and English, and the language of the answers."""
        self.settings["japanese"] = "true" if self.japanese.isChecked() else "false"
        set_language("ja" if self.japanese.isChecked() else "en")
        try:
            SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError as error:
            self.set_status("Cannot save the language preference: {error}", error=error)
        self.setWindowTitle(t("WorkIQ Graph Debugger"))
        retranslate(self)
        for index, name in enumerate(self.outputs):
            self.tabs.setTabText(index, t(name))
        if self.service is not None:
            self.service.settings.update(self.settings)
        self.inputs_changed()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy:
            show("warning", self, "Workflow is running", "Wait for the current workflow to finish before closing.\n"
                 "A write may already be in progress. The app does not cancel or retry it.")
            event.ignore()
            return
        self.timer.stop()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        window = DesktopApp()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        show("critical", None, "Cannot open desktop app", "{error}", error=error)
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
