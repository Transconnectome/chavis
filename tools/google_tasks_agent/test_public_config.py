"""Offline coverage for explicit account setup and preserving private config."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load_module("google_tasks_public_config_agent", "agent.py")
installer = load_module("google_tasks_public_config_installer", "install.py")


class PublicConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="google-tasks-config-test-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "isolated-home"
        # Installation helpers write only to this fake home and never call a service.
        guard = patch.object(installer.subprocess, "run", side_effect=AssertionError("external command forbidden in tests"))
        guard.start()
        self.addCleanup(guard.stop)
        node = patch.object(installer.shutil, "which", return_value="/usr/bin/node")
        node.start()
        self.addCleanup(node.stop)

    def test_missing_account_config_is_rejected(self):
        config = self.home / "config.json"
        with self.assertRaisesRegex(ValueError, "invalid_configuration"):
            agent.config(config)
        self.home.mkdir()
        config.write_text("{}")
        with self.assertRaisesRegex(ValueError, "invalid_configuration"):
            agent.config(config)

    def test_explicit_synthetic_account_config_is_accepted(self):
        self.home.mkdir()
        config = self.home / "config.json"
        config.write_text(json.dumps({"account": "reminder-test@example.com"}))
        self.assertEqual(agent.config(config)["account"], "reminder-test@example.com")

    def test_first_install_without_account_leaves_no_files(self):
        with self.assertRaisesRegex(ValueError, "account_required"):
            installer.install(self.home)
        self.assertFalse(self.home.exists())

    def test_first_install_account_and_existing_config_are_preserved(self):
        first = installer.install(self.home, account="reminder-test@example.com")
        self.assertTrue(first["config_created"])
        config = installer.config_location(self.home)
        self.assertEqual(agent.config(config)["account"], "reminder-test@example.com")
        original = config.read_bytes()
        second = installer.install(self.home, account="replacement@example.com")
        self.assertFalse(second["config_created"])
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(second["unit_changes"], [])


if __name__ == "__main__":
    unittest.main()
