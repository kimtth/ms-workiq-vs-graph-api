import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from desktop import DesktopApp
from desktop_model import prompt_for
from desktop_services import CopilotSignInRequired

QT = QApplication.instance() or QApplication([])


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = DesktopApp({"tenant_id": "", "copilot_model": "", "copilot_data_consent": "false", "japanese": "false"})
        self.app.show()
        self.pump()

    def tearDown(self):
        for dialog in self.app.findChildren(QDialog):
            dialog.close()
        self.app.close()
        self.app.deleteLater()
        self.pump(.02)

    def pump(self, seconds=.15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(.01)

    def wait_for_worker(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.pump(.05)
        self.assertFalse(self.app.busy, "The background worker did not finish.")

    def open_debugger(self):
        self.app.signed_in.add("workiq")
        self.app.continue_to_debugger()
        self.pump(.02)

    def test_dedicated_signin_screen_is_initial_and_continue_is_gated(self):
        self.assertTrue(self.app.signin_visible)
        self.assertTrue(self.app.signin_page.isVisible())
        self.assertFalse(self.app.workspace.isVisible())
        self.assertFalse(self.app.continue_button.isEnabled())
        self.app.continue_to_debugger()
        self.assertTrue(self.app.signin_visible)
        self.app.tenant_picker.setEditText("not-a-guid")
        self.app.sign_in()
        self.assertIn("valid tenant", self.app.signin_labels["workiq"].text())

    def test_successful_signin_event_enables_continue(self):
        self.app.events.put((self.app.key, {"type": "signed_in", "mode": "workiq"}))
        self.app.events.put((self.app.key, {"type": "finished", "seconds": 1}))
        self.pump()
        self.assertTrue(self.app.continue_button.isEnabled())
        self.app.continue_button.click()
        self.pump(.02)
        self.assertFalse(self.app.signin_visible)
        self.assertEqual(self.app.mode, "workiq")
        self.assertTrue(self.app.workspace.isVisible())

    def test_editing_connection_invalidates_signin(self):
        self.app.signed_in.update(("graph", "workiq"))
        self.app.tenant_picker.setEditText("changed")
        self.assertFalse(self.app.signed_in)
        self.assertFalse(self.app.continue_button.isEnabled())
        self.assertIn("Not used", self.app.signin_labels["graph"].text())

    def test_signin_card_has_every_control_and_scrolls_on_small_windows(self):
        self.app.resize(900, 620)
        self.pump(.05)
        content = self.app.signin_page.widget()
        self.assertGreaterEqual(content.height(), content.sizeHint().height())
        for widget in (self.app.login_button, self.app.continue_button, self.app.consent, self.app.account_picker, self.app.tenant_picker):
            self.assertTrue(self.app.signin_page.isAncestorOf(widget))

    def test_new_verified_account_clears_other_accounts_results_and_remembers_metadata(self):
        self.app.settings.update(account="previous@example.com", tenant_id="22222222-2222-2222-2222-222222222222")
        self.app.signed_in.update(("graph", "workiq"))
        self.app.states[("recap", "workiq")] = self.app.state
        self.app.state.answer = "Previous account data"
        profile = {
            "account": "alex@example.com", "tenant_id": "22222222-2222-2222-2222-222222222222",
            "tenant_name": "Contoso", "source": "Microsoft sign-in",
        }
        with tempfile.TemporaryDirectory() as directory:
            accounts = Path(directory) / "accounts.json"
            settings = Path(directory) / "settings.json"
            with patch("desktop.ACCOUNTS", accounts), patch("desktop.SETTINGS", settings):
                self.app.events.put((self.app.key, {"type": "account", "message": profile["account"], "profile": profile}))
                self.pump()
            self.assertEqual(json.loads(accounts.read_text(encoding="utf-8")), [profile])
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["account"], profile["account"])
        self.assertEqual(self.app.signed_in, {"workiq"})
        self.assertEqual(self.app.state.answer, "")
        self.assertNotIn(("recap", "workiq"), self.app.states)
        self.assertEqual(self.app.tenant_picker.currentText(), profile["tenant_id"])
        self.assertEqual(self.app.account_picker.currentText(), profile["account"])

    def test_available_accounts_fill_account_and_tenant_but_do_not_sign_in(self):
        profile = {
            "account": "alex@example.com", "tenant_id": "22222222-2222-2222-2222-222222222222",
            "tenant_name": "Contoso", "source": "Azure CLI",
        }
        self.app.events.put((self.app.key, {"type": "profiles", "data": [profile]}))
        self.pump()
        self.assertEqual(self.app.account_picker.currentText(), profile["account"])
        self.assertEqual(self.app.tenant_picker.currentText(), profile["tenant_id"])
        self.assertEqual(self.app.account_picker.itemText(0), profile["account"])
        self.assertEqual(self.app.tenant_picker.itemText(0), profile["tenant_id"])
        self.assertFalse(self.app.signed_in)
        self.assertFalse(self.app.continue_button.isEnabled())
        self.app.tenant_picker.setEditText("")
        self.assertEqual(self.app.tenant_picker.currentText(), "")

    def test_account_picker_does_not_overwrite_a_typed_tenant(self):
        tenant = "33333333-3333-3333-3333-333333333333"
        self.app.tenant_picker.setEditText(tenant)
        self.app.events.put((self.app.key, {"type": "profiles", "data": [{
            "account": "alex@example.com", "tenant_id": "22222222-2222-2222-2222-222222222222",
        }]}))
        self.pump()
        self.assertEqual(self.app.tenant_picker.currentText(), tenant)
        self.assertEqual(self.app.account_picker.currentText(), "")

    def test_selecting_an_available_account_fills_its_tenant(self):
        profile = {"account": "alex@example.com", "tenant_id": "22222222-2222-2222-2222-222222222222", "source": "Azure CLI"}
        self.app.profiles = [profile]
        self.app.populate_accounts()
        self.app.account_picker.setCurrentIndex(0)
        self.app.tenant_picker.setEditText("")
        self.app.select_available_account(0)
        self.assertEqual(self.app.tenant_picker.currentText(), profile["tenant_id"])
        self.assertIn("Azure CLI", self.app.status_text)

    def test_remember_option_and_clear_button_control_the_saved_signin(self):
        self.assertFalse(self.app.remember.isChecked())
        with patch("desktop.forget_saved_signin", return_value=True) as forget, patch("desktop.SETTINGS", Path(self.tmp.name) / "s.json"):
            self.app.remember.setChecked(True)
            forget.assert_not_called()
            self.assertEqual(self.app.settings["remember_signin"], "true")
            self.app.remember.setChecked(False)
            self.assertEqual(forget.call_count, 1)
            self.app.tenant_picker.setEditText("changed-by-typing")
            self.assertEqual(forget.call_count, 1, "Editing the tenant must not delete the saved sign-in.")
            self.app.clear_button.click()
            self.assertEqual(forget.call_count, 2)
        self.assertIn("Saved sign-in deleted", self.app.status_text)

    def test_startup_restore_checks_copilot_and_shows_its_state(self):
        with patch("desktop_services.LiveServices.sign_in"), patch("desktop_services.LiveServices.check_copilot", new=AsyncMock(return_value={"login": "alex", "host": "github.com"})):
            self.app.restore_signin()
            self.wait_for_worker()
        self.assertIn("alex", self.app.signin_labels["copilot"].text())
        with patch("desktop_services.LiveServices.sign_in"), patch("desktop_services.LiveServices.check_copilot", new=AsyncMock(side_effect=CopilotSignInRequired())), \
                patch("desktop_services.subprocess.run") as login:
            self.app.service = None
            self.app.restore_signin()
            self.wait_for_worker()
        login.assert_not_called()
        self.assertIn("Not connected", self.app.signin_labels["copilot"].text())
        self.assertIn("No saved GitHub Copilot sign-in", self.app.state.logs[-2] + self.app.state.logs[-1])

    def test_startup_restore_is_quiet_and_does_not_leave_a_failure_message(self):
        calls = []

        def fake_sign_in(_self, scenario, force=False, restore=False):
            calls.append((force, restore))
        with patch("desktop_services.LiveServices.sign_in", fake_sign_in), patch("desktop_services.LiveServices.connect_copilot", lambda *_a, **_k: None):
            self.app.restore_signin()
            self.wait_for_worker()
        self.assertEqual(calls, [(False, True)])
        self.assertFalse(self.app.signed_in)
        self.assertIn("Not signed in", self.app.status_text)
        self.assertNotIn("failed", self.app.signin_labels["workiq"].text().lower())
        self.assertNotIn("Looking", self.app.signin_labels["workiq"].text())

    def test_another_account_button_forces_the_picker_and_sign_in_does_not(self):
        calls = []

        def fake_sign_in(_self, scenario, force=False, restore=False):
            calls.append(force)
        with patch("desktop_services.LiveServices.sign_in", fake_sign_in), patch("desktop.SETTINGS", Path(self.tmp.name) / "s.json"):
            self.app.login_button.click()
            self.wait_for_worker()
            self.app.another_account_button.click()
            self.wait_for_worker()
        self.assertEqual(calls, [False, True])

    def test_scenario_shortcuts_toggle_and_flow_sections(self):
        self.open_debugger()
        self.assertEqual(self.app.mode, "workiq")
        rows = self.app.active_rows
        self.assertEqual([i for i, r in enumerate(rows) if r.get("section")], [0, 2])
        self.assertEqual([r["service"] for r in rows[1:2]], ["WorkIQ"])
        self.assertTrue(all(r["equivalent"] and r["state"] == "reference" for r in rows[3:]))
        self.assertEqual({r["service"] for r in rows[3:]}, {"Graph", "GitHub Copilot"})
        self.app.scenario_buttons["actions"].click()
        self.assertEqual(self.app.scenario, "actions")
        self.assertEqual(len(self.app.active_rows), 9)
        self.app.select_call(1)
        self.assertEqual(self.app.flow.selection, 1)
        self.app.mode_buttons["graph"].click()
        self.assertEqual(self.app.mode, "graph")
        self.assertEqual(len(self.app.active_rows), 8)
        self.assertTrue(self.app.active_rows[0]["section"])
        self.app.shortcuts[1].activated.emit()
        self.assertEqual(self.app.scenario, "briefing")

    def test_checkbox_shows_planned_writes_without_changing_results(self):
        self.open_debugger()
        self.assertFalse(self.app.show_workiq_writes.isChecked())
        for scenario, count in (("recap", 2), ("actions", 5)):
            self.app.choose_scenario(scenario)
            self.app.state.answer = "Keep this answer."
            self.app.state.ready = True
            self.app.refresh()
            hidden = len(self.app.active_rows)
            self.assertTrue(self.app.write_button.isEnabled())
            self.app.select_call(1)
            self.app.show_workiq_writes.click()
            self.assertEqual(len(self.app.active_rows), hidden + count + 1)
            self.assertEqual(self.app.selected_step, -1)
            self.assertEqual(self.app.state.answer, "Keep this answer.")
            self.assertTrue(self.app.write_button.isEnabled())
            self.app.show_workiq_writes.click()
            self.assertEqual(len(self.app.active_rows), hidden)
        self.app.state.steps = [{"label": "create_entity /me/events", "service": "WorkIQ", "state": "done", "args": {}}]
        self.app.refresh()
        self.assertIn(self.app.state.steps[0], self.app.active_rows)

    def test_graph_docs_links_open_in_browser_and_fit_narrow_rows(self):
        self.open_debugger()
        self.app.choose_scenario("actions")
        flow = self.app.flow
        self.assertTrue(flow.doc_links)
        for width in (1200, 380):
            flow.setFixedWidth(width)
            self.pump(.02)
            for index, links in flow.doc_links.items():
                top, height = flow.measure()[0][index]
                for link in links:
                    self.assertTrue(link.isVisible())
                    self.assertGreaterEqual(link.y(), top)
                    self.assertLessEqual(link.geometry().bottom(), top + height)
                    self.assertLessEqual(link.geometry().right(), flow.width())
                    self.assertTrue(link.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        index = next(iter(flow.doc_links))
        link = flow.doc_links[index][0]
        url = flow.rows[index]["docs"][0]
        with patch("desktop.webbrowser.open") as opened:
            pos = QPoint(15, link.height() // 2)
            QTest.mousePress(link, Qt.MouseButton.LeftButton, pos=pos)
            self.pump(.1)
            self.assertIs(flow.doc_links[index][0], link, "Selecting a row must not replace its pressed link.")
            QTest.mouseRelease(link, Qt.MouseButton.LeftButton, pos=pos)
            opened.assert_called_once_with(url)
        self.app.show_workiq_writes.click()
        self.pump(.02)
        self.assertEqual(sum(len(links) for links in flow.doc_links.values()),
                         sum(len(row.get("docs", [])) for row in self.app.active_rows))

    def test_output_and_flow_are_side_by_side(self):
        self.open_debugger()
        self.pump(.05)
        output = self.app.tabs.mapTo(self.app.workspace, QPoint(0, 0))
        flow = self.app.scroll.mapTo(self.app.workspace, QPoint(0, 0))
        self.assertLess(output.x() + self.app.tabs.width(), flow.x() + 1)
        self.assertLessEqual(abs(output.y() - flow.y()), 40)
        self.assertGreater(self.app.tabs.width(), 300)
        self.assertGreater(self.app.scroll.width(), 360)
        self.assertEqual([self.app.tabs.tabText(i) for i in range(3)], ["Answer", "Logs", "Evidence"])

    def assert_nothing_is_clipped(self, root):
        problems = []
        for widget in root.findChildren(QWidget):
            if not widget.isVisible() or widget.width() <= 0:
                continue
            name = f"{type(widget).__name__}:{getattr(widget, 'text', lambda: '')()[:40]!r}"
            wrapped = isinstance(widget, QLabel) and widget.wordWrap()
            if wrapped:
                needed, has = widget.heightForWidth(widget.width()), widget.height()
            elif isinstance(widget, QLabel | QAbstractButton) and widget.text():
                needed, has = widget.sizeHint().width(), widget.width()
            else:
                continue
            if needed > has + 1:
                problems.append(f"{name} needs {needed} px, has {has}")
        self.assertEqual(problems, [])

    @unittest.skipIf(QApplication.platformName() == "offscreen", "Offscreen has no fonts, so text widths are meaningless. Run with QT_QPA_PLATFORM=windows.")
    def test_no_text_is_clipped_on_either_screen_at_common_sizes(self):
        for japanese in (False, True):
            self.app.japanese.setChecked(japanese)
            self.app.pages.setCurrentWidget(self.app.signin_page)
            for size in ((1420, 980), (1100, 700)):
                self.app.resize(*size)
                self.pump(.05)
                with self.subTest(language="ja" if japanese else "en", screen="sign-in", size=size):
                    self.assert_nothing_is_clipped(self.app.signin_page)
            self.open_debugger()
            for size in ((1420, 980), (1100, 700)):
                self.app.resize(*size)
                self.pump(.05)
                with self.subTest(language="ja" if japanese else "en", screen="debugger", size=size):
                    self.assert_nothing_is_clipped(self.app.workspace)
                    self.assert_nothing_is_clipped(self.app.statusBar())

    def test_flow_rows_show_all_text_and_grow_when_the_pane_is_narrow(self):
        self.open_debugger()
        result = {"content": [{"type": "text", "text": " ".join(f"[Doc {i}](https://x-my.sharepoint.com/p/Doc{i}.docx)" for i in range(8))}]}
        for event in ({"type": "step", "id": 1, "service": "WorkIQ", "label": "ask", "args": {"question": "q " * 40}},
                      {"type": "result", "data": result}):
            self.app.events.put((self.app.key, event))
        self.pump()
        flow = self.app.flow
        flow.setFixedWidth(1200)
        self.pump(.05)
        wide = flow.measure()[0][-1]
        flow.setFixedWidth(380)
        self.pump(.05)
        narrow = flow.measure()[0][-1]
        self.assertGreater(narrow[0] + narrow[1], wide[0] + wide[1])
        self.assertEqual(flow.minimumHeight(), narrow[0] + narrow[1] + 12)
        for index, (_top, height) in enumerate(flow.measure()[0]):
            for font, _color, text, block in flow.measure()[1][index]:
                self.assertGreaterEqual(block, flow.text_height(font, text, 10_000), "A block must be at least one line tall.")
            self.assertGreater(height, 0)
        arguments = " ".join(text for row in flow.measure()[1] for _f, _c, text, _h in row)
        self.assertIn("Doc 0", arguments)

    def test_result_citations_drive_graph_reference_and_show_the_answer(self):
        self.open_debugger()
        self.app.tabs.setCurrentWidget(self.app.outputs["Logs"])
        result = {"content": [{"type": "text", "text": "See [Plan](https://contoso-my.sharepoint.com/:w:/p/a/Plan.docx)."}]}
        for event in ({"type": "step", "id": 1, "service": "WorkIQ", "label": "ask", "args": {}},
                      {"type": "step_done", "id": 1, "status": "OK", "ms": 5, "response": {}},
                      {"type": "result", "data": result}, {"type": "read_complete"}, {"type": "finished", "seconds": .1}):
            self.app.events.put((self.app.key, event))
        self.pump()
        reference = [r for r in self.app.active_rows if r.get("equivalent")]
        labels = [r["label"] for r in reference]
        self.assertEqual(labels, ["GET /me/drive/root/search(q='...')", "GET /shares/{u!url}/driveItem", "LLM: merge, trim, summarize"])
        self.assertEqual(reference[1]["args"]["files"], ["Plan"])
        self.assertIs(self.app.tabs.currentWidget(), self.app.outputs["Answer"])
        self.assertIn("cited_sources_inferred_from_links", self.app.outputs["Evidence"].toPlainText())

    def test_japanese_checkbox_changes_the_question_and_clears_old_answers(self):
        self.open_debugger()
        self.assertFalse(self.app.japanese.isChecked())
        self.app.state.answer = "English answer"
        self.app.state.ready = True
        self.app.japanese.setChecked(True)
        self.assertTrue(self.app.get_inputs().japanese)
        self.assertEqual(self.app.state.answer, "")
        asked = []

        def fake_read(_self, mode, values, accept_eula=False):
            asked.append(prompt_for(values))
        with patch("desktop_services.LiveServices.read", fake_read):
            self.app.run()
            self.wait_for_worker()
        self.assertEqual(len(asked), 1)
        self.assertIn("Japanese", asked[0])
        self.app.japanese.setChecked(False)
        self.assertNotIn("Japanese", prompt_for(self.app.get_inputs()))
        planned_question = self.app.active_rows[1]["args"]["question"]
        self.assertNotIn("Japanese", planned_question)

    def test_japanese_question_is_shown_in_the_planned_workiq_call(self):
        self.open_debugger()
        self.app.japanese.setChecked(True)
        self.assertIn("日本語", self.app.active_rows[1]["args"]["question"])

    def test_answer_renders_markdown_and_opens_only_web_links(self):
        self.open_debugger()
        self.app.state.answer = "# Briefing\n\n**Risk:** budget\n\n- [Source](https://example.com/a)\n- <b>raw</b>"
        self.app.refresh()
        view = self.app.outputs["Answer"]
        self.assertNotIn("**", view.toPlainText())
        self.assertNotIn("# Briefing", view.toPlainText())
        self.assertIn("Risk: budget", view.toPlainText())
        with patch("desktop.webbrowser.open") as opened:
            self.app.open_link(QUrl("https://example.com/a"))
            self.app.open_link(QUrl("file:///C:/Windows/System32/calc.exe"))
            self.app.open_link(QUrl("javascript:alert(1)"))
        opened.assert_called_once_with("https://example.com/a")
        with patch("desktop.QApplication.clipboard") as clipboard:
            self.app.tabs.setCurrentWidget(view)
            self.app.copy_output()
        self.assertIn("**Risk:**", clipboard.return_value.setText.call_args.args[0])

    def test_flow_click_and_arrow_keys_skip_section_headers(self):
        self.open_debugger()
        flow = self.app.flow
        first_section_top = flow.measure()[0][0][0]
        QTest.mouseClick(flow, Qt.MouseButton.LeftButton, pos=QPoint(100, first_section_top + 5))
        self.assertEqual(self.app.selected_step, -1)
        QTest.mouseClick(flow, Qt.MouseButton.LeftButton, pos=QPoint(100, flow.row_center(1)))
        self.assertEqual(self.app.selected_step, 1)
        QTest.keyClick(flow, Qt.Key.Key_Down)
        self.assertEqual(self.app.selected_step, 3)
        QTest.keyClick(flow, Qt.Key.Key_Up)
        QTest.keyClick(flow, Qt.Key.Key_Up)
        self.assertEqual(self.app.selected_step, 1)
        self.assertFalse(flow.grab().isNull())

    def test_queue_updates_logs_answers_and_call_details(self):
        self.open_debugger()
        events = [
            {"type": "step", "id": 1, "service": "WorkIQ", "label": "ask", "args": {"question": "q"}},
            {"type": "step_done", "id": 1, "status": "OK", "ms": 82, "response": {}},
            {"type": "result", "data": {"summary": "Verified answer", "actions": []}},
            {"type": "read_complete"},
            {"type": "finished", "seconds": .5},
        ]
        for event in events:
            self.app.events.put((self.app.key, event))
        self.pump()
        self.assertIn("WorkIQ  ask", self.app.outputs["Logs"].toPlainText())
        self.assertIn("Verified answer", self.app.outputs["Answer"].toPlainText())
        self.assertEqual(self.app.state.calls, 1)
        self.assertEqual(self.app.active_rows[1]["state"], "done")

    def test_graph_path_is_blocked_and_never_starts_a_worker(self):
        self.open_debugger()
        self.app.mode_buttons["graph"].click()
        with patch("desktop_services.LiveServices.read") as read:
            self.app.run()
        read.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertIn("not available", self.app.status_text)

    def test_worker_failures_are_visible_without_real_network_calls(self):
        self.open_debugger()
        with (
            patch("desktop_services.LiveServices.read", side_effect=RuntimeError("Permission denied")),
            self.assertLogs("desktop", level="ERROR") as logged,
        ):
            self.app.run()
            self.wait_for_worker()
        self.assertIn("Permission denied", "\n".join(logged.output))
        self.assertIn("Permission denied", self.app.outputs["Logs"].toPlainText())
        self.assertIn("Failed", self.app.status_text)
        self.assertFalse(self.app.state.ready)

    def test_changed_inputs_clear_stale_answers_in_every_scenario(self):
        self.open_debugger()
        self.app.state.ready = True
        self.app.state.answer = "Old customer"
        self.app.choose_scenario("recap")
        self.app.state.ready = True
        self.app.topic.setText("Fabrikam")
        self.app.choose_scenario("briefing")
        self.assertFalse(self.app.state.ready)
        self.assertEqual(self.app.state.answer, "")

    def test_busy_controls_and_write_confirmation_guard(self):
        self.open_debugger()
        self.app.set_busy(True)
        self.app.choose_scenario("actions")
        self.assertEqual(self.app.scenario, "briefing")
        self.assertFalse(self.app.run_button.isEnabled())
        self.assertFalse(self.app.continue_button.isEnabled())
        self.app.set_busy(False)
        self.app.choose_scenario("recap")
        self.assertFalse(self.app.write_button.isEnabled())
        self.app.state.ready = True
        self.app.refresh()
        self.assertTrue(self.app.write_button.isEnabled())
        self.app.state.write_attempted = True
        self.app.refresh()
        self.assertFalse(self.app.write_button.isEnabled())

    def test_write_editor_does_not_execute_without_exact_payload_confirmation(self):
        self.open_debugger()
        self.app.choose_scenario("recap")
        self.app.state.ready = True
        self.app.state.answer = "Reviewed meeting recap."
        editor = self.app.write_dialog()
        self.assertIsInstance(editor, QDialog)
        self.pump(.05)
        editor.fields["recipient"].setText("alex@example.com")
        review = editor.findChild(QPushButton, "review")
        with patch.object(self.app, "start_worker") as execute:
            review.click()
            self.pump(.05)
            confirmation = self.app.confirm
            payload = confirmation.findChild(QPlainTextEdit).toPlainText()
            self.assertIn("alex@example.com", payload)
            self.assertIn("Reviewed meeting recap.", payload)
            execute.assert_not_called()
            next(b for b in confirmation.findChildren(QPushButton) if b.text() == "Back").click()
            self.pump(.05)
            execute.assert_not_called()
            self.assertFalse(self.app.state.write_attempted)

    def test_confirming_exact_payload_executes_once(self):
        self.open_debugger()
        self.app.choose_scenario("recap")
        self.app.state.ready = True
        self.app.state.answer = "Reviewed meeting recap."
        editor = self.app.write_dialog()
        editor.fields["recipient"].setText("alex@example.com")
        with patch.object(self.app, "start_worker") as execute:
            editor.findChild(QPushButton, "review").click()
            self.pump(.05)
            confirm = self.app.confirm.findChild(QPushButton, "execute")
            confirm.click()
            confirm.click()
            execute.assert_called_once()
        self.assertTrue(self.app.state.write_attempted)


if __name__ == "__main__":
    unittest.main()
