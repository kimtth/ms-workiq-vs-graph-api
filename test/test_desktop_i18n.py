import ast
import os
import re
import string
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

import desktop_text
from desktop import DesktopApp
from desktop_i18n import Translated
from desktop_ja import JA
from desktop_model import Inputs
from desktop_reference import build_flow
from desktop_text import Tr, set_language, t

QT = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["desktop.py", "desktop_model.py", "desktop_reference.py", "desktop_services.py", "desktop_identity.py"]
# Calls whose English text argument is shown on screen: name -> positions of the text arguments.
SHOWN = {
    "t": (0,), "set_status": (0,), "set_signin": (1,), "set_message": (0,), "show": (2, 3),
    "QLabel": (0,), "QPushButton": (0,), "QCheckBox": (0,), "_label": (0,), "_button": (0,),
    "setToolTip": (0,), "field": (1,), "step": (3,), "section": (0, 1), "_dialog": (1,),
}
VALUES = Inputs("briefing", "Project Falcon", "2026-10-19T00:00:00Z", "2026-10-26T00:00:00Z")


def literals(node: ast.AST) -> list[str]:
    """String constants an expression can produce, looking through conditional expressions."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return literals(node.body) + literals(node.orelse)
    return []


def shown_texts() -> set[tuple[str, str]]:
    found = set()
    for name in SOURCES:
        for node in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for position in SHOWN.get(func, ()):
                if position < len(node.args):
                    found.update(
                        (name, text) for text in literals(node.args[position])
                        if text.strip() and not re.fullmatch(r"[A-Z][A-Za-z]+(\.[A-Z][A-Za-z]+)+", text)  # Graph scope names
                    )
    return found


class CatalogTests(unittest.TestCase):
    def test_every_text_shown_on_screen_has_a_japanese_translation(self):
        missing = sorted(f"{name}: {text[:70]!r}" for name, text in shown_texts() if text not in JA)
        self.assertEqual(missing, [])

    def test_every_widget_text_including_dialogs_is_in_the_catalog(self):
        app = DesktopApp({"tenant_id": "", "copilot_model": "", "copilot_data_consent": "false", "japanese": "false"})
        self.addCleanup(app.deleteLater)
        app.show()
        app.signed_in.add("workiq")
        app.continue_to_debugger()
        sources = set()

        def collect(root):
            for widget in root.findChildren(QWidget):
                if isinstance(widget, Translated):
                    source = widget._source[0] if isinstance(widget._source, tuple) else widget._source
                    sources.update(text for text in (source, widget._tip) if text)
        collect(app)
        app.connection_dialog().close()
        for scenario in ("recap", "actions"):
            app.choose_scenario(scenario)
            app.state.ready = True
            editor = app.write_dialog()
            collect(editor)
            editor.fields["recipient" if scenario == "recap" else "planId"].setText("x")
            editor.close()
        sources.update(app.tabs.tabText(i) for i in range(app.tabs.count()))
        missing = sorted(text[:70] for text in sources if text not in JA and text.strip())
        self.assertEqual(missing, [])

    def test_translations_keep_the_same_placeholders_as_the_english_text(self):
        def fields(text: str) -> set[str]:
            return {name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name}
        wrong = [key[:60] for key, value in JA.items() if fields(key) != fields(value)]
        self.assertEqual(wrong, [])

    def test_translation_works_and_missing_text_stays_english(self):
        try:
            set_language("en")
            self.assertEqual(t("Cancel"), "Cancel")
            set_language("ja")
            self.assertEqual(t("Cancel"), "キャンセル")
            self.assertEqual(t("No such text in the catalog"), "No such text in the catalog")
            self.assertEqual(t("{title} selected. The end date is exclusive.", title=Tr("Weekly briefing")), "週次ブリーフィング を選びました。終了日は含まれません。")
            self.assertEqual(t("Plain text with {braces} is left alone"), "Plain text with {braces} is left alone")
            with self.assertRaises(ValueError):
                set_language("fr")
        finally:
            set_language("en")


class JapaneseScreenTests(unittest.TestCase):
    def setUp(self):
        # No "japanese" key: the default is on.
        self.app = DesktopApp({"tenant_id": "", "copilot_model": "", "copilot_data_consent": "false"})
        self.app.show()
        self.addCleanup(set_language, "en")

    def tearDown(self):
        self.app.close()
        self.app.deleteLater()

    def test_japanese_is_the_default_for_the_screen_and_the_answers(self):
        self.assertEqual(desktop_text.LANGUAGE, "ja")
        self.assertTrue(self.app.japanese.isChecked())
        self.assertTrue(self.app.get_inputs().japanese)
        self.assertEqual(self.app.windowTitle(), "WorkIQ Graph デバッガー")
        self.assertEqual(self.app.login_button.text(), "Microsoft 365 でサインイン")
        self.assertEqual(self.app.status_text, "準備完了です。シナリオを選び、Graph か WorkIQ を選んで実行してください。")
        self.assertEqual(self.app.signin_labels["workiq"].text(), "未接続です。WorkIQ のテナント有効化と同意が必要です。")
        self.assertEqual(self.app.japanese.text(), "日本語で表示・回答")

    def test_workspace_labels_buttons_tabs_and_flow_are_japanese(self):
        self.app.signed_in.add("workiq")
        self.app.continue_to_debugger()
        self.assertEqual(self.app.run_button.text(), "実行  F5")
        self.assertEqual(self.app.scenario_buttons["briefing"].text(), "1  週次ブリーフィング")
        self.assertEqual([self.app.tabs.tabText(i) for i in range(3)], ["回答", "ログ", "根拠"])
        sections = [r for r in self.app.active_rows if r.get("section")]
        self.assertEqual(sections[0]["title"], "WorkIQ - このアプリが行った呼び出し")
        self.assertIn("実行されません", sections[1]["title"])
        self.assertEqual(self.app.outputs["Answer"].toPlainText(), "まだ回答はありません。")
        self.assertIn("計画", self.app.plan_label.text())
        self.assertEqual(self.app.metrics_label.text(), "M365 呼び出し 0 件   |   0.0 秒")

    def test_input_errors_and_status_messages_are_japanese(self):
        self.app.signed_in.add("workiq")
        self.app.continue_to_debugger()
        self.app.choose_scenario("recap")
        self.assertEqual(self.app.status_text, "会議の準備とまとめ を選びました。終了日は含まれません。")
        self.app.topic.setText("")
        with self.assertRaisesRegex(ValueError, "1〜120 文字"):
            self.app.get_inputs()

    def test_switching_the_checkbox_translates_the_whole_screen_both_ways(self):
        self.app.signed_in.add("workiq")
        self.app.continue_to_debugger()
        self.app.choose_scenario("recap")
        self.app.japanese.setChecked(False)
        self.assertEqual(desktop_text.LANGUAGE, "en")
        self.assertFalse(self.app.get_inputs().japanese)
        self.assertEqual(self.app.run_button.text(), "Run  F5")
        self.assertEqual(self.app.scenario_buttons["recap"].text(), "2  Meeting prep and recap")
        self.assertEqual(self.app.status_text, "Meeting prep and recap selected. The end date is exclusive.")
        self.assertEqual(self.app.windowTitle(), "WorkIQ Graph Debugger")
        self.assertEqual([self.app.tabs.tabText(i) for i in range(3)], ["Answer", "Logs", "Evidence"])
        self.assertEqual(self.app.japanese.text(), "Japanese UI and answers (日本語)")
        self.assertEqual(self.app.metrics_label.text(), "0 M365 calls   |   0.0 s")
        self.app.japanese.setChecked(True)
        self.assertEqual(self.app.status_text, "会議の準備とまとめ を選びました。終了日は含まれません。")
        self.assertEqual(self.app.run_button.text(), "実行  F5")

    def test_the_choice_is_saved_and_old_answers_are_cleared(self):
        import json
        import tempfile
        from unittest.mock import patch

        self.app.signed_in.add("workiq")
        self.app.continue_to_debugger()
        self.app.state.answer = "古い回答"
        self.app.state.ready = True
        with tempfile.TemporaryDirectory() as directory, patch("desktop.SETTINGS", Path(directory) / "s.json"):
            self.app.japanese.setChecked(False)
            self.assertEqual(json.loads((Path(directory) / "s.json").read_text(encoding="utf-8"))["japanese"], "false")
        self.assertEqual(self.app.state.answer, "")
        self.assertFalse(self.app.state.ready)

    def test_the_workiq_question_and_flow_follow_the_language(self):
        japanese = Inputs.from_dates("briefing", "Project Falcon", "2026-10-19", "2026-10-26", japanese=True)
        rows = build_flow(japanese, "workiq", [], None)
        self.assertIn("日本語", rows[1]["args"]["question"])
        set_language("en")
        self.assertEqual(build_flow(VALUES, "workiq", [], None)[0]["title"], "WorkIQ - calls made by this app")


if __name__ == "__main__":
    unittest.main()
