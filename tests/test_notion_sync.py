"""Reject fabricated receipts, ambiguous creates, duplicates, and partial readbacks."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_coach.digest_store import DigestStore
from tools.codex_coach import notion_sync as sync


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.digest = DigestStore(self.root).save({
            "kind": "daily", "title": "오늘의 실천", "period_start": "2026-09-12",
            "markdown": "## 근거부터\n\n원문과 p < 0.05를 확인한다.\n\n[출처](https://example.org/paper)",
        })
        self.config = {"data_source_id": "00000000-0000-4000-8000-000000000001",
                       "database_id": "00000000-0000-4000-8000-000000000002"}
        self.payload = sync._payload(self.digest, self.config)
        self.url = "https://www.notion.so/123456781234123412341234567890ab"

    def call(self, tool, args, obj, error=None):
        return {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "codex_apps",
                "tool": tool, "arguments": args, "status": "completed", "error": error,
                "result": {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}}}

    def events(self, existing=False):
        props = self.payload["create_arguments"]["pages"][0]["properties"]
        row = {key: props[key] for key in ("Name", "Record ID", "Digest hash")}
        row["url"] = self.url
        query = lambda rows: self.call("notion.query_data_sources", self.payload["query_arguments"],
                                     {"results": rows, "has_more": False})
        events = [query([row] if existing else [])]
        if not existing:
            events += [self.call("notion.notion_create_pages", self.payload["create_arguments"],
                                 {"pages": [{"url": self.url}]}), query([row])]
        text = ('<page>\n<parent-data-source url="' + self.payload["data_source_url"] + '"/>\n'
                '<properties>\n' + json.dumps(props, ensure_ascii=False) + '\n</properties>\n'
                '<content>\n' + self.digest["markdown"] + '\n</content>\n</page>')
        events += [self.call("notion.fetch", {"id": self.url},
                            {"url": self.url, "title": self.digest["title"], "text": text, "truncated": False})]
        final = {"status": "sent", "page_url": self.url, "record_id": self.digest["id"],
                 "content_hash": self.digest["content_hash"], "verified": True}
        events += [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(final)}}]
        return events

    def run_mock(self, events, returncode=0):
        output = '\n'.join(json.dumps(e, ensure_ascii=False) for e in events)
        process = subprocess.CompletedProcess([], returncode, stdout=output)
        with patch.object(sync.subprocess, "run", return_value=process) as run:
            receipt = sync.sync_via_codex(self.digest, self.root, self.config)
        return receipt, run

    def alter_result(self, event, mutate):
        block = event["item"]["result"]["content"][0]
        obj = json.loads(block["text"])
        mutate(obj)
        block["text"] = json.dumps(obj, ensure_ascii=False)

    def test_create_requires_native_query_and_full_fetch(self):
        receipt, run = self.run_mock(self.events())
        self.assertEqual(receipt["status"], "sent")
        self.assertTrue(receipt["verified"])
        self.assertTrue(receipt["created"])
        self.assertEqual(receipt["page_url"], self.url)
        command = run.call_args.args[0]
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(run.call_args.kwargs["timeout"], 360)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("UNTRUSTED_PAYLOAD", run.call_args.kwargs["input"])
        self.assertFalse(list(self.root.glob(".notion-sync-*")))

    def test_existing_page_reconciles_without_create(self):
        receipt, _ = self.run_mock(self.events(existing=True))
        self.assertEqual(receipt["status"], "sent")
        self.assertFalse(receipt["created"])

    def test_native_cli_hyphenated_tool_names_are_verified(self):
        events = self.events()
        for event in events:
            item = event["item"]
            if item.get("type") == "mcp_tool_call":
                item["tool"] = item["tool"].replace("_", "-")
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["status"], "sent")
        self.assertTrue(receipt["created"])

    def test_fabricated_final_is_not_evidence(self):
        receipt, _ = self.run_mock(self.events()[-1:])
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["verified"])

    def test_timeout_is_unknown_and_does_not_expose_provider_output(self):
        with patch.object(sync.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 360,
                          output="secret-token-value")):
            receipt = sync.sync_via_codex(self.digest, self.root, self.config)
        self.assertEqual(receipt["status"], "unknown")
        self.assertNotIn("secret-token-value", json.dumps(receipt))

    def test_changed_archived_content_never_starts_connector(self):
        self.digest["markdown"] += " modified"
        with patch.object(sync.subprocess, "run") as run:
            receipt = sync.sync_via_codex(self.digest, self.root, self.config)
        self.assertEqual(receipt["status"], "failed")
        run.assert_not_called()

    def test_duplicate_rows_do_not_report_success(self):
        events = self.events(existing=True)
        self.alter_result(events[0], lambda obj: obj["results"].append(obj["results"][0]))
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["reason"], "record_not_unique")
        self.assertEqual(receipt["status"], "failed")

    def test_partial_fetch_after_create_is_unknown(self):
        for bad in ({"truncated": True}, {"unknown_block_count": 1}, {"unknown_block_ids": ["hidden"]}):
            with self.subTest(bad=bad):
                events = self.events()
                self.alter_result(events[-2], lambda obj: obj.update(bad))
                receipt, _ = self.run_mock(events)
                self.assertEqual(receipt["status"], "unknown")
                self.assertEqual(receipt["reason"], "page_readback_mismatch")

    def test_omitted_body_tail_or_changed_number_fails_readback(self):
        for replacement in ("", "원문과 p > 0.05를 확인한다."):
            with self.subTest(replacement=replacement):
                events = self.events()
                self.alter_result(events[-2], lambda obj: obj.update(
                    text=obj["text"].replace("원문과 p < 0.05를 확인한다.", replacement)))
                receipt, _ = self.run_mock(events)
                self.assertEqual(receipt["reason"], "page_readback_mismatch")

    def test_wrong_parent_create_payload_is_unknown(self):
        events = self.events()
        # The mock must not mutate the independently reconstructed expected payload.
        events = json.loads(json.dumps(events))
        events[1]["item"]["arguments"]["parent"]["data_source_id"] = self.config["database_id"]
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["reason"], "creation_parent_mismatch")

    def test_non_notion_tool_invalidates_receipt(self):
        events = self.events()
        events.insert(0, self.call("gmail.send_email", {}, {"ok": True}))
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["reason"], "unexpected_tool_usage")

    def test_error_result_cannot_be_overridden_by_final_claim(self):
        events = self.events()
        events[1]["item"]["result"]["isError"] = True
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["status"], "unknown")
        self.assertFalse(receipt["verified"])

    def test_query_is_exact_and_complete(self):
        events = json.loads(json.dumps(self.events(existing=True)))
        events[0]["item"]["arguments"]["data"]["params"] = ["different"]
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["reason"], "query_not_exact_or_complete")

    def test_notion_cosmetic_markdown_normalization(self):
        self.assertEqual(sync._canonical_markdown("## Heading\n**Word** [label](https://x.org)"),
                         sync._canonical_markdown("## Heading\n**Word** [label]({{https://x.org}})\n<empty-block/>"))

    def test_notion_payload_omits_only_one_opening_h1_and_keeps_archive_hash(self):
        archived = DigestStore(self.root).save({"kind": "daily", "title": "Next day",
                   "domain": "학과 행정", "period_start": "2026-09-13",
                   "markdown": "# 2026-09-13 · Next day\n\nBody\n# Kept heading\nText"})
        payload = sync._payload(archived, self.config)
        self.assertEqual(payload["create_arguments"]["pages"][0]["content"], "Body\n# Kept heading\nText")
        self.assertEqual(json.loads(payload["create_arguments"]["pages"][0]["properties"]["분야"]), ["학과 행정"])
        self.assertEqual(payload["content_hash"], archived["content_hash"])
        self.assertTrue(archived["markdown"].startswith("# 2026-09-13"))

    def test_unrecognized_action_is_unknown_with_shape_only_diagnostics(self):
        events = [{"type": "item.completed", "item": {"type": "dynamic_tool_call", "tool": "notion.create_pages",
                   "arguments": {"secret": "secret-token"}, "result": {"private_body": "secret-token"}}}]
        receipt, _ = self.run_mock(events)
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["reason"], "unrecognized_action_event")
        self.assertNotIn("secret-token", json.dumps(receipt))
        self.assertEqual(receipt["diagnostics"]["event_shapes"][0]["item_type"], "dynamic_tool_call")


if __name__ == "__main__":
    unittest.main()
