"""Schedule, ownership, and upgrade invariants for the shared coach installer."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_coach import install


class DigestInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        for relative, text in (
            ("skills/codex-coach/SKILL.md", "A synthetic shared skill.\n"),
            ("tools/codex_coach/coach.py", "# synthetic observer runner\n"),
            ("tools/codex_coach/digest.py", "# synthetic digest runner\n"),
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.units = self.home / ".config/systemd/user"

    def install(self, **options):
        return install.install_files(self.home, self.repo, **options)

    def test_explicit_korean_schedules_and_current_runner_commands(self):
        files = install.installation_files(self.home, self.repo)
        expected = {
            "research": ("*-*-* 07:00:00 Asia/Seoul", "coach.py", "research", 480),
            "digest-prepare": ("*-*-* 07:20:00 Asia/Seoul", "digest.py", "prepare-daily", 1200),
            "digest-delivery": ("*-*-* 08:00:00 Asia/Seoul", "digest.py", "deliver-daily", 600),
            "digest-weekly": ("Sun *-*-* 18:00:00 Asia/Seoul", "digest.py", "weekly", 900),
        }
        for name, (calendar, runner, command, timeout) in expected.items():
            timer = files[self.units / f"codex-coach-{name}.timer"].decode()
            service = files[self.units / f"codex-coach-{name}.service"].decode()
            self.assertIn(f"OnCalendar={calendar}\n", timer)
            self.assertIn("Persistent=true\n", timer)
            self.assertIn(f"Unit=codex-coach-{name}.service\n", timer)
            self.assertIn(f'/{runner}" {command}\n', service)
            self.assertIn(f"TimeoutStartSec={timeout}\n", service)
            self.assertIn("UMask=0077\n", service)
        self.assertIn(b"OnUnitInactiveSec=120s\n", files[self.units / "codex-coach-monitor.timer"])
        self.assertEqual(
            set(install.TIMERS),
            {path.name for path in files if path.suffix == ".timer"},
        )

    def test_missing_digest_source_prevents_installation(self):
        (self.repo / "tools/codex_coach/digest.py").unlink()
        with self.assertRaisesRegex(ValueError, "coach_source_missing"):
            self.install()
        self.assertFalse(self.home.exists())

    def test_idempotent_install_and_read_only_verification(self):
        first = self.install()
        self.assertEqual(first["status"], "updated")
        mtimes = {path: path.stat().st_mtime_ns for path in install.installation_files(self.home, self.repo)}
        self.assertEqual(self.install(), {"status": "unchanged", "changed": []})
        self.assertEqual(self.install(check=True), {"status": "verified", "drift": []})
        self.assertEqual(mtimes, {path: path.stat().st_mtime_ns for path in mtimes})
        self.assertEqual(install.manifest_path(self.home).stat().st_mode & 0o777, 0o600)

    def test_upgrade_adds_owned_digest_units_and_preserves_unrelated_files(self):
        self.install()
        manifest_file = install.manifest_path(self.home)
        manifest = json.loads(manifest_file.read_text())
        for path in list(manifest["files"]):
            if "codex-coach-digest-" in path:
                Path(path).unlink()
                del manifest["files"][path]
        research = self.units / "codex-coach-research.timer"
        old = research.read_bytes().replace(b"07:00:00", b"09:10:00")
        research.write_bytes(old)
        manifest["files"][str(research)] = install.digest(old)
        manifest_file.write_text(json.dumps(manifest))
        unrelated = self.units / "unrelated.timer"
        unrelated.write_text("keep me\n")

        changed = self.install()["changed"]
        self.assertEqual(len(changed), 7)
        self.assertIn(str(research), changed)
        self.assertIn(b"07:00:00 Asia/Seoul", research.read_bytes())
        self.assertEqual(unrelated.read_text(), "keep me\n")
        self.assertEqual(self.install(check=True)["status"], "verified")

    def test_modified_owned_or_unowned_units_block_all_writes(self):
        self.install()
        timer = self.units / "codex-coach-digest-delivery.timer"
        timer.write_text("user modified timer\n")
        skill = self.repo / "skills/codex-coach/SKILL.md"
        previous_skill = (self.home / ".codex/skills/codex-coach/SKILL.md").read_bytes()
        skill.write_text("a source update that must not partly install\n")
        with self.assertRaisesRegex(ValueError, "owned_destination_modified"):
            self.install()
        self.assertEqual((self.home / ".codex/skills/codex-coach/SKILL.md").read_bytes(), previous_skill)
        self.assertEqual(timer.read_text(), "user modified timer\n")
        self.assertIn(str(timer), self.install(check=True)["drift"])

        manifest_file = install.manifest_path(self.home)
        manifest = json.loads(manifest_file.read_text())
        del manifest["files"][str(timer)]
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "unowned_destination_conflict"):
            self.install()

    def test_activation_only_touches_declared_timers_and_restarts_changed_active_schedule(self):
        active = {timer: {"enabled": True, "active": True} for timer in install.TIMERS}
        changed_timer = "codex-coach-research.timer"
        with patch.object(install, "timer_status", return_value=active), patch.object(install, "systemctl") as call:
            self.assertEqual(install.enable_timers([str(self.units / changed_timer)]), active)
        self.assertEqual(call.call_args_list[0].args, ("daemon-reload",))
        self.assertEqual(call.call_args_list[1].args, ("enable", "--now", *install.TIMERS))
        self.assertEqual(call.call_args_list[2].args, ("restart", changed_timer))
        self.assertEqual(call.call_count, 3)


if __name__ == "__main__":
    unittest.main()
