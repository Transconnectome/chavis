"""Install local command, skill bridges and an optional user refresh timer.

Never changes source-service settings or starts an inference service. Existing
skills are preserved with a marked append and a private pre-edit backup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
MARKER = "<!-- CHA-PHILOSOPHY:BEGIN v1 -->"
BRIDGE = """

<!-- CHA-PHILOSOPHY:BEGIN v1 -->
## 근거가 갱신되는 차교수 원칙 적용

차지욱 교수의 글쓰기·연구·평가·리뷰 작업에서는
`/home/juke/git/chavis/skills/cha-philosophy/SKILL.md`를 읽고 현재 작업에 맞는
`cha-philosophy bundle TASK '구체적인 요청' --limit 3`의 근거·범위·예외를 확인한다.
등록부의 검토된 해석만 개인화에 적용하고, 과거 캐릭터 요약이나 추출 후보를
교수 본인의 확인된 입장으로 취급하지 않는다. 근거가 없으면 개인화 귀속을 유보한다.
현재 요청·실제 증거·공식 rubric을 우선하고, 개인 선호로 점수나 과학적 결론을 바꾸지 않는다.
원칙 ID와 바뀐 문장/판단을 짧은 적용 기록에 남긴다. 이 연결은 외부 발송 권한을 추가하지 않는다.
<!-- CHA-PHILOSOPHY:END -->
"""


def _new_or_identical(path, content, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_text()!=content: raise FileExistsError(f"existing different installation: {path.name}")
        os.chmod(path,mode)
        return
    with os.fdopen(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode),"w") as handle:
        handle.write(content)


def _link(path, target):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() and path.resolve()==target.resolve():return
    if path.exists() or path.is_symlink():raise FileExistsError(f"existing installation: {path.name}")
    path.symlink_to(target,target_is_directory=target.is_dir())


def _unit_quote(value):
    # systemd expands specifiers even within quotes; double a literal percent.
    return '"'+str(value).replace("%","%%").replace("\\","\\\\").replace('"','\\"')+'"'


def install(user_home, private_home, python, *, enable_timer=False, bridge=True):
    user_home=Path(user_home).resolve();private_home=Path(private_home).resolve()
    private_home.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(private_home,0o700)
    command=user_home/".local/bin/cha-philosophy"
    wrapper="#!/bin/sh\ncd "+shlex.quote(str(REPO))+" || exit 1\nexec "+shlex.quote(str(python))+" -m tools.cha_philosophy \"$@\"\n"
    _new_or_identical(command,wrapper,0o700)
    paths=[str(command)]
    for root in (".codex/skills",".agents/skills",".claude/skills"):
        path=user_home/root/"cha-philosophy";_link(path,REPO/"skills/cha-philosophy");paths.append(str(path))
    agent=user_home/".claude/agents/cha-philosophy.md"
    _link(agent,REPO/"agents/cha-philosophy.md");paths.append(str(agent))
    backups=[];seen=set()
    if bridge:
        backup_dir=private_home/"installation_backups";backup_dir.mkdir(exist_ok=True,mode=0o700)
        for root in (".codex/skills",".agents/skills",".claude/skills"):
            for name in ("cha-writer","reviewer","write-loop","chavis-antisyc"):
                skill=user_home/root/name/"SKILL.md"
                if not skill.exists() or skill.resolve() in seen:continue
                seen.add(skill.resolve());original=skill.read_text()
                if MARKER in original:continue
                snapshot=backup_dir/(hashlib.sha256(str(skill.resolve()).encode()).hexdigest()[:16]+".md")
                _new_or_identical(snapshot,original)
                # Preserve pre-existing file mode; append only this integration.
                with skill.open("a") as handle:handle.write(BRIDGE)
                backups.append({"skill":str(skill.resolve()),"backup":str(snapshot)})
    units=user_home/".config/systemd/user"
    service="""[Unit]
Description=Read local Cha philosophy sources and queue evidence candidates

[Service]
Type=oneshot
UMask=0077
WorkingDirectory="""+str(REPO).replace("%","%%")+"\nExecStart="+_unit_quote(command)+" --home "+_unit_quote(private_home)+" refresh\n"+"""TimeoutStartSec=45min
Nice=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=null
StandardError=journal
"""
    timer="""[Unit]
Description=Refresh Cha philosophy evidence hourly

[Timer]
OnActiveSec=5min
OnUnitInactiveSec=1h
RandomizedDelaySec=2min
Unit=cha-philosophy-refresh.service

[Install]
WantedBy=timers.target
"""
    _new_or_identical(units/"cha-philosophy-refresh.service",service)
    _new_or_identical(units/"cha-philosophy-refresh.timer",timer)
    report={"installed_at":datetime.now(timezone.utc).isoformat(),"paths":paths,"new_skill_bridges":backups,
            "timer_enabled":False,"model_service_started":False,"credential_persisted":False}
    if enable_timer:
        subprocess.run(["systemd-analyze","--user","verify",str(units/"cha-philosophy-refresh.service"),
                        str(units/"cha-philosophy-refresh.timer")],check=True,capture_output=True)
        # Import an already-authorized session credential without reading,
        # printing or writing its value. It lasts only this user-manager session.
        keys=[k for k in ("GOG_KEYRING_PASSWORD","CHA_TEAMS_ACCESS_TOKEN") if os.environ.get(k)]
        if keys:subprocess.run(["systemctl","--user","import-environment",*keys],check=True,capture_output=True)
        subprocess.run(["systemctl","--user","daemon-reload"],check=True,capture_output=True)
        subprocess.run(["systemctl","--user","enable","--now","cha-philosophy-refresh.timer"],check=True,capture_output=True)
        subprocess.run(["systemctl","--user","is-active","--quiet","cha-philosophy-refresh.timer"],check=True,capture_output=True)
        report["timer_enabled"]=True
        report["session_credential_names"]=keys
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    _new_or_identical(private_home/("installation-"+stamp+".json"),json.dumps(report,ensure_ascii=False,indent=2))
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--enable-timer",action="store_true")
    args=parser.parse_args()
    result=install(Path.home(),Path.home()/".local/share/cha-philosophy",Path(sys.executable),enable_timer=args.enable_timer)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
