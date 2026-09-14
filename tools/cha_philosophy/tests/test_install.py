"""Installation preservation and unattended startup contracts; no real service changes."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from tools.cha_philosophy.install import BRIDGE, install


def test_installer_preserves_existing_skill_and_private_backup_is_idempotent(tmp_path):
    user=tmp_path/"user";private=tmp_path/"private"
    skill=user/".codex/skills/cha-writer/SKILL.md"
    skill.parent.mkdir(parents=True);skill.write_text("Existing writing guidance.\n")
    skill.chmod(0o640)
    first=install(user,private,Path(sys.executable),bridge=True)
    assert skill.read_text()=="Existing writing guidance.\n"+BRIDGE
    assert skill.stat().st_mode & 0o777 == 0o640
    backup=Path(first["new_skill_bridges"][0]["backup"])
    assert backup.read_text()=="Existing writing guidance.\n"
    assert backup.stat().st_mode & 0o077 == 0
    second=install(user,private,Path(sys.executable),bridge=True)
    assert second["new_skill_bridges"]==[]
    assert skill.read_text().count("CHA-PHILOSOPHY:BEGIN")==1
    wrapper=user/".local/bin/cha-philosophy"
    result=subprocess.run([str(wrapper),"--home",str(private),"status"],capture_output=True,text=True)
    assert result.returncode==0
    assert isinstance(json.loads(result.stdout),dict)


def test_installation_never_overwrites_a_different_existing_command(tmp_path):
    user=tmp_path/"user";command=user/".local/bin/cha-philosophy"
    command.parent.mkdir(parents=True);command.write_text("existing user command")
    with pytest.raises(FileExistsError):install(user,tmp_path/"private",Path(sys.executable),bridge=False)
    assert command.read_text()=="existing user command"


def test_timer_imports_environment_names_without_persisting_secret_values(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setenv("GOG_KEYRING_PASSWORD","SYNTHETIC_GOG_PASSWORD_DO_NOT_PERSIST")
    monkeypatch.setenv("CHA_TEAMS_ACCESS_TOKEN","SYNTHETIC_TEAMS_TOKEN_DO_NOT_PERSIST")
    monkeypatch.setattr("tools.cha_philosophy.install.subprocess.run",lambda args,**kwargs:calls.append(args))
    user=tmp_path/"user";private=tmp_path/"private"
    report=install(user,private,Path(sys.executable),bridge=False,enable_timer=True)
    assert report["timer_enabled"] is True
    assert ["systemctl","--user","import-environment","GOG_KEYRING_PASSWORD","CHA_TEAMS_ACCESS_TOKEN"] in calls
    assert ["systemctl","--user","enable","--now","cha-philosophy-refresh.timer"] in calls
    texts=[json.dumps(report),str(calls)]
    for path in (user/".local/bin/cha-philosophy",user/".config/systemd/user/cha-philosophy-refresh.service",*private.glob("installation-*.json")):
        texts.append(path.read_text())
    assert "SYNTHETIC_GOG_PASSWORD" not in "\n".join(texts)
    assert "SYNTHETIC_TEAMS_TOKEN" not in "\n".join(texts)


def test_identical_nonexecutable_wrapper_is_not_reported_as_successful_install(tmp_path):
    user=tmp_path/"user";private=tmp_path/"private"
    install(user,private,Path(sys.executable),bridge=False)
    command=user/".local/bin/cha-philosophy";command.chmod(0o600)
    # Repairing the owned executable or refusing the unhealthy install is safe.
    try:install(user,private,Path(sys.executable),bridge=False)
    except (FileExistsError,PermissionError):return
    assert os.access(command,os.X_OK)


def test_timer_quotes_command_and_private_home_paths(tmp_path):
    user=tmp_path/"user with spaces";private=tmp_path/"private with spaces"
    install(user,private,Path(sys.executable),bridge=False)
    unit=(user/".config/systemd/user/cha-philosophy-refresh.service").read_text()
    exec_line=next(line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_line)==[str(user/".local/bin/cha-philosophy"),"--home",str(private),"refresh"]
    if shutil.which("systemd-analyze"):
        units=user/".config/systemd/user"
        result=subprocess.run(["systemd-analyze","--user","verify",str(units/"cha-philosophy-refresh.service"),
                               str(units/"cha-philosophy-refresh.timer")],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
