import asyncio
import base64
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from desktop_model import Inputs
from desktop_services import (
    PUBLIC_CLIENT_ID,
    CopilotSignInRequired,
    LiveServices,
    forget_saved_signin,
)

VALUES = Inputs("briefing", "Contoso", "2026-10-19T00:00:00Z", "2026-10-26T00:00:00Z")


class Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self.data = data
        self.text = text
        self.ok = 200 <= status < 400

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, _size):
        yield json.dumps(self.data).encode()

    def json(self):
        return self.data


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.service = LiveServices(self.events.append, {
            "tenant_id": "22222222-2222-2222-2222-222222222222",
            "copilot_model": "",
            "copilot_data_consent": "true",
        })

    def test_auth_uses_separate_resource_scopes_and_memory_cache(self):
        identity = Mock()
        identity.get_accounts.return_value = [{"username": "alex@example.com"}]
        identity.acquire_token_silent.return_value = {
            "access_token": "private-test-token", "id_token_claims": {
                "preferred_username": "alex@example.com", "tid": self.service.settings["tenant_id"],
            },
        }
        factory = Mock(return_value=identity)
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=factory)}):
            self.assertEqual(self.service.token("graph", "briefing"), "private-test-token")
            graph_scopes = identity.acquire_token_silent.call_args.args[0]
            self.assertTrue(all(s.startswith("https://graph.microsoft.com/") for s in graph_scopes))
            self.assertNotIn("https://graph.microsoft.com/Mail.Send", graph_scopes)
            self.service.token("workiq", "briefing")
            self.assertEqual(identity.acquire_token_silent.call_args.args[0], ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"])
            self.service.token("graph", "recap", write=True)
            self.assertIn("https://graph.microsoft.com/Mail.Send", identity.acquire_token_silent.call_args.args[0])
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn("private-test-token", json.dumps(self.events))

    def test_workiq_signin_requests_no_graph_permissions(self):
        identity = Mock()
        identity.get_accounts.return_value = []
        identity.acquire_token_interactive.return_value = {
            "access_token": "workiq-token", "id_token_claims": {
                "preferred_username": "alex@example.com", "tid": self.service.settings["tenant_id"],
            },
        }
        factory = Mock(return_value=identity)
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=factory)}):
            self.service.sign_in("briefing")
        self.assertEqual(factory.call_args.args[0], PUBLIC_CLIENT_ID)
        self.assertEqual(identity.acquire_token_interactive.call_count, 1)
        self.assertEqual(identity.acquire_token_interactive.call_args.kwargs["scopes"], ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"])
        self.assertEqual(identity.acquire_token_interactive.call_args.kwargs["port"], 12798)
        identity.acquire_token_silent.assert_not_called()
        self.assertEqual([e["mode"] for e in self.events if e["type"] == "signed_in"], ["workiq"])

    def test_identity_falls_back_to_token_payload_then_cached_account(self):
        payload = base64.urlsafe_b64encode(json.dumps({
            "upn": "alex@example.com", "tid": "33333333-3333-3333-3333-333333333333",
        }).encode()).decode().rstrip("=")
        identity = Mock()
        identity.get_accounts.return_value = []
        identity.acquire_token_interactive.return_value = {"access_token": f"h.{payload}.s"}
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}):
            self.service.settings["tenant_id"] = ""
            self.service.token("workiq", "briefing", interactive=True)
        self.assertEqual(self.events[-1]["profile"]["account"], "alex@example.com")
        identity = Mock()
        identity.get_accounts.side_effect = [[], [{"username": "sam@example.com", "realm": "44444444-4444-4444-4444-444444444444"}]]
        identity.acquire_token_interactive.return_value = {"access_token": "opaque"}
        self.service.identity = None
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}):
            self.service.settings.update(tenant_id="", account="")
            self.service.token("workiq", "briefing", interactive=True)
        self.assertEqual(self.events[-1]["profile"]["tenant_id"], "44444444-4444-4444-4444-444444444444")

    def test_saved_signin_is_reused_without_a_popup_and_another_account_is_on_request(self):
        tenant = self.service.settings["tenant_id"]
        token = {"access_token": "silent-token", "id_token_claims": {"preferred_username": "alex@example.com", "tid": tenant}}
        identity = Mock()
        identity.get_accounts.return_value = [{"username": "alex@example.com", "realm": tenant}]
        identity.acquire_token_silent.return_value = token
        identity.acquire_token_interactive.return_value = {**token, "access_token": "popup-token"}
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}):
            self.service.sign_in("briefing")
            identity.acquire_token_interactive.assert_not_called()
            self.service.sign_in("briefing", force=True)
        self.assertEqual(identity.acquire_token_interactive.call_count, 1)
        self.assertEqual(identity.acquire_token_interactive.call_args.kwargs["prompt"], "select_account")

    def test_startup_restore_never_opens_a_browser(self):
        tenant = self.service.settings["tenant_id"]
        identity = Mock()
        identity.get_accounts.return_value = []
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}):
            self.service.sign_in("briefing", restore=True)
        identity.acquire_token_interactive.assert_not_called()
        self.assertFalse([e for e in self.events if e["type"] == "signed_in"])
        self.assertIn("No saved sign-in", self.events[-1]["message"])
        identity.get_accounts.return_value = [{"username": "alex@example.com", "realm": tenant}]
        identity.acquire_token_silent.return_value = {
            "access_token": "t", "id_token_claims": {"preferred_username": "alex@example.com", "tid": tenant},
        }
        self.service.identity = None
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}):
            self.service.sign_in("briefing", restore=True)
        self.assertEqual(self.events[-1], {"type": "signed_in", "mode": "workiq"})
        identity.acquire_token_interactive.assert_not_called()

    def test_tokens_stay_in_memory_unless_the_user_allows_saving(self):
        self.assertIsNone(self.service.token_cache())
        self.service.settings["remember_signin"] = "true"
        with tempfile.TemporaryDirectory() as directory, patch("desktop_services.cache_path", return_value=Path(directory) / "cache.bin"):
            if os.name == "nt":
                from msal_extensions import PersistedTokenCache

                self.assertIsInstance(self.service.token_cache(), PersistedTokenCache)
            with patch("msal_extensions.build_encrypted_persistence", side_effect=OSError("no encryption")):
                self.assertIsNone(self.service.token_cache())
        self.assertIn("stays in memory only", self.events[-1]["message"])

    def test_forgetting_deletes_only_the_saved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.bin"
            path.write_bytes(b"x")
            with patch("desktop_services.cache_path", return_value=path):
                self.assertTrue(forget_saved_signin())
                self.assertFalse(path.exists())
                self.assertFalse(forget_saved_signin())

    def test_signin_failure_is_explicit(self):
        identity = Mock()
        identity.get_accounts.return_value = []
        identity.acquire_token_interactive.return_value = {"error": "consent_required", "error_description": "Admin consent needed"}
        factory = Mock(return_value=identity)
        with (
            patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=factory)}),
            self.assertRaisesRegex(RuntimeError, "Admin consent needed"),
        ):
            self.service.token("workiq", "briefing")
        self.assertEqual(identity.acquire_token_interactive.call_args.kwargs["timeout"], 120)

    def test_blank_tenant_uses_organizations_and_populates_verified_identity(self):
        self.service.settings["tenant_id"] = ""
        identity = Mock()
        identity.get_accounts.return_value = []
        identity.acquire_token_interactive.return_value = {
            "access_token": "private-token", "id_token_claims": {
                "preferred_username": "alex@example.com", "tid": "33333333-3333-3333-3333-333333333333",
            },
        }
        factory = Mock(return_value=identity)
        with patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=factory)}):
            self.service.token("graph", "briefing", interactive=True)
        self.assertEqual(factory.call_args.kwargs["authority"], "https://login.microsoftonline.com/organizations")
        self.assertEqual(identity.acquire_token_interactive.call_args.kwargs["prompt"], "select_account")
        self.assertEqual(self.service.settings["account"], "alex@example.com")
        self.assertEqual(self.service.settings["tenant_id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(self.events[-1]["profile"]["tenant_id"], self.service.settings["tenant_id"])

    def test_explicit_tenant_mismatch_is_rejected(self):
        identity = Mock()
        identity.get_accounts.return_value = []
        identity.acquire_token_interactive.return_value = {
            "access_token": "private-token", "id_token_claims": {
                "preferred_username": "alex@example.com", "tid": "33333333-3333-3333-3333-333333333333",
            },
        }
        with (
            patch.dict(sys.modules, {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=identity))}),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            self.service.token("graph", "briefing")

    def test_graph_trace_preserves_arguments_response_and_status_without_token(self):
        request = Mock(return_value=Response(data={"value": []}))
        with patch.dict(sys.modules, {"requests": types.SimpleNamespace(request=request)}):
            data = self.service.graph("private-test-token", "GET", "/me/messages")
        self.assertEqual(data, {"value": []})
        self.assertEqual(self.events[0]["scope"], "Mail.Read")
        self.assertEqual(self.events[-1]["status"], 200)
        self.assertEqual(self.events[-1]["response"], {"value": []})
        self.assertNotIn("private-test-token", json.dumps(self.events))

    def test_graph_policy_failure_is_not_retried(self):
        request = Mock(return_value=Response(status=403, text="Policy denied"))
        with (
            patch.dict(sys.modules, {"requests": types.SimpleNamespace(request=request)}),
            self.assertRaisesRegex(RuntimeError, "Graph 403: Policy denied"),
        ):
            self.service.graph("token", "POST", "/me/sendMail", {})
        self.assertEqual(request.call_count, 1)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.events[-1]["type"], "step_error")

    def test_redirect_on_write_is_not_reported_as_success(self):
        request = Mock(return_value=Response(status=307, text="Redirect"))
        with (
            patch.dict(sys.modules, {"requests": types.SimpleNamespace(request=request)}),
            self.assertRaisesRegex(RuntimeError, "307"),
        ):
            self.service.graph("token", "POST", "/me/events", {})
        self.assertEqual(request.call_count, 1)

    def copilot_fixture(self, content='{"summary":"Text","actions":[]}'):
        session = types.SimpleNamespace(
            session_id="only-this-session",
            send_and_wait=AsyncMock(return_value=types.SimpleNamespace(data=types.SimpleNamespace(content=content))),
            disconnect=AsyncMock(),
        )
        client = types.SimpleNamespace(
            get_auth_status=AsyncMock(return_value=types.SimpleNamespace(isAuthenticated=True)),
            create_session=AsyncMock(return_value=session), delete_session=AsyncMock(),
        )

        @asynccontextmanager
        async def connection():
            yield client
        self.service.copilot_client = connection
        return client, session

    def test_bad_model_output_is_a_visible_failure(self):
        client, session = self.copilot_fixture('{"summary":"Text","actions":"wrong"}')
        with self.assertRaisesRegex(RuntimeError, "actions list"):
            self.service.summarize(VALUES, {key: [] for key in ("messages", "meetings", "files", "notes")})
        self.assertEqual(self.events[-1]["type"], "step_error")
        session.disconnect.assert_awaited_once()
        client.delete_session.assert_awaited_once_with("only-this-session")

    def test_copilot_accepts_a_single_json_code_block(self):
        client, session = self.copilot_fixture('```json\n{"summary":"Text","actions":["Send agenda"]}\n```')
        result = self.service.summarize(VALUES, {key: [] for key in ("messages", "meetings", "files", "notes")})
        self.assertEqual(result, {"summary": "Text", "actions": ["Send agenda"]})
        session.disconnect.assert_awaited_once()
        client.delete_session.assert_awaited_once_with("only-this-session")

    def test_copilot_rejects_malformed_or_missing_summary_content(self):
        for content in (None, "", "Not JSON", 'Explanation\n{"summary":"Text","actions":[]}'):
            with self.subTest(content=content):
                client, session = self.copilot_fixture(content)
                with self.assertRaisesRegex(RuntimeError, "Copilot synthesis failed"):
                    self.service.summarize(VALUES, {key: [] for key in ("messages", "meetings", "files", "notes")})
                session.disconnect.assert_awaited_once()
                client.delete_session.assert_awaited_once_with("only-this-session")

    def test_copilot_requests_session_deletion_even_if_disconnect_fails(self):
        client, session = self.copilot_fixture()
        session.disconnect.side_effect = RuntimeError("Disconnect failed")
        with self.assertRaisesRegex(RuntimeError, "Disconnect failed"):
            self.service.summarize(VALUES, {key: [] for key in ("messages", "meetings", "files", "notes")})
        client.delete_session.assert_awaited_once_with("only-this-session")

    def test_copilot_summary_disables_tools_config_discovery_and_permission_approval(self):
        client, session = self.copilot_fixture()
        evidence = {key: [] for key in ("messages", "meetings", "files", "notes")}
        result = self.service.summarize(VALUES, evidence)
        self.assertEqual(result, {"summary": "Text", "actions": []})
        options = client.create_session.call_args.kwargs
        self.assertEqual(options["available_tools"], [])
        self.assertEqual(options["mcp_servers"], {})
        self.assertFalse(options["enable_config_discovery"])
        self.assertEqual(options["on_permission_request"]({}, {}).kind, "user-not-available")
        self.assertIn("<untrusted_evidence>", session.send_and_wait.call_args.args[0])
        client.delete_session.assert_awaited_once_with("only-this-session")

    def test_copilot_requires_content_sharing_permission(self):
        client, session = self.copilot_fixture()
        self.service.settings["copilot_data_consent"] = "false"
        with self.assertRaisesRegex(RuntimeError, "permission to send"):
            self.service.summarize(VALUES, {key: [] for key in ("messages", "meetings", "files", "notes")})
        client.create_session.assert_not_called()
        session.send_and_wait.assert_not_called()

    def test_copilot_reuses_existing_signin_without_launching_browser(self):
        self.service.check_copilot = AsyncMock(return_value={"login": "alex", "host": "github.com"})
        with patch("desktop_services.subprocess.run") as login:
            self.service.connect_copilot()
        login.assert_not_called()
        self.assertEqual(self.events[-1]["type"], "copilot_connected")

    def test_existing_copilot_signin_is_found_once_and_never_opens_a_browser(self):
        self.service.check_copilot = AsyncMock(return_value={"login": "alex", "host": "github.com"})
        with patch("desktop_services.subprocess.run") as login:
            self.service.connect_copilot(restore=True)
            self.service.connect_copilot()
            self.service.connect_copilot(restore=True)
        login.assert_not_called()
        self.assertEqual(self.service.check_copilot.await_count, 1, "The CLI must be asked only once per session.")
        self.assertEqual([e["type"] for e in self.events if e["type"].startswith("copilot")], ["copilot_connected"] * 3)

    def test_startup_check_without_a_copilot_signin_stays_quiet(self):
        self.service.check_copilot = AsyncMock(side_effect=CopilotSignInRequired())
        with patch("desktop_services.subprocess.run") as login, patch("desktop_services.shutil.which", return_value=r"C:\Tools\copilot.exe"):
            self.service.connect_copilot(restore=True)
        login.assert_not_called()
        self.assertEqual(self.events[-1]["type"], "copilot_missing")
        self.assertIsNone(self.service.copilot_profile)
        self.service.check_copilot = AsyncMock(side_effect=OSError("cli missing"))
        self.service.connect_copilot(restore=True)
        self.assertEqual(self.events[-1]["type"], "copilot_missing")
        with self.assertRaises(OSError):
            self.service.connect_copilot()

    def test_copilot_opens_browser_login_only_after_confirmed_unauthenticated_status(self):
        self.service.check_copilot = AsyncMock(side_effect=[CopilotSignInRequired(), {"login": "alex"}])
        with (
            patch("desktop_services.shutil.which", return_value=r"C:\Tools\copilot.exe"),
            patch("desktop_services.subprocess.run") as login,
        ):
            self.service.connect_copilot()
        self.assertEqual(login.call_args.args[0], [r"C:\Tools\copilot.exe", "login", "--web-flow"])
        self.assertEqual(self.events[-1]["type"], "copilot_connected")

    def test_workiq_batch_checks_schemas_then_stops_on_first_failed_write(self):
        action = {
            "kind": "actions", "planId": "real-plan", "bucketId": "real-bucket",
            "titles": ["Task 1", "Task 2"], "subject": "Follow-up",
            "start": "2026-10-23T01:00:00Z", "end": "2026-10-23T01:30:00Z",
        }
        calls = []

        @asynccontextmanager
        async def session(_token):
            yield object()

        async def call(_session, name, args):
            calls.append((name, args))
            if name == "create_entity" and args["jsonBody"].get("title") == "Task 2":
                raise RuntimeError("Policy denied")
            return {"structuredContent": {"id": "created-id"}}

        self.service.work_session = session
        self.service.work_call = call
        with (
            patch.object(self.service, "token", return_value="token"),
            self.assertRaisesRegex(RuntimeError, "Policy denied"),
        ):
            self.service.write("workiq", Inputs("actions", "Contoso", VALUES.start, VALUES.end), action)
        self.assertEqual([c[0] for c in calls], ["search_paths", "get_schema", "get_schema", "create_entity", "create_entity"])
        writes = [e for e in self.events if e["type"] == "write"]
        self.assertEqual(len(writes), 1)
        self.assertFalse(any(e["type"] == "write_complete" for e in self.events))

    def test_mcp_result_errors_become_failed_calls(self):
        class Session:
            async def call_tool(self, *_args, **_kw):
                return types.SimpleNamespace(model_dump=lambda **_kw: {
                    "content": [{"type": "text", "text": "Access denied for path: /me/sendMail"}],
                })
        with self.assertRaisesRegex(RuntimeError, "Access denied"):
            asyncio.run(self.service.work_call(Session(), "do_action", {"actionUrl": "/me/sendMail"}))
        self.assertEqual(self.events[0]["service"], "WorkIQ")
        self.assertEqual(self.events[-1]["type"], "step_error")


if __name__ == "__main__":
    unittest.main()
