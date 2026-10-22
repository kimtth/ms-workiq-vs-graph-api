"""Graph reference flow: the calls a Graph client would make for what WorkIQ cited.

WorkIQ does not report its internal calls. These rows are built from the links in its answer.
They are shown for comparison and are never executed.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from desktop_model import Inputs, planned_flow, planned_writes, work_text, write_started
from desktop_text import Tr, t

LINK_PATTERN = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"'\\]+")
OUTLOOK_HOSTS = ("outlook.office.com", "outlook.office365.com", "outlook.live.com")
MAX_LISTED = 10
GRAPH_DOCS = "https://learn.microsoft.com/en-us/graph/"
API_DOCS = {
    "GET /me/messages": ["user-list-messages"],
    "GET /me/calendarView": ["calendar-list-calendarview"],
    "POST /search/query": ["search-query"],
    "GET /me/drive/root/search(q='...')": ["driveitem-search"],
    "GET /me/messages/{id}": ["message-get"],
    "GET /me/events/{id}": ["event-get"],
    "GET /shares/{u!url}/driveItem": ["shares-get"],
    "GET /sites/{host}:/{site}  then  GET /sites/{id}/pages/{id}": ["site-getbypath", "sitepage-get"],
    "GET /chats/{chat-id}/messages/{message-id}": ["chatmessage-get"],
    "GET /me/onlineMeetings/{id}/transcripts": ["onlinemeeting-get", "onlinemeeting-list-transcripts"],
    'GET /me/people?$search="{name}"': ["user-list-people"],
    "POST /me/sendMail": ["user-sendmail"],
    "POST /planner/tasks": ["planner-post-tasks"],
    "POST /me/events": ["user-post-events"],
}


def clean(url: str) -> str:
    """Drop the fragment and trailing punctuation. `url#1` and `url` are the same item."""
    return url.split("#", 1)[0].rstrip(".,;")


def classify(url: str) -> tuple[str, dict[str, Any]]:
    """Return the kind of Microsoft 365 item a link points to, with the identifiers in the link."""
    parts = urlsplit(url)
    host, path, query = parts.netloc.lower(), unquote(parts.path), parse_qs(parts.query)
    low = path.lower()
    segments = [s for s in path.split("/") if s]
    last = segments[-1] if segments else host
    if host.endswith("teams.microsoft.com"):
        if "/l/meeting/details" in low or "meetup-join" in low:
            return "calendar", {"id": query.get("eventId", [""])[0]}
        if "/l/message/" in low and "message" in segments and len(segments) > segments.index("message") + 2:
            at = segments.index("message")
            return "chat", {"chat_id": segments[at + 1], "message_id": segments[at + 2]}
        return "chat", {}
    if host in OUTLOOK_HOSTS:
        if "/calendar" in low:
            return "calendar", {"id": query.get("itemid", query.get("eventId", [""]))[0]}
        found = re.search(r"/read/([^/?]+)", path)
        return "mail", {"id": found.group(1) if found else ""}
    if host.endswith("office.com") and low.startswith("/search"):
        return "people", {"name": query.get("q", [""])[0]}
    if host.endswith("sharepoint.com"):
        site = low.startswith(("/teams/", "/sites/"))
        sourcedoc = query.get("sourcedoc", [""])[0].strip("{}").lower()
        name = query.get("file", [""])[0]
        if not name:
            # OneNote links have no file= value. The notebook name is inside wd=target(/Name.one/).
            notebook = re.search(r"target\(/?([^/)]+)", query.get("wd", [""])[0])
            name = notebook.group(1) if notebook else (last if last.lower() != "doc.aspx" else f"Doc.aspx ({sourcedoc[:8]})")
        if "/sitepages/" in low:
            return "page", {"name": name, "site": True, "path": "/".join(segments[:2])}
        if "/recordings/" in low or name.lower().endswith((".mp4", ".m4a")):
            return "recording", {"name": name, "site": site}
        return "file", {"name": name, "site": site, "sourcedoc": sourcedoc}
    if host.endswith(("onedrive.live.com", "1drv.ms")):
        return "file", {"name": last, "site": False}
    return "other", {}


def workiq_sources(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Items WorkIQ cited, one per distinct item. This is an inference from links, not a trace."""
    text = work_text(result)
    titles: dict[str, str] = {}
    for title, url in LINK_PATTERN.findall(text):
        # Footnote links such as [1](...) carry a number, not a title.
        if not title.strip().isdigit():
            titles.setdefault(clean(url), title.strip())
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in URL_PATTERN.findall(text + "\n" + json.dumps(result)):
        url = clean(raw)
        if not url:
            continue
        kind, info = classify(url)
        # The same file can be cited with and without #fragments or extra parameters. Its document ID is the identity.
        identity = info.get("id") or info.get("message_id") or info.get("sourcedoc") or info.get("name") or url
        found.setdefault((kind, identity), {"kind": kind, "title": titles.get(url) or info.get("name") or urlsplit(url).netloc, "url": url, **info})
    return list(found.values())


def listed(values: list[Any]) -> list[Any]:
    return values[:MAX_LISTED] + ([t("... +{count} more", count=len(values) - MAX_LISTED)] if len(values) > MAX_LISTED else [])


def graph_reference(values: Inputs, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The Graph calls a client needs for the same task, in order: find, fetch, resolve, summarize, write."""
    def step(label: str, args: dict[str, Any], scopes: list[str], message: str, calls: int = 1, service: str = "Graph") -> dict[str, Any]:
        # Only exact catalog texts change here. Already translated text, user text, and API names pass through.
        shown = {key: t(value) if isinstance(value, str) else value for key, value in args.items()}
        return {
            "service": service, "label": t(label), "args": shown, "scope": ", ".join(scopes), "scopes": scopes,
            "message": t(message), "calls": calls, "state": "reference", "equivalent": True,
        }
    sources = workiq_sources(result) if result else []
    group: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        group.setdefault(source["kind"], []).append(source)
    ahead = not result
    rows: list[dict[str, Any]] = []

    # 1. Find candidates. Graph has no search by meaning, so the client searches by keyword and date.
    if ahead or "mail" in group:
        rows.append(step("GET /me/messages", {"$search": f'"{values.customer}"', "$top": 25}, ["Mail.Read"],
                         "Find candidate emails by keyword. The client pages and filters by date itself."))
    if values.scenario != "actions" and (ahead or "calendar" in group):
        rows.append(step("GET /me/calendarView", {"startDateTime": values.start, "endDateTime": values.end}, ["Calendars.Read"],
                         "List every event in the window. The client filters by subject itself."))
    file_kinds = [s for kind in ("file", "recording", "page") for s in group.get(kind, [])]
    if ahead or file_kinds:
        if any(s.get("site") for s in file_kinds):
            rows.append(step("POST /search/query", {"entityTypes": ["driveItem"], "query": {"queryString": values.customer}},
                             ["Files.Read.All", "Sites.Read.All"],
                             "WorkIQ cited files in SharePoint sites. Drive search covers only your own OneDrive, so the Search API is needed."))
        else:
            rows.append(step("GET /me/drive/root/search(q='...')", {"q": values.customer}, ["Files.Read"],
                             "Search your own OneDrive by keyword."))

    # 2. Fetch every item WorkIQ cited, one call each. Only known after the answer arrives.
    if ahead:
        rows.append(step("GET <item>/{id}", {"note": "One call per item found. The list is known after WorkIQ answers."}, [],
                         "Each email, event, file, and person is a separate call."))
    else:
        def fetch(kind: str, label: str, args: dict[str, Any], scopes: list[str], noun: str, extra: str = "", factor: int = 1) -> None:
            items = group.get(kind, [])
            if items:
                rows.append(step(label, {**args, "count": len(items)}, scopes,
                                 f"{t('WorkIQ cited {count} {noun}.', count=len(items), noun=Tr(noun))} {t(extra)}".strip(), calls=len(items) * factor))
        fetch("mail", "GET /me/messages/{id}", {"ids": listed([s["id"] for s in group.get("mail", [])]), "header": 'Prefer: IdType="ImmutableId"'},
              ["Mail.Read"], "email(s)", "One call each, by the ID in the link.")
        fetch("calendar", "GET /me/events/{id}", {"ids": listed([s["id"] for s in group.get("calendar", [])])},
              ["Calendars.Read"], "meeting(s)", "One call each, by the event ID in the link.")
        shared = [s for kind in ("file", "recording") for s in group.get(kind, [])]
        if shared:
            rows.append(step("GET /shares/{u!url}/driveItem", {"files": listed([s["title"] for s in shared]), "count": len(shared)},
                             ["Files.Read.All"],
                             t("WorkIQ cited {files} file(s), including {recordings} recording(s). One call each, using the sharing link encoded as u!base64url.",
                               files=len(shared), recordings=len(group.get("recording", []))), calls=len(shared)))
        fetch("page", "GET /sites/{host}:/{site}  then  GET /sites/{id}/pages/{id}", {"pages": listed([s["title"] for s in group.get("page", [])])},
              ["Sites.Read.All"], "site page(s)", "Two calls each: find the site, then read the page.", factor=2)
        fetch("chat", "GET /chats/{chat-id}/messages/{message-id}",
              {"messages": listed([{"chat": s.get("chat_id", ""), "message": s.get("message_id", "")} for s in group.get("chat", [])])},
              ["Chat.Read"], "Teams message(s)", "WorkIQ found them by meaning. A Graph client needs the chat and message IDs first.")
        if "transcript" in work_text(result).lower():
            rows.append(step("GET /me/onlineMeetings/{id}/transcripts", {"lookup": "GET /me/onlineMeetings?$filter=JoinWebUrl eq '<join url>'"},
                             ["OnlineMeetingTranscript.Read.All"],
                             "The answer mentions a transcript. This needs a meeting lookup, then the transcript. Admin consent is required.", calls=2))
        # 3. People. Attendees, senders, and recipients already come back inside events and emails.
        names = listed([s["title"] for s in group.get("people", [])])
        if group.get("people") and (group.get("calendar") or group.get("mail")):
            rows.append(step("(people: inside the event and email responses)", {"names": names, "count": len(group["people"])}, [],
                             t("WorkIQ linked {count} people. Graph returns attendees, senders, and recipients inside each event or email, so reading them needs no People call.",
                               count=len(group["people"])), calls=0))
        else:
            fetch("people", "GET /me/people?$search=\"{name}\"", {"names": names}, ["People.Read"], "person link(s)", "One search each.")
        others = group.get("other", [])
        if others:
            rows.append(step("(no Graph mapping)", {"links": listed([s["url"] for s in others])}, [],
                             t("{count} cited link(s) are not Microsoft 365 items a Graph call can read.", count=len(others)), calls=0))
        if not sources:
            rows.append(step("(no citations found)", {"note": "The answer had no source links, so no Graph call can be inferred."}, [],
                             "Nothing to map. Ask WorkIQ to cite its sources.", calls=0))

    # 4. Graph returns raw items. A model still has to read and summarize them.
    rows.append(step("LLM: merge, trim, summarize", {"input": "Everything fetched above"}, [],
                     "Graph returns raw items, not an answer. WorkIQ returned the answer itself.", calls=0, service="GitHub Copilot"))
    # 5. Writes map one to one.
    rows += [{**s, "scopes": [s["scope"]], "calls": 1, "state": "reference", "equivalent": True} for s in planned_writes(values, "graph")]
    for row in rows:
        pages = API_DOCS.get(row["label"], [])
        if pages:
            row["docs"] = [f"{GRAPH_DOCS}api/{page}?view=graph-rest-1.0" for page in pages]
        elif row["label"] == "GET <item>/{id}":
            row["docs"] = [f"{GRAPH_DOCS}api/overview?view=graph-rest-1.0"]
    return rows


def build_flow(values: Inputs, mode: str, steps: list[dict[str, Any]], result: dict[str, Any] | None, *, show_workiq_writes: bool = False) -> list[dict[str, Any]]:
    """Rows for the timeline. Section rows separate what the app ran, what waits for confirmation, and the Graph reference."""
    def section(title: str, detail: str, service: str) -> dict[str, Any]:
        return {"section": True, "title": title, "detail": detail, "service": service}
    service = "Graph" if mode == "graph" else "WorkIQ"
    later = [] if write_started(steps) or (mode == "workiq" and not show_workiq_writes) else planned_writes(values, mode)
    pending = [
        section(t("{service} - writes, only after you confirm", service=service),
                t("Run does not make these calls. They follow Review writes... and an exact-payload confirmation."), service),
        *later,
    ] if later else []
    if mode == "graph":
        title = t("Graph - executed by this app") if steps else t("Graph - planned (not available in this environment)")
        return [section(title, "", "Graph"), *(steps or planned_flow(values, "graph")), *pending]
    actual = steps or planned_flow(values, "workiq")
    reference = graph_reference(values, result)
    scopes = sorted({scope for row in reference for scope in row.get("scopes", [])})
    calls = sum(row.get("calls", 1) for row in reference)
    summary = "{calls} call(s), {count} permission(s): {scopes}" if result else "at least {calls} call(s), {count} permission(s): {scopes}"
    return [
        section(t("WorkIQ - calls made by this app"), t("{count} call(s), 1 permission: WorkIQAgent.Ask", count=len(actual)), "WorkIQ"),
        *actual,
        *pending,
        section(t("Graph reference - what a Graph client would do instead. Never executed."),
                t(summary, calls=calls, count=len(scopes), scopes=", ".join(scopes) or t("none")), "Graph"),
        *reference,
    ]
