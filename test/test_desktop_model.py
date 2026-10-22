import unittest

from desktop_model import (
    SCENARIOS,
    Inputs,
    RunState,
    check_work_result,
    collect_graph,
    graph_url,
    operations_for,
    planned_flow,
    planned_writes,
    prompt_for,
    utc_time,
)

VALUES = Inputs("briefing", "Contoso", "2026-10-19T00:00:00Z", "2026-10-26T00:00:00Z")
ACTION = {
    "kind": "actions", "planId": "real-plan", "bucketId": "real-bucket",
    "titles": ["Confirm budget", "Review launch"], "subject": "Follow-up",
    "start": "2026-10-23T10:00:00+09:00", "end": "2026-10-23T10:30:00+09:00",
}


class WorkflowTests(unittest.TestCase):
    def test_japanese_option_asks_for_a_japanese_answer_and_changes_the_inputs(self):
        english = prompt_for(VALUES)
        japanese_values = Inputs.from_dates("briefing", "Contoso", "2026-10-19", "2026-10-26", japanese=True)
        self.assertNotIn("Japanese", english)
        self.assertIn("Japanese", prompt_for(japanese_values))
        self.assertIn("日本語", prompt_for(japanese_values))
        self.assertFalse(Inputs.from_dates("briefing", "Contoso", "2026-10-19", "2026-10-26").japanese)
        self.assertNotEqual(japanese_values, Inputs.from_dates("briefing", "Contoso", "2026-10-19", "2026-10-26"))

    def test_utc_parser_accepts_z_and_numeric_offsets(self):
        expected = utc_time("2026-10-20T01:00:00Z")
        self.assertEqual(expected, utc_time("2026-10-20T01:00:00+00:00"))
        self.assertEqual(expected, utc_time("2026-10-20T10:00:00+09:00"))

    def test_invalid_graph_collection_has_an_explicit_type_error(self):
        for response in (None, [], {}, {"value": "not a list"}):
            with self.subTest(response=response), self.assertRaisesRegex(TypeError, "Graph did not return a collection"):
                collect_graph(VALUES, lambda *_args, value=response: value, lambda _event: None)

    def test_scope_counts_and_plans(self):
        self.assertEqual([len(set(read + write)) for _, read, write in SCENARIOS.values()], [3, 4, 4])
        for scenario, reads, writes in (("briefing", 1, 0), ("recap", 1, 2), ("actions", 1, 5)):
            values = Inputs(scenario, VALUES.customer, VALUES.start, VALUES.end)
            self.assertEqual(len(planned_flow(values, "workiq")), reads)
            self.assertEqual(len(planned_writes(values, "workiq")), writes)
            self.assertTrue(all(step["state"] == "planned" for step in planned_flow(values, "graph") + planned_writes(values, "graph")))
            self.assertFalse(any(s["label"].startswith(("POST", "do_action", "create_entity")) for s in planned_flow(values, "graph") + planned_flow(values, "workiq")))
            self.assertIn("Do not perform any writes", prompt_for(values))

    def test_input_validation(self):
        for customer, start, end in (
            ("", VALUES.start, VALUES.end), ("Contoso", VALUES.end, VALUES.start),
            ("Contoso", VALUES.start, "2027-01-01T00:00:00Z"),
            ("Contoso", "2026-10-19T00:00:00", VALUES.end),
        ):
            with self.assertRaises(ValueError):
                Inputs("briefing", customer, start, end)
        with self.assertRaises(ValueError):
            Inputs.from_dates("briefing", "Contoso", "wrong", "2026-10-26")

    def test_graph_continuation_link_validation(self):
        self.assertEqual(graph_url("/me/messages"), "https://graph.microsoft.com/v1.0/me/messages")
        self.assertEqual(graph_url("https://graph.microsoft.com/v1.0/me/messages?$skip=25"),
                         "https://graph.microsoft.com/v1.0/me/messages?$skip=25")
        for path in ("https://example.com/token", "//evil.com", "/../beta/users", "/%2e%2e/beta/users", "/\\evil", "/me\r\nx"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                graph_url(path)

    def test_graph_retrieval_dates_sources_and_limits(self):
        paths = []
        events = []

        def call(method, path, **kwargs):
            paths.append(path)
            if path.startswith("/me/messages"):
                return {"value": [
                    {"receivedDateTime": "2026-10-20T09:00:00Z", "subject": "In range"},
                    {"receivedDateTime": "2020-01-01T00:00:00Z", "subject": "Old"},
                ]}
            if "calendarView" in path:
                return {"value": [{"subject": "Contoso review"}, {"subject": "Other meeting"}]}
            if "search(" in path:
                return {"value": [{"id": "opaque=id", "name": "Notes.txt"}, {"id": "word", "name": "Notes.docx"}]}
            self.assertTrue(kwargs["text"])
            return "Alex will review the launch."

        evidence = collect_graph(VALUES, call, events.append)
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(evidence["messages"]), 1)
        self.assertEqual(len(evidence["meetings"]), 1)
        self.assertEqual(len(evidence["notes"]), 1)
        self.assertIn("opaque%3Did", paths[-1])
        self.assertTrue(any("Transcripts are excluded" in limit for limit in evidence["limits"]))
        self.assertEqual(events[-1]["type"], "evidence")

    def test_graph_paging_is_bounded(self):
        paths = []

        def call(method, path):
            paths.append(path)
            if "search(" in path:
                return {"value": []}
            return {"value": [{"receivedDateTime": VALUES.start}], "@odata.nextLink": "/me/messages?next=1"}

        evidence = collect_graph(Inputs("actions", "O'Brien & Co", VALUES.start, VALUES.end), call, lambda _: None)
        self.assertEqual(len(paths), 4)
        self.assertNotIn("calendarView", "".join(paths))
        self.assertIn("O%27%27Brien%20%26%20Co", paths[-1])
        self.assertTrue(any("partial" in item for item in evidence["limits"]))

    def test_mutation_payloads_and_local_time_conversion(self):
        for mode in ("graph", "workiq"):
            operations = operations_for(ACTION, mode)
            self.assertEqual(len(operations), 3)
            self.assertEqual(operations[0]["body"]["bucketId"], "real-bucket")
            event = operations[-1]["body"]
            self.assertEqual(event["start"], {"dateTime": "2026-10-23T01:00:00", "timeZone": "UTC"})
            self.assertNotIn("attendees", event)
        recap = {"kind": "recap", "recipient": "alex@example.com", "subject": "Recap", "body": "<plain text>"}
        self.assertIn("message", operations_for(recap, "graph")[0]["body"])
        self.assertIn("Message", operations_for(recap, "workiq")[0]["body"])

    def test_mutation_validation(self):
        for change in (
            {"planId": "../invalid"}, {"titles": []}, {"titles": ["Task"] * 11},
            {"end": ACTION["start"]}, {"titles": [""]}, {"subject": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                operations_for({**ACTION, **change}, "graph")

    def test_workiq_errors_and_uncertain_writes(self):
        for result in (
            {"content": [{"type": "text", "text": "You must accept the EULA first."}]},
            {"content": [{"type": "text", "text": '{"statusCode":403,"error":"Denied"}'}]},
            {"content": []},
        ):
            with self.assertRaises(RuntimeError):
                check_work_result(result)
        with self.assertRaisesRegex(RuntimeError, "indeterminate"):
            check_work_result({"content": [{"type": "text", "text": "Maybe done"}]}, mutation=True)
        check_work_result({"structuredContent": {"statusCode": 202}}, mutation=True)
        check_work_result({"structuredContent": {"id": "created-task"}}, mutation=True)

    def test_live_state_updates_by_call_id(self):
        state = RunState()
        for identifier in (1, 2):
            state.apply({"type": "step", "id": identifier, "service": "Graph", "label": "POST /planner/tasks", "args": {}})
        state.apply({"type": "step_done", "id": 1, "ms": 40, "status": 201})
        state.apply({"type": "step_error", "id": 2, "message": "Denied"})
        self.assertEqual(state.calls, 2)
        self.assertEqual([s["state"] for s in state.steps], ["done", "failed"])
        state.apply({"type": "read_complete"})
        self.assertTrue(state.ready)
        state.apply({"type": "error", "message": "Failed"})
        self.assertFalse(state.ready)


if __name__ == "__main__":
    unittest.main()
