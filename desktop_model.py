"""Workflow definitions shared by the native desktop GUI and its tests."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from desktop_text import t

Event = dict[str, Any]
Emit = Callable[[Event], None]
SCENARIOS = {
    "briefing": ("Weekly briefing", ("Mail.Read", "Calendars.Read", "Files.Read"), ()),
    "recap": ("Meeting prep and recap", ("Mail.Read", "Calendars.Read", "Files.Read"), ("Mail.Send",)),
    "actions": ("Action items and follow-up", ("Mail.Read", "Files.Read"), ("Tasks.ReadWrite", "Calendars.ReadWrite")),
}


def utc_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError(t("A timezone is required."))
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Inputs:
    scenario: str
    customer: str
    start: str
    end: str
    japanese: bool = False

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(t("Choose a supported scenario."))
        if not self.customer.strip() or len(self.customer) > 120:
            raise ValueError(t("Enter a customer or topic between 1 and 120 characters."))
        duration = utc_time(self.end) - utc_time(self.start)
        if not timedelta(0) < duration <= timedelta(days=31):
            raise ValueError(t("Choose a date window longer than zero and no more than 31 days."))

    @classmethod
    def from_dates(cls, scenario: str, customer: str, start: str, end: str, japanese: bool = False) -> Inputs:
        def convert(value: str) -> str:
            local = datetime.strptime(value, "%Y-%m-%d").astimezone()
            return local.astimezone(UTC).isoformat()
        return cls(scenario, customer.strip(), convert(start), convert(end), japanese)


def prompt_for(values: Inputs) -> str:
    goal = {
        "briefing": "Create a weekly customer briefing with progress, decisions, risks, owners, and next steps.",
        "recap": "Prepare for the next customer meeting in this window. Include attendees, agenda, open decisions, and a suggested recap. Do not send anything.",
        "actions": "Extract supported action items from email threads and notes files. Include owners and deadlines only when stated. Do not create tasks or events.",
    }[values.scenario]
    language = (
        "\nWrite the whole answer in Japanese (日本語), including headings and tables. "
        "Keep names, titles, and source links exactly as they are."
        if values.japanese else ""
    )
    return (
        f"{goal}\nCustomer or topic: {values.customer}\n"
        f"Window: {values.start} to {values.end}.\n"
        "Use only sources I can access. Cite source links. State missing evidence. "
        f"Do not perform any writes.{language}"
    )


def graph_url(path: str) -> str:
    prefix = "https://graph.microsoft.com/v1.0"
    if path.startswith(prefix + "/"):
        path = path[len(prefix):]
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(c) < 32 for c in path):
        raise ValueError(t("Rejected an invalid Graph path."))
    parts = urlsplit(prefix + path)
    decoded = unquote(parts.path)
    if (
        parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
        or not decoded.startswith("/v1.0/")
        or any(p in (".", "..") for p in decoded.split("/"))
        or parts.fragment
    ):
        raise ValueError(t("Graph continuation link must stay within Microsoft Graph v1.0."))
    return prefix + path


def graph_scope(method: str, path: str) -> str:
    if method == "POST":
        return {"/me/sendMail": "Mail.Send", "/planner/tasks": "Tasks.ReadWrite", "/me/events": "Calendars.ReadWrite"}[path]
    return "Files.Read" if "/drive/" in path else "Calendars.Read" if "calendarView" in path else "Mail.Read"


def collect_graph(values: Inputs, call: Callable[..., Any], emit: Emit) -> dict[str, Any]:
    evidence: dict[str, Any] = {"messages": [], "meetings": [], "files": [], "notes": [], "limits": []}

    def pages(path: str, key: str) -> None:
        next_path = path
        for _ in range(3):
            data = call("GET", next_path)
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise TypeError(t("Graph did not return a collection for {key}.", key=key))
            evidence[key].extend(data["value"])
            next_path = data.get("@odata.nextLink")
            if not next_path:
                return
            graph_url(next_path)
        evidence["limits"].append(f"{key}: stopped after three pages. Results are partial.")

    term = quote(f'"{values.customer}"', safe="")
    pages(f"/me/messages?$search={term}&$select=id,subject,from,receivedDateTime,bodyPreview,webLink&$top=25", "messages")
    start, end = utc_time(values.start), utc_time(values.end)
    evidence["messages"] = [m for m in evidence["messages"] if start <= utc_time(m["receivedDateTime"]) < end]
    evidence["limits"].append("Mail search is bounded and date-filtered locally. Some matching messages may be missed.")
    if values.scenario != "actions":
        pages(
            f"/me/calendarView?startDateTime={quote(values.start, safe='')}&endDateTime={quote(values.end, safe='')}"
            "&$select=id,subject,start,end,attendees,bodyPreview,webLink&$top=25", "meetings",
        )
        evidence["meetings"] = [
            m for m in evidence["meetings"]
            if values.customer.casefold() in f"{m.get('subject', '')} {m.get('bodyPreview', '')} {json.dumps(m.get('attendees', []))}".casefold()
        ]
        evidence["limits"].append("Calendar matching uses topic text. Meetings with indirect names may be missed.")
    search = quote(values.customer.replace("'", "''"), safe="")
    pages(f"/me/drive/root/search(q='{search}')?$select=id,name,webUrl,file,lastModifiedDateTime&$top=25", "files")
    notes = [f for f in evidence["files"] if re.search(r"\.(txt|md|csv)$", f.get("name", ""), re.IGNORECASE)][:3]
    for note in notes:
        text = call("GET", f"/me/drive/items/{quote(note['id'], safe='')}/content", text=True)
        evidence["notes"].append({"name": note["name"], "webUrl": note.get("webUrl"), "content": text[:16000]})
        if len(text) > 16000:
            evidence["limits"].append(f"{note['name']}: limited to 16,000 characters.")
    evidence["limits"].append(
        "At most three plain-text notes are read. Other files provide metadata only. "
        "Email uses body previews. File search is not date-filtered. Transcripts are excluded."
    )
    emit({"type": "evidence", "data": evidence})
    return evidence


def validate_action(action: dict[str, Any]) -> None:
    def text(key: str, maximum: int = 250) -> None:
        if not isinstance(action.get(key), str) or not 1 <= len(action[key].strip()) <= maximum:
            raise ValueError(t("{key} must contain 1 to {maximum} characters.", key=key, maximum=maximum))
    text("subject")
    if action.get("kind") == "recap":
        text("body", 20000)
        if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", action.get("recipient", "")):
            raise ValueError(t("Enter the recipient's exact email address."))
    elif action.get("kind") == "actions":
        for key in ("planId", "bucketId"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", action.get(key, "")):
                raise ValueError(t("Enter a real Planner {key}.", key=key))
        titles = action.get("titles")
        if not isinstance(titles, list) or not 1 <= len(titles) <= 10:
            raise ValueError(t("Enter between 1 and 10 task titles."))
        if any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 250 for t in titles):
            raise ValueError(t("Each task title must contain 1 to 250 characters."))
        if utc_time(action["end"]) <= utc_time(action["start"]):
            raise ValueError(t("Event end must be after its start."))
    else:
        raise ValueError(t("Unsupported write action."))


def operations_for(action: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    validate_action(action)
    if action["kind"] == "recap":
        message = {
            "subject": action["subject"],
            "body": {"contentType": "Text", "content": action["body"]},
            "toRecipients": [{"emailAddress": {"address": action["recipient"]}}],
        }
        body = {"message": message, "saveToSentItems": True} if mode == "graph" else {"Message": message, "SaveToSentItems": True}
        return [{"path": "/me/sendMail", "tool": "do_action", "body": body}]
    operations = [
        {"path": "/planner/tasks", "tool": "create_entity", "body": {"planId": action["planId"], "bucketId": action["bucketId"], "title": title}}
        for title in action["titles"]
    ]
    operations.append({
        "path": "/me/events", "tool": "create_entity",
        "body": {
            "subject": action["subject"],
            "start": {"dateTime": utc_time(action["start"]).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": utc_time(action["end"]).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        },
    })
    return operations


def check_work_result(result: dict[str, Any], mutation: bool = False) -> dict[str, Any]:
    text = work_text(result)
    if result.get("isError") or re.search(r"must accept.*(?:EULA|license)|Access denied for path", text, re.IGNORECASE):
        raise RuntimeError(text or t("WorkIQ failed without diagnostic detail."))
    values: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            values.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result.get("structuredContent"))
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                data = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            visit(data)

    def status(value: dict[str, Any]) -> int:
        raw = str(value.get("statusCode", value.get("status", "")))
        return int(raw) if raw.isdigit() else 0

    failure = next((v for v in values if v.get("error") or v.get("success") is False or status(v) >= 400), None)
    if failure is not None:
        raise RuntimeError(t("WorkIQ service error: {detail}", detail=json.dumps(failure)))
    if not result.get("structuredContent") and not result.get("content"):
        raise RuntimeError(t("WorkIQ returned no diagnostic detail."))
    if mutation and not any(v.get("id") or v.get("success") is True or 200 <= status(v) < 300 for v in values):
        raise RuntimeError(t("WorkIQ write outcome is indeterminate. Check Microsoft 365 before repeating.\n{detail}", detail=text))
    return result


def work_text(result: dict[str, Any]) -> str:
    blocks = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    return "\n\n".join(blocks) if blocks else json.dumps(result.get("structuredContent", result), indent=2)


def planned_steps(values: Inputs, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Planned calls as (read run, write run). Run only does the read part. Writes wait for a confirmed review."""
    def step(service: str, label: str, args: dict[str, Any], scope: str = "", message: str = "") -> dict[str, Any]:
        # Only exact catalog texts change. User text and the question sent to WorkIQ are left as they are.
        shown = {key: t(value) if isinstance(value, str) else value for key, value in args.items()}
        return {"service": service, "label": t(label), "args": shown, "scope": scope, "state": "planned", "message": t(message) if message else ""}
    writes: list[dict[str, Any]] = []
    if mode == "workiq":
        reads = [step("WorkIQ", "ask", {"question": prompt_for(values)}, "WorkIQAgent.Ask", "Ask for the customer context.")]
        if values.scenario == "recap":
            writes = [
                step("WorkIQ", "get_schema /me/sendMail", {"operationType": "action"}, message="Check the shape of the email action."),
                step("WorkIQ", "do_action /me/sendMail", {"jsonBody": "Confirmed email"}, message="Send the recap."),
            ]
        elif values.scenario == "actions":
            writes = [
                step("WorkIQ", "search_paths /planner/tasks", {"filter": "/planner/tasks"}, message="Find the Planner path."),
                step("WorkIQ", "get_schema /planner/tasks", {"operationType": "create"}),
                step("WorkIQ", "get_schema /me/events", {"operationType": "create"}),
                step("WorkIQ", "create_entity /planner/tasks", {"repeat": "One call per task"}, message="Create the reviewed tasks."),
                step("WorkIQ", "create_entity /me/events", {"jsonBody": "Confirmed personal event"}),
            ]
        return reads, writes
    reads = [step("Graph", "GET /me/messages", {"$search": values.customer}, "Mail.Read", "Find related email.")]
    if values.scenario != "actions":
        reads.append(step("Graph", "GET /me/calendarView", {"start": values.start, "end": values.end}, "Calendars.Read", "Find related meetings."))
    reads += [
        step("Graph", "GET /me/drive/root/search(...)", {"q": values.customer}, "Files.Read", "Find notes and files."),
        step("Graph", "GET /me/drive/items/{id}/content", {"optional": "Up to three plain-text notes"}, "Files.Read", "Read notes using returned file IDs."),
        step("GitHub Copilot", "Copilot SDK: summarize evidence", {"input": "Retrieved sources and limits"}, message="Merge, trim, and summarize."),
    ]
    if values.scenario == "recap":
        writes = [step("Graph", "POST /me/sendMail", {"body": "Confirmed email"}, "Mail.Send", "Send the recap.")]
    elif values.scenario == "actions":
        writes = [
            step("Graph", "POST /planner/tasks", {"repeat": "One call per task"}, "Tasks.ReadWrite", "Create the reviewed tasks."),
            step("Graph", "POST /me/events", {"body": "Confirmed personal event"}, "Calendars.ReadWrite", "Create the follow-up."),
        ]
    return reads, writes


def planned_flow(values: Inputs, mode: str) -> list[dict[str, Any]]:
    """Calls that the Run button makes. Writes are not included: they run only after Review writes."""
    return planned_steps(values, mode)[0]


def planned_writes(values: Inputs, mode: str) -> list[dict[str, Any]]:
    return planned_steps(values, mode)[1]


def write_started(steps: list[dict[str, Any]]) -> bool:
    return any(s.get("label", "").startswith(("do_action", "create_entity", "search_paths", "POST ")) for s in steps)


@dataclass
class RunState:
    steps: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    answer: str = ""
    evidence: Any = None
    result: dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    write_attempted: bool = False
    calls: int = 0
    elapsed: float = 0
    error: str = ""

    def apply(self, event: Event) -> None:
        kind = event["type"]
        if kind == "step":
            self.steps.append({**event, "state": "running"})
            if event["service"] in ("Graph", "WorkIQ"):
                self.calls += 1
        elif kind in ("step_done", "step_error"):
            entry = next((s for s in reversed(self.steps) if s["id"] == event["id"]), None)
            if entry is not None:
                entry.update(event)
                entry["state"] = "failed" if kind == "step_error" else "done"
        elif kind == "evidence":
            self.evidence = event["data"]
        elif kind == "result":
            self.result = event["data"]
            self.answer = self.result.get("summary", "") or work_text(self.result)
        elif kind == "error":
            self.error = event["message"]
            self.ready = False
            for entry in self.steps:
                if entry["state"] == "running":
                    entry.update(state="failed", message=self.error)
        elif kind == "read_complete":
            self.ready = True
