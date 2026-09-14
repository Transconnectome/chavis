"""The real module entry point exits even with a running provider thread."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from tools.cha_philosophy.store import Store


def test_refresh_sigint_exits_promptly_with_committed_checkpoint(tmp_path):
    home, ready, config = tmp_path / "private", tmp_path / "ready", tmp_path / "config.json"
    config.write_text("{}")
    code = '''
import importlib, pathlib, runpy, sys, threading
refresh = importlib.import_module('tools.cha_philosophy.refresh')
ready = pathlib.Path(sys.argv[1])
def provider(store, config, platform, limit):
    if platform == 'drive':
        store.checkpoint('synthetic:drive', {'processed': 1})
        ready.write_text('ready')
        threading.Event().wait(120)
    return {'results': [{'platform': platform, 'state': 'partial'}]}
refresh.sync = provider
refresh.local_ollama_available = lambda: False
sys.argv = ['cha-philosophy', '--home', sys.argv[2], 'refresh', '--config', sys.argv[3]]
runpy.run_module('tools.cha_philosophy', run_name='__main__')
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(ready), str(home), str(config)],
                               cwd=Path(__file__).resolve().parents[3],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={key: value for key, value in os.environ.items() if key != "CHA_TEAMS_ACCESS_TOKEN"})
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.01)
        assert ready.exists(), "synthetic provider did not start"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 130
        assert stdout == b"" and stderr == b""
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
    store = Store(home)
    try:
        assert store.checkpoint("synthetic:drive") == {"processed": 1}
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not (home / "refresh_status.json").exists()
    finally:
        store.close()
