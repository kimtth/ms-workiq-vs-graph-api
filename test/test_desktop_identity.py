import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_identity import account_profiles, discover_accounts, load_profiles

PROFILE = {
    "account": "alex@example.com", "tenant_id": "22222222-2222-2222-2222-222222222222",
    "tenant_name": "Contoso", "source": "Microsoft sign-in",
}


class IdentityTests(unittest.TestCase):
    def test_profiles_deduplicate_by_account_and_tenant(self):
        self.assertEqual(len(account_profiles([PROFILE, {**PROFILE, "account": "Alex@example.com"}])), 1)
        self.assertEqual(account_profiles([{**PROFILE, "tenant_id": "not-a-tenant"}]), [])

    def test_discovery_reads_only_supported_account_metadata_command(self):
        with (
            patch("desktop_identity.shutil.which", return_value=r"C:\Tools\az.cmd"),
            patch("desktop_identity.subprocess.run", return_value=types.SimpleNamespace(stdout=json.dumps([PROFILE]))) as command,
        ):
            profiles = discover_accounts()
        self.assertEqual(profiles[0]["account"], PROFILE["account"])
        self.assertEqual(profiles[0]["source"], "Azure CLI")
        arguments = str(command.call_args.args[0])
        self.assertIn("account", arguments)
        self.assertIn("list", arguments)
        self.assertNotIn("get-access-token", arguments)
        self.assertEqual(command.call_args.kwargs["timeout"], 15)

    def test_no_cli_is_not_treated_as_authenticated(self):
        with patch("desktop_identity.shutil.which", return_value=None):
            self.assertEqual(discover_accounts(), [])

    def test_saved_account_metadata_does_not_require_token_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            self.assertEqual(load_profiles(path), [])
            path.write_text(json.dumps([PROFILE]), encoding="utf-8")
            self.assertEqual(load_profiles(path), [PROFILE])
            path.write_text('{"invalid":"shape"}', encoding="utf-8")
            with self.assertRaises(TypeError):
                load_profiles(path)


if __name__ == "__main__":
    unittest.main()
