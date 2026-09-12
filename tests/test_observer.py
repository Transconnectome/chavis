"""Privacy and incremental-consumption invariants for the local coach observer."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_coach.observer import collect_recent


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)

    def record(self, kind, payload, when=None):
        return {"type": kind, "timestamp": (when or self.now).isoformat(), "payload": payload}

    def meta(self, **values):
        return self.record("session_meta", {"id": "main", "cwd": "/project", "timestamp": self.now.isoformat(), **values})

    def message(self, role, text, *, event=False, when=None, **values):
        if event:
            return self.record("event_msg", {"type": "user_message" if role == "user" else "agent_message", "message": text, **values}, when)
        return self.record("response_item", {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **values}, when)

    def write(self, name, records):
        path = self.root / name
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def collect(self, state=None, **limits):
        return collect_recent([self.root], state or {}, now=self.now, **limits)

    def texts(self, result):
        return [m["text"] for session in result["sessions"] for m in session["messages"]]

    def test_only_public_messages_never_reasoning_tools_or_instructions(self):
        self.write("main.jsonl", [
            self.meta(base_instructions="PRIVATE_BASE"),
            self.message("system", "PRIVATE_SYSTEM"),
            self.message("developer", "PRIVATE_DEVELOPER"),
            self.message("user", "<recommended_plugins>PRIVATE_PLUGINS</recommended_plugins>\n# AGENTS.md instructions\n<INSTRUCTIONS>PRIVATE_RULES</INSTRUCTIONS>\n<environment_context>PRIVATE_ENV</environment_context>"),
            self.message("user", "Inspect the document"),
            self.record("response_item", {"type": "reasoning", "summary": "PRIVATE_REASONING"}),
            self.record("response_item", {"type": "function_call_output", "output": "PRIVATE_TOOL"}),
            self.message("assistant", "PRIVATE_ANALYSIS", phase="analysis"),
            self.message("assistant", "I will check the sources", phase="commentary"),
            self.message("assistant", "Sources checked", phase="final_answer"),
        ])
        result = self.collect()
        self.assertEqual(self.texts(result), ["Inspect the document", "I will check the sources", "Sources checked"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("Inspect the document", json.dumps(result["cursor"]))

    def test_event_mirror_is_returned_once(self):
        self.write("main.jsonl", [self.meta(), self.message("user", "check"), self.message("user", "check", event=True), self.message("assistant", "done"), self.message("assistant", "done", event=True)])
        self.assertEqual(self.texts(self.collect()), ["check", "done"])

    def test_incremental_continuation_and_partial_line_retry(self):
        path = self.write("main.jsonl", [self.meta(), self.message("user", "check")])
        partial = json.dumps(self.message("assistant", "finished")).encode()
        with path.open("ab") as handle:
            handle.write(partial[:50])
        first = self.collect()
        self.assertEqual(self.texts(first), ["check"])
        self.assertEqual(first["coverage"]["partial_lines"], 1)
        with path.open("ab") as handle:
            handle.write(partial[50:] + b"\n")
        second = self.collect(first)
        self.assertEqual(self.texts(second), ["finished"])
        self.assertEqual(self.texts(self.collect(second)), [])

    def test_caps_defer_unprocessed_records_and_sessions(self):
        first_path = self.write("a.jsonl", [self.meta(id="a"), self.message("user", "a"), self.message("assistant", "a result")])
        second_path = self.write("b.jsonl", [self.meta(id="b"), self.message("user", "b")])
        os.utime(first_path, (self.now.timestamp(), self.now.timestamp()))
        os.utime(second_path, (self.now.timestamp() - 1, self.now.timestamp() - 1))
        first = self.collect(max_sessions=1, max_messages=1)
        self.assertEqual(self.texts(first), ["a"])
        self.assertNotIn(str(second_path), first["cursor"])
        second = self.collect(first)
        self.assertEqual(self.texts(second), ["a result", "b"])

    def test_long_running_assistant_remains_eligible_after_user_window_expires(self):
        old = self.now - timedelta(hours=2)
        self.write("old.jsonl", [self.meta(timestamp=old.isoformat()), self.message("user", "old user", when=old), self.message("assistant", "recent assistant")])
        self.assertEqual(self.texts(self.collect(lookback_hours=0.5)), ["recent assistant"])

    def test_old_user_eligibility_survives_incremental_pass(self):
        old = self.now - timedelta(hours=2)
        path = self.write("old.jsonl", [self.meta(timestamp=old.isoformat()), self.message("user", "old user", when=old)])
        first = self.collect(lookback_hours=0.5)
        self.assertEqual(self.texts(first), [])
        with path.open("a") as handle:
            handle.write(json.dumps(self.message("assistant", "recent continuation")) + "\n")
        self.assertEqual(self.texts(self.collect(first, lookback_hours=0.5)), ["recent continuation"])

    def test_fork_copied_user_does_not_establish_eligibility_for_new_assistant(self):
        old = self.now - timedelta(hours=2)
        self.write("fork.jsonl", [self.meta(forked_from_id="parent"), self.message("user", "copied user", when=old), self.message("assistant", "no fresh user turn")])
        self.assertEqual(self.texts(self.collect(lookback_hours=0.5)), [])

    def test_same_session_copied_across_roots_is_not_observed_twice(self):
        records = [self.meta(), self.message("user", "check"), self.message("assistant", "done")]
        self.write("original.jsonl", records)
        archive = self.root / "archived"
        archive.mkdir()
        copy = archive / "copy.jsonl"
        copy.write_text("".join(json.dumps(record) + "\n" for record in records))
        first = self.collect()
        self.assertEqual(self.texts(first), ["check", "done"])
        self.assertEqual(first["coverage"]["duplicate_messages"], 2)
        copy2 = archive / "later-copy.jsonl"
        copy2.write_bytes(copy.read_bytes())
        second = self.collect(first)
        self.assertEqual(self.texts(second), [])
        self.assertEqual(second["coverage"]["duplicate_messages"], 2)

    def test_identical_requests_in_different_sessions_remain_distinct(self):
        self.write("a.jsonl", [self.meta(id="a"), self.message("user", "check")])
        self.write("b.jsonl", [self.meta(id="b"), self.message("user", "check")])
        self.assertEqual(self.texts(self.collect()), ["check", "check"])

    def test_fork_history_and_explicit_subagents_are_excluded(self):
        old = self.now - timedelta(minutes=10)
        self.write("fork.jsonl", [self.meta(id="fork", forked_from_id="parent"), self.message("user", "copied user", when=old), self.message("assistant", "copied assistant", when=old), self.message("user", "new fork request")])
        self.write("subagent.jsonl", [self.meta(id="worker", source={"subagent": {"thread_spawn": {"depth": 1}}}), self.message("user", "private subagent request")])
        self.assertEqual(self.texts(self.collect()), ["new fork request"])

    def test_internal_marker_only_from_public_user_prompt(self):
        self.write("internal.jsonl", [self.meta(), self.message("user", "CODEX_COACH_INTERNAL\nJudge the samples")])
        self.write("ordinary.jsonl", [self.meta(id="ordinary"), self.record("response_item", {"type": "function_call_output", "output": "CODEX_COACH_INTERNAL"}), self.message("user", "Explain the CODEX_COACH_INTERNAL marker")])
        self.assertEqual(self.texts(self.collect()), ["Explain the CODEX_COACH_INTERNAL marker"])

    def test_credentials_and_email_are_redacted(self):
        self.write("main.jsonl", [self.meta(), self.message("user", "Mail person@example.org; API_KEY=secret123; OPENAI_API_KEY=hidden123; Bearer abc.def.ghi; sk-proj-123456789abcdefgh; hf_" + "q" * 30 + "; 123456789:" + "a" * 35 + "; " + "z" * 55)])
        text = " ".join(self.texts(self.collect()))
        for secret in ("person@example.org", "secret123", "hidden123", "abc.def.ghi", "sk-proj-123456789abcdefgh", "a" * 35, "z" * 55, "q" * 30):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED_EMAIL]", text)

    def test_malformed_lines_and_oversized_tools_do_not_stall(self):
        path = self.write("main.jsonl", [self.meta(), self.message("user", "check")])
        with path.open("ab") as handle:
            handle.write(b'{broken}\n')
            handle.write(json.dumps(self.record("response_item", {"type": "function_call_output", "output": "x" * 900})).encode() + b"\n")
            handle.write(json.dumps(self.message("assistant", "finished")).encode() + b"\n")
        with patch("tools.codex_coach.observer.MAX_LINE_BYTES", 300), patch("tools.codex_coach.observer.MAX_FILE_BYTES", 700):
            state = {}
            texts = []
            for _ in range(8):
                state = self.collect(state)
                texts.extend(self.texts(state))
        self.assertEqual(texts, ["check", "finished"])
        self.assertEqual(state["cursor"][str(path)]["offset"], path.stat().st_size)

    def test_character_cap_reports_truncation_and_resumes_later_records(self):
        self.write("main.jsonl", [self.meta(), self.message("user", "abcdefgh"), self.message("assistant", "result")])
        first = self.collect(max_chars=3)
        self.assertEqual(self.texts(first), ["abc"])
        self.assertEqual(first["coverage"]["truncated_messages"], 1)
        self.assertEqual(self.texts(self.collect(first)), ["result"])

    def test_stale_files_missing_roots_and_source_immutability(self):
        path = self.write("old.jsonl", [self.meta(), self.message("user", "check")])
        contents = path.read_bytes()
        old = (self.now - timedelta(hours=4)).timestamp()
        os.utime(path, (old, old))
        result = collect_recent([self.root, self.root / "missing"], {}, now=self.now)
        self.assertEqual(result["coverage"]["candidate_files"], 0)
        self.assertEqual(len(result["coverage"]["missing_roots"]), 1)
        self.assertEqual(path.read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
