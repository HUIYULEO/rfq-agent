"""Context, pagination and tool-loop regression checks, without model/network calls."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rfq_agent.llm.genesis import Genesis, GenesisError, MAX_ROUNDS, Session, dispatch
from rfq_agent.llm.task_context import (MAX_RESULT_CHARS, encode, overview, page_rows,
                                       review_detail, tool_message)


def task_fixture():
    reviews = [{"id": f"RI-{i}", "status": "open", "scope": "line", "severity": "blocking",
                "kinds": ["moq_exceeds", "condition_mismatch"],
                "target": {"line_number": i // 2 + 1}, "question": "需人工确认",
                "options": [{"id": "request_quotation", "label": "转人工询价"}],
                "evidence": [{"rule": "moq_exceeds", "detail": "MOQ 50 > 需求 10"}],
                "locator": {"file": "input.eml", "row": i + 2}, "answer": None}
               for i in range(22)]
    return {"task_id": "task-a", "revision": 1, "status": "awaiting_review",
            "rfq": {"lines": [], "requirements": []}, "results": [], "review_items": reviews,
            "interruptions": [{"id": "INT-" + r["id"], "status": "pending",
                               "gate": "pricing_exception", "review_item_id": r["id"],
                               "question": r["question"], "evidence": r["evidence"],
                               "options": r["options"], "revision": 1} for r in reviews]}


def call(name="get_task_overview", **args):
    return {"id": "call-1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"task_id": "task-a"})}}


class ContextTests(unittest.TestCase):
    def test_overview_deduplicates_gates_and_counts_unique_lines(self):
        task = task_fixture()
        result = overview(task)
        self.assertEqual(result["open_review_count"], 22)
        self.assertEqual(result["pending_gate_counts"], {"pricing_exception": 22})
        self.assertEqual(len(result["review_groups"]), 2)
        for group in result["review_groups"]:
            self.assertEqual(group["review_count"], 22)
            self.assertEqual(group["affected_line_count"], 11)
            self.assertEqual(len(group["example_lines"]), 3)
        self.assertNotIn("interruptions", result)
        self.assertNotIn("review_items", result)
        self.assertLess(len(encode(result)), 3000)

    def test_resolved_reviews_not_counted_and_single_detail_keeps_options(self):
        task = task_fixture()
        task["review_items"][0]["status"] = "resolved"
        self.assertEqual(overview(task)["open_review_count"], 21)
        detail = review_detail(task, "RI-0")
        self.assertEqual(detail["options"], task["review_items"][0]["options"])
        self.assertEqual(detail["evidence"]["items"], task["review_items"][0]["evidence"])
        self.assertNotIn("options", detail["gates"][0])
        with self.assertRaises(ValueError):
            review_detail(task, "missing")

    def test_issue_filter_and_pagination_cover_all_records(self):
        service = Mock()
        service.get_task_result.return_value = task_fixture()
        all_ids, cursor = [], 0
        while cursor is not None:
            result = dispatch(service, Path('.'), "list_task_issues",
                              {"task_id": "task-a", "kind": "moq_exceeds", "cursor": cursor, "limit": 7})
            all_ids.extend(r["id"] for r in result["items"])
            cursor = result["next_cursor"]
        self.assertEqual(all_ids, [f"RI-{i}" for i in range(22)])
        self.assertEqual(dispatch(service, Path('.'), "list_task_issues",
                                 {"task_id": "task-a", "kind": "absent"})["total"], 0)

    def test_size_pagination_returns_complete_records_and_real_cursor(self):
        rows = [{"id": i, "text": "长" * 4000} for i in range(8)]
        result = page_rows(rows, 0, 20)
        self.assertLessEqual(len(encode(result)), MAX_RESULT_CHARS)
        self.assertEqual(result["next_cursor"], len(result["items"]))
        self.assertEqual(result["items"], rows[:result["next_cursor"]])
        self.assertEqual(page_rows(rows, len(rows))["items"], [])
        with self.assertRaises(ValueError):
            page_rows(rows, -1)

    def test_oversize_single_record_is_explicit_valid_json_error(self):
        result = json.loads(tool_message({"evidence": "证据" * MAX_RESULT_CHARS}, "explain_line"))
        self.assertEqual(result["error"], "tool_result_too_large")
        self.assertNotIn("evidence", result)


class LoopTests(unittest.TestCase):
    def session(self):
        service = Mock()
        service.store.get_desk.return_value = {"revision": 1}
        session = Session(service, Path('.'), {})
        session.client = Mock()
        session._state = Mock(return_value=None)
        return session

    @patch("rfq_agent.llm.genesis.dispatch", return_value={"revision": 1, "open_review_count": 22})
    def test_overview_then_answer_takes_one_tool_call(self, routed):
        session = self.session()
        session.client.chat.side_effect = [{"tool_calls": [call()]}, {"content": "22 项待复核"}]
        result = session.send("这单有什么问题", "task-a")
        self.assertEqual(routed.call_count, 1)
        self.assertEqual(result["reply"], "22 项待复核")
        self.assertEqual(len(result["trace"]), 1)

    @patch("rfq_agent.llm.genesis.dispatch", return_value={"revision": 1})
    def test_repeat_read_is_reused_then_forced_to_summarize(self, routed):
        session = self.session()
        session.client.chat.side_effect = [{"tool_calls": [call()]}, {"tool_calls": [call()]},
                                           {"content": "根据已读取的概况回答"}]
        result = session.send("这单有什么问题", "task-a")
        self.assertEqual(routed.call_count, 1)
        self.assertTrue(result["trace"][1]["cached"])
        self.assertEqual(session.client.chat.call_args.args[1], [])

    @patch("rfq_agent.llm.genesis.dispatch", return_value={"revision": 2})
    def test_changed_revision_does_not_reuse_old_evidence(self, routed):
        session = self.session()
        session.service.store.get_desk.side_effect = [{"revision": 1}, {"revision": 2}]
        session.client.chat.side_effect = [{"tool_calls": [call()]}, {"tool_calls": [call()]},
                                           {"content": "更新后的状态"}]
        result = session.send("检查", "task-a")
        self.assertEqual(routed.call_count, 2)
        self.assertFalse(result["trace"][1]["cached"])

    @patch("rfq_agent.llm.genesis.dispatch", return_value={"revision": 1})
    def test_sixth_round_still_gets_a_summary_chance(self, routed):
        session = self.session()
        session.client.chat.side_effect = [
            {"tool_calls": [call("explain_line", task_id="task-a", line_number=i)]}
            for i in range(MAX_ROUNDS)] + [{"content": "有限证据总结"}]
        self.assertEqual(session.send("检查", "task-a")["reply"], "有限证据总结")
        self.assertEqual(routed.call_count, MAX_ROUNDS)
        self.assertEqual(session.client.chat.call_args.args[1], [])

    @patch("rfq_agent.llm.genesis.dispatch", return_value={"revision": 1})
    def test_upstream_failure_preserves_trace_and_next_turn_discards_tool_payload(self, routed):
        session = self.session()
        session.client.chat.side_effect = [{"tool_calls": [call()]}, GenesisError("模型暂不可用")]
        with self.assertRaises(GenesisError) as caught:
            session.send("检查", "task-a")
        self.assertEqual(caught.exception.trace[0]["tool"], "get_task_overview")
        self.assertEqual(session.last_trace, caught.exception.trace)
        session.client.chat.side_effect = [{"content": "需要重新查询"}]
        session.send("再看看", "task-a")
        self.assertFalse(any(m["role"] == "tool" or m.get("tool_calls") for m in session.messages))

    def test_switch_task_clears_old_conversation(self):
        session = self.session()
        session.client.chat.side_effect = [{"content": "任务 A"}, {"content": "任务 B"}]
        session.send("A 的问题", "task-a")
        session.send("B 的问题", "task-b")
        self.assertEqual(session.task_id, "task-b")
        self.assertNotIn("A 的问题", encode(session.messages))

    @patch("rfq_agent.llm.genesis.dispatch")
    def test_malformed_arguments_and_empty_final_answer_preserve_failures(self, routed):
        session = self.session()
        bad_call = call()
        bad_call["function"]["arguments"] = '[]'
        session.client.chat.side_effect = [{"tool_calls": [bad_call]}, {"content": ""}]
        with self.assertRaises(GenesisError) as caught:
            session.send("检查", "task-a")
        routed.assert_not_called()
        self.assertFalse(caught.exception.trace[0]["ok"])

    def test_final_model_request_explicitly_disables_tools(self):
        client = Genesis({"GENESIS_BASE_URL": "https://example.test/v1", "GENESIS_MODEL": "test",
                          "GENESIS_ACCESS_TOKEN": "test"})
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"done"}}]}'
            client.chat([{"role": "user", "content": "answer"}], [])
            payload = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(payload["tool_choice"], "none")
            self.assertNotIn("tools", payload)


if __name__ == "__main__":
    unittest.main()
