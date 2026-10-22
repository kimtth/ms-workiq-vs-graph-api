"""Read account metadata without reading another application's token cache."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from desktop_text import t

GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def account_profiles(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    profiles: dict[tuple[str, str], dict[str, str]] = {}
    for record in records:
        account = record.get("account", "")
        tenant = record.get("tenant_id", "")
        if not isinstance(account, str) or not account or not isinstance(tenant, str) or not GUID.fullmatch(tenant):
            continue
        profiles[(account.casefold(), tenant.casefold())] = {
            "account": account, "tenant_id": tenant,
            "tenant_name": str(record.get("tenant_name") or tenant),
            "source": str(record.get("source", "Microsoft sign-in")),
        }
    return list(profiles.values())


def load_profiles(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise TypeError(t("Remembered accounts must be a list of account metadata objects."))
    return account_profiles(data)


def discover_accounts() -> list[dict[str, str]]:
    cli = shutil.which("az")
    if not cli:
        return []
    query = "[?user.type=='user'].{account:user.name,tenant_id:tenantId,tenant_name:tenantDisplayName}"
    arguments = [cli, "account", "list", "--all", "--output", "json", "--query", query]
    if os.name == "nt" and Path(cli).suffix.casefold() in (".cmd", ".bat"):
        arguments = [os.environ["COMSPEC"], "/d", "/c", "call", *arguments]
    result = subprocess.run(
        arguments, capture_output=True, text=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise TypeError(t("Azure CLI did not return a list of account metadata."))
    return account_profiles([{**item, "source": "Azure CLI"} for item in data if isinstance(item, dict)])
