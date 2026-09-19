#!/usr/bin/env python3
"""Preview or install only the Google Tasks agent's own user service and timer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


OWNER = "google-tasks-agent-installer-v1"
SOURCE = Path(__file__).resolve().parent
UNIT_NAMES = ("google-tasks-agent.service", "google-tasks-agent.timer")


def reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"symlink_denied: {component}")


def systemd_quote(value: str) -> str:
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError("invalid_unit_value")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def defaults(home: Path) -> dict:
    return {
        "account": "",
        "gog_path": str(home / "bin/gog"),
        "openclaw_path": str(home / ".npm-global/bin/openclaw"),
        "route_path": str(home / ".local/state/codex-coach/telegram-route.json"),
        "state_dir": str(home / ".local/state/google-tasks-agent"),
        "timezone": "Asia/Seoul",
        "digest_times": ["08:30", "17:30"],
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "upcoming_days": 3,
    }


def config_location(home: Path, config_path: Path | None = None) -> Path:
    return (config_path or home / ".config/google-tasks-agent/config.json").absolute()


def installation_files(home: Path, config_path: Path | None = None) -> dict[Path, bytes]:
    config = config_location(home, config_path)
    runner = SOURCE / "agent.py"
    reject_symlinks(runner)
    reject_symlinks(config)
    node = shutil.which("node")
    # OpenClaw's /usr/bin/env node must resolve in systemd as it does interactively.
    paths = ([str(Path(node).parent)] if node else []) + [
        str(home / ".local/bin"), str(home / ".npm-global/bin"),
        str(home / "bin"), "/usr/local/bin", "/usr/bin", "/bin",
    ]
    executable_path = ":".join(dict.fromkeys(paths))
    units = home / ".config/systemd/user"
    service = (
        f"# Managed by {OWNER}.\n"
        "[Unit]\nDescription=Monitor Google Tasks and deliver personal reminders\n"
        "After=network-online.target\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/python3 {systemd_quote(str(runner))} --config {systemd_quote(str(config))} tick\n"
        f"Environment={systemd_quote('PATH=' + executable_path)}\n"
        # 재부팅·로그아웃으로 user manager 환경이 비어도 자격증명이 살아남게 한다.
        # 선행 `-`는 필수 — 파일이 없어도 unit이 기동해야 한다. 없으면 서비스가
        # 아예 시작하지 않아 health 경고마저 사라지고 지금보다 나빠진다.
        # systemd 지정자 %h(사용자 홈)를 쓴다. systemd_quote()로 감싸면
        # EnvironmentFile= 파서가 따옴표를 경로의 일부로 읽어 "not absolute"로
        # 무시해 버린다(Environment= 와 파싱 규칙이 다르다).
        "EnvironmentFile=-%h/.config/google-tasks-agent/environment\n"
        "TimeoutStartSec=240\nUMask=0077\n"
    )
    timer = (
        f"# Managed by {OWNER}.\n"
        "[Unit]\nDescription=Check Google Tasks every five minutes\n\n"
        "[Timer]\nOnCalendar=*-*-* *:0/5:00\nPersistent=true\n"
        "AccuracySec=10s\nUnit=google-tasks-agent.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {units / UNIT_NAMES[0]: service.encode(), units / UNIT_NAMES[1]: timer.encode()}


def manifest_location(home: Path) -> Path:
    return home / ".local/state/google-tasks-agent/install-manifest.json"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(home: Path) -> dict:
    path = manifest_location(home)
    reject_symlinks(path)
    if not path.exists():
        return {"owner": OWNER, "files": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("owner") != OWNER or not isinstance(data.get("files"), dict):
        raise ValueError("manifest_owner_invalid")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data["files"].items()):
        raise ValueError("manifest_entries_invalid")
    return data


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        reject_symlinks(path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect(home: Path, config_path: Path | None = None) -> dict:
    files = installation_files(home, config_path)
    previous = read_manifest(home)
    changed, conflicts = [], []
    for path, data in files.items():
        reject_symlinks(path)
        if path.exists() and not path.is_file():
            conflicts.append(str(path))
            continue
        current = path.read_bytes() if path.exists() else None
        if current == data:
            continue
        changed.append(str(path))
        if current is not None and previous["files"].get(str(path)) != file_hash(current):
            conflicts.append(str(path))
    config = config_location(home, config_path)
    reject_symlinks(config)
    if config.exists():
        if not config.is_file() or not isinstance(json.loads(config.read_text()), dict):
            raise ValueError("config_not_json_object")
    return {"unit_changes": changed, "conflicts": conflicts, "config_path": str(config),
            "config_exists": config.exists(), "units": [str(p) for p in files]}


def install(home: Path, config_path: Path | None = None, *, account: str | None = None) -> dict:
    home = home.absolute()
    report = inspect(home, config_path)
    if report["conflicts"]:
        raise ValueError("unowned_or_modified_unit_conflict: " + ", ".join(report["conflicts"]))
    if not (SOURCE / "agent.py").is_file():
        raise ValueError("agent_source_missing")
    if not shutil.which("node"):
        raise ValueError("node_runtime_missing")
    config = config_location(home, config_path)
    values = json.loads(config.read_text()) if config.exists() else defaults(home)
    if not config.exists() and account:
        values['account'] = account
    if not isinstance(values.get('account'), str) or not values['account'].strip():
        raise ValueError('account_required: set account in private config or pass --account on first install')
    state = manifest_location(home).parent
    reject_symlinks(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(config, (json.dumps(values, indent=2) + "\n").encode(), 0o600)
    files = installation_files(home, config_path)
    for path, data in files.items():
        if str(path) in report["unit_changes"]:
            atomic_write(path, data, 0o644)
    manifest = {"owner": OWNER, "files": {str(p): file_hash(data) for p, data in files.items()}}
    atomic_write(manifest_location(home), (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    return {**report, "status": "installed", "config_created": not report["config_exists"]}


def systemctl(*arguments: str, required: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError(f"systemctl_failed: {arguments[0]} (exit {result.returncode})")
    return result


def runtime_status() -> dict:
    return {
        "timer_enabled": systemctl("is-enabled", "--quiet", UNIT_NAMES[1]).returncode == 0,
        "timer_active": systemctl("is-active", "--quiet", UNIT_NAMES[1]).returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable", action="store_true", help="Install and enable this agent's timer")
    mode.add_argument("--check", action="store_true", help="Read installation and timer status")
    parser.add_argument("--config", type=Path, help="Existing or new config path")
    parser.add_argument("--account", help="Google account for first install; existing config is preserved")
    args = parser.parse_args()
    home = Path.home()
    try:
        if args.enable:
            before = runtime_status()
            report = install(home, args.config, account=args.account)
            systemctl("daemon-reload", required=True)
            systemctl("enable", "--now", UNIT_NAMES[1], required=True)
            if before["timer_active"] and any(Path(p).name == UNIT_NAMES[1] for p in report["unit_changes"]):
                systemctl("restart", UNIT_NAMES[1], required=True)
            report.update(runtime_status())
            success = report["timer_enabled"] and report["timer_active"]
        else:
            report = inspect(home, args.config)
            report["status"] = "check" if args.check else "preview_no_writes"
            if args.check:
                report.update(runtime_status())
                success = (not report["unit_changes"] and not report["conflicts"] and report["config_exists"]
                           and report["timer_enabled"] and report["timer_active"])
            else:
                success = not report["conflicts"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if success else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
