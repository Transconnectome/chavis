#!/usr/bin/env python3
"""Install the shared coach skills and its own user timers, with drift checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


OWNER = "codex-coach-installer-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMERS = (
    "codex-coach-monitor.timer",
    "codex-coach-research.timer",
    "codex-coach-digest-prepare.timer",
    "codex-coach-digest-delivery.timer",
    "codex-coach-digest-weekly.timer",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"symlink_denied: {component}")


def systemd_quote(value: str) -> str:
    """Quote a single systemd argument, including specifier expansion."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("invalid_unit_path")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def installation_files(home: Path, repo_root: Path = REPO_ROOT) -> dict[Path, bytes]:
    skill = repo_root / "skills/codex-coach/SKILL.md"
    coach = repo_root / "tools/codex_coach/coach.py"
    digest_runner = repo_root / "tools/codex_coach/digest.py"
    reject_symlinks(skill)
    reject_symlinks(coach)
    reject_symlinks(digest_runner)
    if not skill.is_file() or not coach.is_file() or not digest_runner.is_file():
        raise ValueError("coach_source_missing")
    source = skill.read_bytes()
    if not source.strip():
        raise ValueError("coach_skill_empty")
    unit_root = home / ".config/systemd/user"
    # WorkingDirectory takes one path value and does not remove shell quotes.
    directory = str(coach.parent).replace("%", "%%")
    path = ":".join(str(home / sub) for sub in (".local/bin", ".npm-global/bin"))
    path += ":/usr/local/bin:/usr/bin:/bin"
    files = {
        home / ".codex/skills/codex-coach/SKILL.md": source,
        home / ".openclaw/workspace/skills/codex-coach/SKILL.md": source,
        home / ".openclaw/skills/codex-coach/SKILL.md": source,
    }
    services = (
        ("monitor", coach, "tick", 480),
        ("research", coach, "research", 480),
        ("digest-prepare", digest_runner, "prepare-daily", 1200),
        ("digest-delivery", digest_runner, "deliver-daily", 600),
        ("digest-weekly", digest_runner, "weekly", 900),
    )
    for name, runner, command, timeout in services:
        text = (
            f"# Managed by {OWNER}.\n"
            "[Unit]\n"
            f"Description=Codex Coach {name}\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"WorkingDirectory={directory}\n"
            f"ExecStart=/usr/bin/python3 {systemd_quote(str(runner))} {command}\n"
            f"Environment={systemd_quote('PATH=' + path)}\n"
            f"TimeoutStartSec={timeout}\n"
            "UMask=0077\n"
        )
        files[unit_root / f"codex-coach-{name}.service"] = text.encode()
    files[unit_root / TIMERS[0]] = (
        f"# Managed by {OWNER}.\n"
        "[Unit]\nDescription=Observe Codex activity every two minutes\n\n"
        "[Timer]\nOnStartupSec=120s\nOnUnitInactiveSec=120s\n"
        "AccuracySec=5s\nUnit=codex-coach-monitor.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    ).encode()
    calendars = (
        ("research", "Refresh Codex Coach research daily", "*-*-* 07:00:00 Asia/Seoul"),
        ("digest-prepare", "Prepare daily agentic AI tip", "*-*-* 07:20:00 Asia/Seoul"),
        ("digest-delivery", "Deliver daily agentic AI tip", "*-*-* 08:00:00 Asia/Seoul"),
        ("digest-weekly", "Prepare and deliver weekly agentic AI summary", "Sun *-*-* 18:00:00 Asia/Seoul"),
    )
    for name, description, calendar in calendars:
        files[unit_root / f"codex-coach-{name}.timer"] = (
            f"# Managed by {OWNER}.\n"
            f"[Unit]\nDescription={description}\n\n"
            f"[Timer]\nOnCalendar={calendar}\n"
            f"Persistent=true\nAccuracySec=30s\nUnit=codex-coach-{name}.service\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ).encode()
    return files


def manifest_path(home: Path) -> Path:
    return home / ".local/state/codex-coach/install-manifest.json"


def read_manifest(home: Path) -> dict:
    path = manifest_path(home)
    reject_symlinks(path)
    if not path.exists():
        return {"owner": OWNER, "files": {}}
    if not path.is_file():
        raise ValueError("manifest_not_regular_file")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("owner") != OWNER or not isinstance(data.get("files"), dict):
        raise ValueError("manifest_owner_invalid")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data["files"].items()):
        raise ValueError("manifest_entries_invalid")
    return data


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        reject_symlinks(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_files(home: Path, repo_root: Path = REPO_ROOT, *, check: bool = False) -> dict:
    home = home.absolute()
    files = installation_files(home, repo_root)
    previous = read_manifest(home)
    modified: list[str] = []
    drift: list[str] = []
    # Preflight every destination before writing any of them.
    for path, data in files.items():
        reject_symlinks(path)
        if path.exists() and not path.is_file():
            raise ValueError(f"destination_not_regular_file: {path}")
        current = path.read_bytes() if path.exists() else None
        if check:
            if current != data or previous["files"].get(str(path)) != digest(data):
                drift.append(str(path))
            continue
        if current == data:
            continue  # Identical files are retained, without overwriting them.
        if current is not None:
            owned_hash = previous["files"].get(str(path))
            if owned_hash is None:
                raise ValueError(f"unowned_destination_conflict: {path}")
            if digest(current) != owned_hash:
                raise ValueError(f"owned_destination_modified: {path}")
        modified.append(str(path))
    if check:
        return {"status": "verified" if not drift else "drift", "drift": drift}
    state = manifest_path(home).parent
    reject_symlinks(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    for path, data in files.items():
        if str(path) in modified:
            atomic_write(path, data, 0o644)
    manifest = {"owner": OWNER, "files": {str(path): digest(data) for path, data in files.items()}}
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    target = manifest_path(home)
    if not target.exists() or target.read_bytes() != manifest_data:
        atomic_write(target, manifest_data, 0o600)
    else:
        os.chmod(target, 0o600)
    return {"status": "updated" if modified else "unchanged", "changed": modified}


def systemctl(*arguments: str, required: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=30
    )
    if required and result.returncode != 0:
        raise RuntimeError(f"systemctl_failed: {arguments[0]} (exit {result.returncode})")
    return result


def timer_status() -> dict:
    return {
        timer: {
            "enabled": systemctl("is-enabled", "--quiet", timer).returncode == 0,
            "active": systemctl("is-active", "--quiet", timer).returncode == 0,
        }
        for timer in TIMERS
    }


def enable_timers(changed: list[str]) -> dict:
    before = timer_status()
    systemctl("daemon-reload", required=True)
    systemctl("enable", "--now", *TIMERS, required=True)
    for timer in TIMERS:
        if before[timer]["active"] and any(Path(path).name == timer for path in changed):
            systemctl("restart", timer, required=True)
    after = timer_status()
    if not all(state["enabled"] and state["active"] for state in after.values()):
        raise RuntimeError("timer_activation_not_verified")
    return after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument("--check", action="store_true", help="Read-only ownership, file, and timer verification")
    flags.add_argument("--enable", action="store_true", help="Install files and enable only the five Codex Coach timers")
    arguments = parser.parse_args()
    try:
        result = install_files(Path.home(), check=arguments.check)
        if arguments.enable:
            result["timers"] = enable_timers(result["changed"])
        elif arguments.check:
            result["timers"] = timer_status()
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "drift":
            return 1
        if arguments.check and not all(s["enabled"] and s["active"] for s in result["timers"].values()):
            return 1
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
