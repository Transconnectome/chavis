"""Offline behavioral tests; every subprocess and notification is intercepted."""
from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


SPEC = importlib.util.spec_from_file_location("google_tasks_reminder_agent", Path(__file__).with_name("agent.py"))
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)
SEOUL = ZoneInfo("Asia/Seoul")


def at(value="2026-09-17 10:00"):
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SEOUL)


def source_task(task_id="one", title="확인할 작업", due="2026-09-17", **extra):
    row = {"id": task_id, "title": title, "status": "needsAction", **extra}
    if due:
        row["due"] = due + "T00:00:00.000Z"
    return row


def task_set(*rows):
    def reader(cfg, args):
        if args[:3] == ["tasks", "lists", "list"]:
            return {"tasklists": [{"id": "work", "title": "업무"}]}
        return {"tasks": list(rows)}
    return agent.fetch_tasks(agent.DEFAULTS, read=reader)[0]


class AgentCase(unittest.TestCase):
    def setUp(self):
        # Fail closed if a test accidentally reaches a real command or sender.
        self.subprocess_guard = patch.object(agent.subprocess, "run", side_effect=AssertionError("external command forbidden in tests"))
        self.subprocess_guard.start()
        self.addCleanup(self.subprocess_guard.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="google-tasks-test-")
        self.addCleanup(self.directory.cleanup)
        self.cfg = agent.DEFAULTS | {"state_dir": self.directory.name, "digest_times": []}
        self.db = agent.connect(self.directory.name)
        self.addCleanup(lambda: self.db.close())
        self.sender = Mock(return_value={"message_id": "test-only-1"})

    def tick(self, tasks, now=None, sender=None, **kwargs):
        return agent.tick(self.db, self.cfg, now or at(), fetch=lambda cfg: (tasks, 1),
                          sender=sender or self.sender, **kwargs)

    def persisted_tasks(self):
        return {row["key"]: json.loads(row["payload"]) for row in self.db.execute("SELECT * FROM tasks")}

    def queued(self):
        return {row["key"] for row in self.db.execute("SELECT * FROM changes")}

    def restart(self):
        self.db.close()
        self.db = agent.connect(self.directory.name)


class GoogleReadTests(AgentCase):
    def test_every_list_all_pages_assigned_tasks_and_list_scoped_ids(self):
        calls = []
        def reader(cfg, args):
            calls.append(args)
            if args[:3] == ["tasks", "lists", "list"]:
                return {"tasklists": [{"id": "work", "title": "업무"}, {"id": "home", "title": "개인"}]}
            return {"tasks": [source_task("same-id", title=args[2])]}
        tasks, count = agent.fetch_tasks(self.cfg, read=reader)
        self.assertEqual(count, 2)
        self.assertEqual(set(tasks), {"work/same-id", "home/same-id"})
        self.assertEqual({task["title"] for task in tasks.values()}, {"work", "home"})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("--all" in args for args in calls), calls)
        self.assertTrue(all("--show-assigned" in args for args in calls[1:]), calls)

    def test_page_token_on_lists_or_tasks_is_rejected(self):
        for level in ("lists", "tasks"):
            with self.subTest(level=level):
                def reader(cfg, args):
                    if args[:3] == ["tasks", "lists", "list"]:
                        result = {"tasklists": [{"id": "work"}]}
                        if level == "lists":
                            result["nextPageToken"] = "page-not-read"
                        return result
                    return {"tasks": [source_task()], "nextPageToken": "page-not-read"}
                with self.assertRaises(agent.AgentError):
                    agent.fetch_tasks(self.cfg, read=reader)

    def test_failure_after_first_list_preserves_entire_previous_snapshot(self):
        original = task_set(source_task("previous"))
        self.tick(original, at("2026-09-17 09:00"))
        def reader(cfg, args):
            if args[:3] == ["tasks", "lists", "list"]:
                return {"tasklists": [{"id": "work"}, {"id": "home"}]}
            if args[2] == "work":
                return {"tasks": [source_task("replacement")]}
            raise agent.AgentError("google_read_failed")
        outcome = agent.tick(self.db, self.cfg, at(),
                             fetch=lambda cfg: agent.fetch_tasks(cfg, read=reader), sender=self.sender)
        self.assertEqual(outcome["status"], "source_failed")
        self.assertEqual(self.persisted_tasks(), original)
        self.assertEqual(agent.get_meta(self.db, "last_success"), at("2026-09-17 09:00").isoformat(timespec="seconds"))
        self.sender.assert_not_called()

    def test_malformed_empty_task_collection_cannot_erase_snapshot(self):
        original = task_set(source_task())
        self.tick(original)
        def reader(cfg, args):
            return {"tasklists": [{"id": "work"}]} if args[:3] == ["tasks", "lists", "list"] else {"tasks": {}}
        outcome = agent.tick(self.db, self.cfg, at("2026-09-17 10:05"),
                             fetch=lambda cfg: agent.fetch_tasks(cfg, read=reader), sender=self.sender)
        self.assertEqual(outcome["status"], "source_failed")
        self.assertEqual(self.persisted_tasks(), original)

    def test_completed_deleted_hidden_are_never_reminder_candidates(self):
        tasks = task_set(source_task("active"), source_task("done", status="completed"),
                         source_task("deleted", deleted=True), source_task("hidden", hidden=True))
        self.assertEqual(set(tasks), {"work/active"})

    def test_invalid_due_rejects_whole_fetch(self):
        with self.assertRaises(agent.AgentError):
            task_set(source_task("valid"), source_task("bad", due="2026-02-30"))


class ScheduleTests(AgentCase):
    def test_quiet_hours_boundaries_in_seoul(self):
        for clock, expected in (("07:59", True), ("08:00", False), ("21:59", False), ("22:00", True), ("00:00", True)):
            with self.subTest(clock=clock):
                self.assertEqual(agent.is_quiet(self.cfg, at("2026-09-17 " + clock)), expected)

    def test_bootstrap_records_existing_backlog_without_change_storm(self):
        tasks = task_set(*(source_task(str(i), due="2025-01-01") for i in range(60)))
        self.tick(tasks)
        self.assertEqual(len(self.persisted_tasks()), 60)
        self.assertEqual(self.queued(), set())
        self.sender.assert_not_called()

    def test_daily_slots_are_deduplicated_across_restart(self):
        self.cfg["digest_times"] = ["08:30", "17:30"]
        tasks = task_set(source_task())
        self.tick(tasks, at("2026-09-17 08:29"))
        self.sender.assert_not_called()
        self.tick(tasks, at("2026-09-17 08:30"))
        self.restart()
        self.tick(tasks, at("2026-09-17 08:35"))
        self.tick(tasks, at("2026-09-17 17:30"))
        self.tick(tasks, at("2026-09-17 17:35"))
        self.assertEqual(self.sender.call_count, 2)
        self.tick(tasks, at("2026-09-18 08:30"))
        self.assertEqual(self.sender.call_count, 3)

    def test_downtime_catches_up_only_latest_digest(self):
        self.cfg["digest_times"] = ["08:30", "17:30"]
        self.tick(task_set(source_task()), at("2026-09-17 19:00"))
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual([row[0] for row in self.db.execute("SELECT key FROM deliveries")], ["digest:2026-09-17:17:30"])

    def test_quiet_hours_continue_snapshot_and_queue_until_morning(self):
        self.tick({}, at("2026-09-17 21:55"))
        tasks = task_set(source_task())
        result = self.tick(tasks, at("2026-09-17 22:05"))
        self.assertEqual(result["status"], "quiet_hours")
        self.assertEqual(self.persisted_tasks(), tasks)
        self.assertEqual(self.queued(), {"work/one"})
        self.sender.assert_not_called()
        self.restart()
        self.tick(tasks, at("2026-09-18 07:59"))
        self.sender.assert_not_called()
        self.tick(tasks, at("2026-09-18 08:00"))
        self.assertEqual(self.sender.call_count, 1)
        self.assertIn("확인할 작업", self.sender.call_args.args[1])
        self.assertEqual(self.queued(), set())

    def test_completed_or_postponed_queued_changes_are_cancelled(self):
        self.tick({}, at("2026-09-17 21:55"))
        tasks = task_set(source_task("completed-next"), source_task("postponed"))
        self.tick(tasks, at("2026-09-17 22:05"))
        self.assertEqual(len(self.queued()), 2)
        self.tick(task_set(source_task("postponed", due="2026-09-25")), at("2026-09-18 08:00"))
        self.assertEqual(self.queued(), set())
        self.sender.assert_not_called()

    def test_second_change_in_sent_hour_waits_for_next_hour(self):
        self.tick({}, at("2026-09-17 09:55"))
        first = task_set(source_task("first", title="first task"))
        both = task_set(source_task("first", title="first task"), source_task("second", title="second task"))
        self.tick(first, at("2026-09-17 10:00"))
        self.tick(both, at("2026-09-17 10:05"))
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.queued(), {"work/second"})
        self.restart()
        self.tick(both, at("2026-09-17 11:00"))
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("second task", self.sender.call_args.args[1])
        self.assertEqual(self.queued(), set())

    def test_google_date_is_not_converted_to_utc_day(self):
        self.tick({}, at("2026-09-16 23:55"))
        today = task_set(source_task("today", due="2026-09-17"), source_task("tomorrow", due="2026-09-18"))
        self.tick(today, at("2026-09-17 00:05"))
        self.assertEqual(self.queued(), {"work/today"})


class DeliveryAndHealthTests(AgentCase):
    def test_unknown_send_is_not_retried_even_after_restart(self):
        sender = Mock(side_effect=agent.AgentError("telegram_unknown_timeout"))
        self.tick({}, at("2026-09-17 09:55"))
        tasks = task_set(source_task())
        self.tick(tasks, at(), sender=sender)
        self.restart()
        self.tick(tasks, at("2026-09-17 10:05"), sender=sender)
        self.tick(tasks, at("2026-09-17 11:05"), sender=sender)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.db.execute("SELECT status FROM deliveries").fetchone()[0], "unknown")

    def test_interrupted_send_intent_becomes_unknown_and_is_not_retried(self):
        self.cfg["digest_times"] = ["08:30"]
        self.db.execute("INSERT INTO deliveries (key, kind, status, created, updated, receipt, error) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                        ("digest:2026-09-17:08:30", "digest", "sending", agent.stamp(at()), agent.stamp(at())))
        self.db.commit()
        self.restart()
        self.tick(task_set(source_task()), at("2026-09-17 10:05"))
        self.sender.assert_not_called()
        row = self.db.execute("SELECT * FROM deliveries").fetchone()
        self.assertEqual((row["status"], row["error"]), ("unknown", "interrupted_send"))

    def test_unknown_digest_does_not_fall_through_to_duplicate_changes(self):
        self.cfg["digest_times"] = ["08:30"]
        self.tick({}, at("2026-09-17 08:00"))
        sender = Mock(side_effect=agent.AgentError("telegram_unknown_timeout"))
        tasks = task_set(source_task())
        self.tick(tasks, at("2026-09-17 08:30"), sender=sender)
        self.assertEqual(sender.call_count, 1)
        self.restart()
        self.tick(tasks, at("2026-09-17 09:30"), sender=sender)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.queued(), set())

    def test_crash_during_changes_send_does_not_resend_members_next_hour(self):
        self.tick({}, at("2026-09-17 09:55"))
        tasks = task_set(source_task())
        crash = Mock(side_effect=SystemExit("simulated process death after send intent"))
        with self.assertRaises(SystemExit):
            self.tick(tasks, at(), sender=crash)
        self.restart()
        self.tick(tasks, at("2026-09-17 10:05"))
        self.tick(tasks, at("2026-09-17 11:05"))
        self.sender.assert_not_called()
        self.assertEqual(self.queued(), set())
        row = self.db.execute("SELECT status, error FROM deliveries").fetchone()
        self.assertEqual(tuple(row), ("unknown", "interrupted_send"))

    def test_summary_cap_preserves_omitted_changes_for_later_delivery(self):
        self.tick({}, at("2026-09-17 09:55"))
        titles = {f"unique task {i:02}" for i in range(9)}
        tasks = task_set(*(source_task(str(i), title=f"unique task {i:02}") for i in range(9)))
        self.tick(tasks, at())
        self.assertEqual(self.sender.call_count, 1)
        first_message = self.sender.call_args.args[1]
        shown = {title for title in titles if title in first_message}
        self.assertGreater(len(shown), 0)
        self.assertLess(len(shown), len(titles))
        queued_titles = {tasks[key]["title"] for key in self.queued()}
        self.assertEqual(queued_titles, titles - shown)
        self.restart()
        self.tick(tasks, at("2026-09-17 11:00"))
        all_messages = "\n".join(call.args[1] for call in self.sender.call_args_list)
        self.assertTrue(all(title in all_messages for title in titles))
        self.assertEqual(self.queued(), set())

    def test_recent_source_success_does_not_hide_delivery_failure(self):
        self.cfg["digest_times"] = ["08:30"]
        sender = Mock(side_effect=agent.AgentError("telegram_route_unavailable"))
        self.tick(task_set(source_task()), at(), sender=sender)
        report = agent.status(self.db, at())
        self.assertEqual(report["snapshot_age_seconds"], 0)
        self.assertNotEqual(report["status"], "healthy")
        self.assertEqual(report["deliveries"][0]["status"], "failed")

    def test_known_pre_send_failure_retries_after_backoff(self):
        self.cfg["digest_times"] = ["08:30"]
        sender = Mock(side_effect=[agent.AgentError("telegram_route_unavailable"), {"message_id": "retry-receipt"}])
        tasks = task_set(source_task())
        self.tick(tasks, at(), sender=sender)
        self.tick(tasks, at("2026-09-17 10:14"), sender=sender)
        self.assertEqual(sender.call_count, 1)
        self.restart()
        self.tick(tasks, at("2026-09-17 10:15"), sender=sender)
        self.tick(tasks, at("2026-09-17 10:20"), sender=sender)
        self.assertEqual(sender.call_count, 2)
        row = self.db.execute("SELECT * FROM deliveries").fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(json.loads(row["receipt"])["message_id"], "retry-receipt")

    def test_consecutive_source_failures_notify_once_daily_and_preserve_snapshot(self):
        original = task_set(source_task())
        self.tick(original, at("2026-09-17 09:55"))
        fetch = Mock(side_effect=agent.AgentError("google_read_failed"))
        for minute in (0, 5):
            agent.tick(self.db, self.cfg, at() + timedelta(minutes=minute), fetch=fetch, sender=self.sender)
        self.sender.assert_not_called()
        for minute in (10, 15, 20):
            agent.tick(self.db, self.cfg, at() + timedelta(minutes=minute), fetch=fetch, sender=self.sender)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.persisted_tasks(), original)
        self.assertEqual(agent.get_meta(self.db, "consecutive_failures"), 5)
        self.tick(original, at("2026-09-17 10:25"))
        self.assertEqual(agent.get_meta(self.db, "consecutive_failures"), 0)
        self.assertIsNone(agent.get_meta(self.db, "last_error"))

    def test_stale_status_detects_stopped_monitor_and_latest_fetch_failure(self):
        self.cfg["digest_times"] = ["08:30"]
        self.assertEqual(agent.status(self.db, at())["status"], "stale")
        self.tick(task_set(source_task()), at())
        self.assertEqual(agent.status(self.db, at() + timedelta(minutes=19))["status"], "healthy")
        self.assertEqual(agent.status(self.db, at() + timedelta(minutes=20))["status"], "stale")
        agent.tick(self.db, self.cfg, at() + timedelta(minutes=1),
                   fetch=Mock(side_effect=agent.AgentError("google_read_failed")), sender=self.sender)
        self.assertEqual(agent.status(self.db, at() + timedelta(minutes=1))["status"], "stale")

    def test_message_utf16_limit_including_astral_characters_and_source_link(self):
        rows = []
        for prefix, due, count in (("today", "2026-09-17", 8), ("late", "2026-09-16", 8),
                                    ("soon", "2026-09-18", 8), ("undated", "", 8)):
            rows.extend(source_task(f"{prefix}-{i}", title="🧑🏽‍🔬" * 100, due=due) for i in range(count))
        tasks = task_set(*rows)
        for task in list(tasks.values())[-3:]:
            task["list_title"] = "High Priority"
        message = agent.summary(tasks, self.cfg, at())
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
        self.assertTrue(message.endswith("https://tasks.google.com/"))
        self.assertNotIn("\ufffd", message)
        self.assertIn("미완료 32", message)


if __name__ == "__main__":
    unittest.main()
