import json
import unittest

from desktop_model import Inputs
from desktop_reference import build_flow, classify, graph_reference, workiq_sources

VALUES = Inputs("briefing", "Project Falcon", "2026-10-19T00:00:00Z", "2026-10-26T00:00:00Z")
EVENT = "https://teams.microsoft.com/l/meeting/details?eventId=AAMkAGEVENT1%3d%3d"
# Link shapes seen in a real WorkIQ answer, with made-up identifiers.
ANSWER = (
    f"Next [Falcon weekly]({EVENT}&EntityRepresentationId=abc) organized by "
    "[Alex Doe](https://www.office.com/search?q=Alex+Doe&EntityRepresentationId=1). "
    f"[1]({EVENT}#573675) [2]({EVENT}#573676)\n"
    "See [Falcon schedule](https://contoso.sharepoint.com/teams/FalconSite/_layouts/15/Doc.aspx?sourcedoc=%7BAAA%7D&file=Falcon%20Schedule.xlsx&action=default) "
    "and https://contoso-my.sharepoint.com/personal/alex_contoso_com/Documents/Recordings/Falcon%20Weekly-20261020.mp4?web=1 "
    "and https://contoso.sharepoint.com/teams/FalconSite/SitePages/Key-Messages.aspx?web=1 "
    "and https://outlook.office.com/mail/MBX%3Aid%4072f/applink/read/AAkALgMAIL1-x_y/?ds=alex%40contoso.com "
    "and https://outlook.office.com/mail/MBX%3Aid%4072f/applink/read/AAkALgMAIL2/?ds=alex%40contoso.com "
    "and https://teams.microsoft.com/l/message/19:meeting_abc@thread.v2/1792458181532?context=%7B%22contextType%22:%22chat%22%7D "
    "and https://example.com/other. The WS2 transcript was excluded."
)
RESULT = {"content": [{"type": "text", "text": ANSWER}]}


class ReferenceTests(unittest.TestCase):
    def test_each_link_shape_maps_to_the_item_it_identifies(self):
        self.assertEqual(classify(EVENT), ("calendar", {"id": "AAMkAGEVENT1=="}))
        self.assertEqual(classify("https://www.office.com/search?q=Alex+Doe")[1], {"name": "Alex Doe"})
        self.assertEqual(classify("https://outlook.office.com/mail/MBX%3Ax/applink/read/AAkALgMAIL1-x_y/?ds=a")[1], {"id": "AAkALgMAIL1-x_y"})
        self.assertEqual(
            classify("https://teams.microsoft.com/l/message/19:meeting_abc@thread.v2/1792458181532?context=x")[1],
            {"chat_id": "19:meeting_abc@thread.v2", "message_id": "1792458181532"},
        )
        kind, info = classify("https://contoso.sharepoint.com/teams/FalconSite/_layouts/15/Doc.aspx?sourcedoc=%7BA%7D&file=Falcon%20Schedule.xlsx")
        self.assertEqual((kind, info["name"], info["site"]), ("file", "Falcon Schedule.xlsx", True))
        self.assertEqual(classify("https://contoso-my.sharepoint.com/personal/a_c_com/Documents/Recordings/Weekly.mp4?web=1")[0], "recording")
        self.assertEqual(classify("https://contoso.sharepoint.com/teams/FalconSite/SitePages/Key-Messages.aspx")[0], "page")
        self.assertEqual(classify("https://example.com/x")[0], "other")

    def test_one_item_cited_several_ways_is_counted_once_and_footnotes_are_not_titles(self):
        sources = workiq_sources(RESULT)
        events = [s for s in sources if s["kind"] == "calendar"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Falcon weekly")
        self.assertEqual([s["kind"] for s in sources].count("mail"), 2)
        self.assertNotIn("1", [s["title"] for s in sources])

    def test_one_file_cited_with_different_parameters_is_one_call_and_notebooks_get_a_name(self):
        base = "https://contoso.sharepoint.com/teams/FalconSite/_layouts/15/Doc.aspx?sourcedoc=%7BAAAA-1111%7D&file=Plan.pptx&action=edit"
        copy = "https://contoso-my.sharepoint.com/personal/a_contoso_com/_layouts/15/Doc.aspx?sourcedoc=%7Bbbbb-2222%7D&file=Plan.pptx&action=edit"
        note = ("https://contoso.sharepoint.com/teams/FalconSite/_layouts/15/Doc.aspx?action=edit&sourcedoc=%7Bcccc-3333%7D"
                "&wd=target%28/Delivery.one/%29&wdpartid=%7B1%7D")
        text = f"[Plan]({base}&DefaultItemOpen=1#357efe) [Plan again]({base}&mobileredirect=true) [Plan copy]({copy}) [Notes]({note})"
        sources = workiq_sources({"content": [{"type": "text", "text": text}]})
        self.assertEqual(sorted(s["name"] for s in sources), ["Delivery.one", "Plan.pptx", "Plan.pptx"])
        shares = next(r for r in graph_reference(VALUES, {"content": [{"type": "text", "text": text}]}) if r["label"].startswith("GET /shares"))
        self.assertEqual(shares["calls"], 3)

    def test_graph_flow_has_find_fetch_summarize_steps_with_real_identifiers(self):
        rows = graph_reference(VALUES, RESULT)
        by_label = {r["label"]: r for r in rows}
        search = by_label["POST /search/query"]
        self.assertEqual(search["args"]["query"]["queryString"], "Project Falcon")
        self.assertEqual(set(search["scopes"]), {"Files.Read.All", "Sites.Read.All"})
        self.assertEqual(by_label["GET /me/calendarView"]["args"]["startDateTime"], VALUES.start)
        self.assertEqual(by_label["GET /me/messages/{id}"]["args"]["ids"], ["AAkALgMAIL1-x_y", "AAkALgMAIL2"])
        self.assertEqual(by_label["GET /me/messages/{id}"]["calls"], 2)
        self.assertEqual(by_label["GET /me/events/{id}"]["args"]["ids"], ["AAMkAGEVENT1=="])
        self.assertEqual(by_label["GET /shares/{u!url}/driveItem"]["calls"], 2)
        self.assertEqual(by_label["GET /chats/{chat-id}/messages/{message-id}"]["args"]["messages"],
                         [{"chat": "19:meeting_abc@thread.v2", "message": "1792458181532"}])
        self.assertEqual(by_label["(people: inside the event and email responses)"]["calls"], 0)
        self.assertEqual(by_label["(people: inside the event and email responses)"]["args"]["names"], ["Alex Doe"])
        pages = by_label["GET /sites/{host}:/{site}  then  GET /sites/{id}/pages/{id}"]
        self.assertEqual(pages["calls"], 2)
        self.assertEqual(by_label["GET /me/onlineMeetings/{id}/transcripts"]["scopes"], ["OnlineMeetingTranscript.Read.All"])
        self.assertEqual(by_label["(no Graph mapping)"]["calls"], 0)
        self.assertEqual(rows[-1]["label"], "LLM: merge, trim, summarize")
        self.assertEqual(rows[-1]["service"], "GitHub Copilot")
        labels = [r["label"] for r in rows]
        self.assertLess(labels.index("POST /search/query"), labels.index("GET /me/messages/{id}"))
        self.assertLess(labels.index("GET /me/messages/{id}"), labels.index("LLM: merge, trim, summarize"))

    def test_people_need_a_search_only_when_no_event_or_email_carries_them(self):
        only_people = {"content": [{"type": "text", "text": "[Alex Doe](https://www.office.com/search?q=Alex+Doe) [Sam Roe](https://www.office.com/search?q=Sam+Roe)"}]}
        rows = {r["label"]: r for r in graph_reference(VALUES, only_people)}
        people = rows['GET /me/people?$search="{name}"']
        self.assertEqual((people["calls"], people["scopes"]), (2, ["People.Read"]))

    def test_writes_wait_in_their_own_section_until_a_write_runs(self):
        actions = Inputs("actions", "Project Falcon", VALUES.start, VALUES.end)
        rows = build_flow(actions, "workiq", [], None, show_workiq_writes=True)
        titles = [r["title"] for r in rows if r.get("section")]
        self.assertEqual(titles[1], "WorkIQ - writes, only after you confirm")
        first, second = [i for i, r in enumerate(rows) if r.get("section")][:2]
        self.assertEqual([r["label"] for r in rows[:second] if not r.get("section")][:1], ["ask"])
        waiting = rows[second + 1:[i for i, r in enumerate(rows) if r.get("section")][2]]
        self.assertEqual(len(waiting), 5)
        self.assertTrue(all(r["state"] == "planned" and not r.get("equivalent") for r in waiting))
        self.assertEqual(first, 0)
        ran = [{"label": "ask", "service": "WorkIQ", "state": "done", "args": {}}, {"label": "search_paths /planner/tasks", "service": "WorkIQ", "state": "done", "args": {}}]
        later = build_flow(actions, "workiq", ran, None, show_workiq_writes=True)
        self.assertNotIn("WorkIQ - writes, only after you confirm", [r["title"] for r in later if r.get("section")])
        self.assertEqual(len(build_flow(VALUES, "workiq", [], None)), 2 + 1 + len([r for r in build_flow(VALUES, "workiq", [], None) if r.get("equivalent")]))

    def test_workiq_writes_are_hidden_by_default_without_hiding_attempted_calls(self):
        for scenario in ("recap", "actions"):
            values = Inputs(scenario, VALUES.customer, VALUES.start, VALUES.end)
            rows = build_flow(values, "workiq", [], None)
            self.assertEqual(len([r for r in rows if r.get("section")]), 2)
            self.assertFalse(any(r["service"] == "WorkIQ" and r.get("label") != "ask"
                                 for r in rows if not r.get("section")))
            attempted = [{"label": "create_entity /me/events", "service": "WorkIQ", "state": "failed", "args": {}}]
            self.assertIn(attempted[0], build_flow(values, "workiq", attempted, None))
        self.assertEqual(len([r for r in build_flow(values, "graph", [], None) if r.get("section")]), 2)

    def test_reference_api_rows_have_official_docs_but_non_api_rows_do_not(self):
        for scenario in ("briefing", "recap", "actions"):
            values = Inputs(scenario, VALUES.customer, VALUES.start, VALUES.end)
            for result in (None, RESULT, {"content": [{"type": "text", "text": "No links."}]}):
                for row in graph_reference(values, result):
                    if row["label"].startswith(("GET ", "POST ")):
                        self.assertTrue(row["docs"], row["label"])
                        self.assertTrue(all(url.startswith("https://learn.microsoft.com/en-us/graph/api/")
                                            and url.endswith("?view=graph-rest-1.0") for url in row["docs"]))
                    else:
                        self.assertNotIn("docs", row)
        rows = {r["label"]: r for r in graph_reference(VALUES, RESULT)}
        self.assertEqual(len(rows["GET /sites/{host}:/{site}  then  GET /sites/{id}/pages/{id}"]["docs"]), 2)
        self.assertIn("api/message-get?", rows["GET /me/messages/{id}"]["docs"][0])

    def test_section_totals_count_every_graph_call_against_one_workiq_call(self):
        rows = build_flow(VALUES, "workiq", [], RESULT)
        sections = [r for r in rows if r.get("section")]
        reference = [r for r in rows if r.get("equivalent")]
        total = sum(r["calls"] for r in reference)
        self.assertIn(f"{total} call(s)", sections[1]["detail"])
        self.assertGreater(total, 10)
        self.assertIn("1 call(s), 1 permission: WorkIQAgent.Ask", sections[0]["detail"])
        self.assertIn("Never executed", sections[1]["title"])
        self.assertTrue(all(r["state"] == "reference" for r in reference))

    def test_before_a_run_the_flow_is_an_estimate_not_a_guess_about_items(self):
        rows = build_flow(VALUES, "workiq", [], None)
        reference = [r for r in rows if r.get("equivalent")]
        self.assertIn("at least", [r for r in rows if r.get("section")][1]["detail"])
        self.assertIn("GET <item>/{id}", [r["label"] for r in reference])
        self.assertNotIn("sources:", json.dumps([r["args"] for r in reference if r["label"].startswith("GET <item>")]))

    def test_no_citations_means_no_invented_graph_calls(self):
        rows = graph_reference(VALUES, {"content": [{"type": "text", "text": "No links here."}]})
        labels = [r["label"] for r in rows]
        self.assertIn("(no citations found)", labels)
        self.assertFalse([label for label in labels if label.startswith("GET /me/events") or "/shares/" in label])

    def test_writes_map_one_to_one_and_actions_skip_the_calendar_search(self):
        actions = Inputs("actions", "Project Falcon", VALUES.start, VALUES.end)
        labels = [r["label"] for r in graph_reference(actions, None)]
        self.assertNotIn("GET /me/calendarView", labels)
        self.assertIn("POST /planner/tasks", labels)
        self.assertIn("POST /me/events", labels)
        recap = Inputs("recap", "Project Falcon", VALUES.start, VALUES.end)
        self.assertIn("Mail.Send", [s for r in graph_reference(recap, None) for s in r["scopes"]])


if __name__ == "__main__":
    unittest.main()
