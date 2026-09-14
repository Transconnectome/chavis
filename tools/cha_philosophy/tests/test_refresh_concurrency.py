"""Independent source progress and lock/connection lifetime, without providers."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib
import json
import sqlite3
from threading import Event, Lock, Thread, get_ident

import pytest

from tools.cha_philosophy.store import Store

refresh_module = importlib.import_module("tools.cha_philosophy.refresh")
sync_module = importlib.import_module("tools.cha_philosophy.sync")
CONFIG = {"account": "professor@example.test", "teams_auth": {"synthetic": True}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("CHA_TEAMS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(sync_module, "GogClient", lambda *args, **kwargs: object())
    value = Store(tmp_path / "private")
    yield value
    value.close()


def successful(platform):
    return {"platform": platform, "state": "partial", "changed_sources": 0}


def install_providers(monkeypatch, provider):
    monkeypatch.setattr(sync_module, "sync_gmail",
                        lambda store, config, client, limit: provider(store, "gmail"))
    monkeypatch.setattr(sync_module, "sync_drive",
                        lambda store, config, client, limit: provider(store, "drive"))
    monkeypatch.setattr(sync_module, "sync_teams",
                        lambda store, config, limit: provider(store, "teams"))
    monkeypatch.setattr(sync_module, "sync_teams_archive",
                        lambda store, config: provider(store, "teams_archive"))


def run_sync(home, platform):
    own = Store(home)
    try:
        return sync_module.sync(own, CONFIG, platform=platform, limit=1)
    finally:
        own.close()


def test_blocked_drive_does_not_delay_committed_gmail_and_teams_progress(store, monkeypatch):
    drive_started, release_drive, teams_committed = Event(), Event(), Event()
    opened, closed, providers, observations = [], [], {}, []
    records = Lock()
    original_coverage = Store.coverage

    class WorkerStore(Store):
        def __init__(self, home):
            super().__init__(home)
            with records:
                opened.append((id(self), get_ident()))

        def close(self):
            with records:
                closed.append((id(self), get_ident()))
            super().close()

        def coverage(self, scope, value):
            original_coverage(self, scope, value)
            if scope == "teams:refresh_attempt":
                teams_committed.set()

    monkeypatch.setattr(refresh_module, "Store", WorkerStore)

    def provider(own, platform):
        with records:
            assert len(opened) == 3  # No source writes overlap worker Store startup.
            providers[platform] = (id(own), get_ident())
        assert own is not store
        own.db.execute("SELECT 1").fetchone()  # Connection belongs to this thread.
        if platform == "drive":
            drive_started.set()
            assert release_drive.wait(5)
        own.checkpoint("synthetic:" + platform, {"processed": 1})
        return successful(platform)

    install_providers(monkeypatch, provider)

    def observe_then_release():
        try:
            assert drive_started.wait(5)
            assert teams_committed.wait(5)
            with sqlite3.connect("file:" + str(store.path) + "?mode=ro", uri=True) as reader:
                teams = reader.execute("SELECT data FROM checkpoints WHERE scope='synthetic:teams'").fetchone()
                attempt = reader.execute("SELECT data FROM coverage WHERE scope='teams:refresh_attempt'").fetchone()
                drive = reader.execute("SELECT data FROM checkpoints WHERE scope='synthetic:drive'").fetchone()
            observations.append((json.loads(teams[0]), json.loads(attempt[0]), drive))
        except BaseException as exc:
            observations.append(exc)
        finally:
            release_drive.set()

    observer = Thread(target=observe_then_release)
    observer.start()
    try:
        result = refresh_module.refresh(store, CONFIG, distill_limit=0)
    finally:
        release_drive.set()
        observer.join(5)
    assert len(observations) == 1 and not isinstance(observations[0], BaseException)
    checkpoint, attempt, drive = observations[0]
    assert checkpoint == {"processed": 1}
    assert attempt["state"] == "partial" and attempt["attempt_succeeded"] is True
    assert drive is None
    assert list(result["sources"]) == ["gmail", "drive", "teams", "teams_archive"]
    assert len({thread for _, thread in providers.values()}) == 3
    assert sorted(opened) == sorted(closed)
    assert all(thread != get_ident() for _, thread in opened)


def test_same_platform_excluded_while_other_platforms_and_all_continue(store, monkeypatch):
    drive_started, release_drive = Event(), Event()
    calls = []

    def provider(own, platform):
        calls.append(platform)
        if platform == "drive":
            drive_started.set()
            assert release_drive.wait(5)
        own.checkpoint("synthetic:" + platform, {"processed": 1})
        return successful(platform)

    install_providers(monkeypatch, provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run_sync, store.home, "drive")
        try:
            assert drive_started.wait(5)
            assert sync_module.sync(store, CONFIG, platform="drive") == {"state": "already_running"}
            result = sync_module.sync(store, CONFIG, platform="all")
            assert [row["platform"] for row in result["results"]] == ["gmail", "drive", "teams", "teams_archive"]
            assert result["results"][1]["state"] == "already_running"
            assert store.checkpoint("synthetic:gmail") == {"processed": 1}
            assert store.checkpoint("synthetic:teams") == {"processed": 1}
            assert store.checkpoint("synthetic:drive") is None
            assert calls.count("drive") == 1
            with (store.home / "sync.lock").open("a") as maintenance:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(maintenance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release_drive.set()
        assert pending.result(timeout=5)["results"][0]["state"] == "partial"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in store.home.glob("sync*.lock"))


def test_global_exclusive_maintenance_blocks_every_source_platform(store, monkeypatch):
    calls = []
    install_providers(monkeypatch, lambda own, platform: calls.append(platform) or successful(platform))
    with (store.home / "sync.lock").open("a") as maintenance:
        fcntl.flock(maintenance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for platform in ("gmail", "drive", "teams", "teams_archive", "all"):
            assert sync_module.sync(store, CONFIG, platform=platform) == {"state": "already_running"}
        assert calls == []
    assert sync_module.sync(store, CONFIG, platform="gmail")["results"][0]["platform"] == "gmail"


def test_platform_error_keeps_order_and_other_platform_checkpoints(store, monkeypatch):
    def provider(own, platform):
        if platform == "gmail":
            raise RuntimeError("synthetic private provider detail")
        own.checkpoint("synthetic:" + platform, {"processed": 1})
        return successful(platform)

    install_providers(monkeypatch, provider)
    result = sync_module.sync(store, CONFIG, platform="all")
    assert [row["platform"] for row in result["results"]] == ["gmail", "drive", "teams", "teams_archive"]
    assert result["results"][0]["state"] == "error"
    assert result["results"][0]["checkpoint_preserved"] is True
    assert store.checkpoint("synthetic:drive") == {"processed": 1}
    assert store.checkpoint("synthetic:teams") == {"processed": 1}
    assert "synthetic private provider detail" not in json.dumps(result)


def test_unconfigured_teams_has_no_worker_or_provider_call(store, monkeypatch):
    calls = []
    install_providers(monkeypatch, lambda own, platform: calls.append(platform) or successful(platform))
    result = refresh_module.refresh(store, {"account": CONFIG["account"]}, distill_limit=0)
    assert sorted(calls) == ["drive", "gmail"]
    assert result["sources"]["teams"]["state"] == "not_configured"
    assert list(result["sources"]) == ["gmail", "drive", "teams", "teams_archive"]


def test_worker_initialization_failure_does_not_deadlock_other_sources(store, monkeypatch):
    created = 0
    calls = []

    def sometimes_unavailable(home):
        nonlocal created
        created += 1
        if created == 2:
            raise OSError("synthetic private Store detail")
        return Store(home)

    monkeypatch.setattr(refresh_module, "Store", sometimes_unavailable)
    install_providers(monkeypatch, lambda own, platform: calls.append(platform) or successful(platform))
    result = refresh_module.refresh(store, CONFIG, distill_limit=0)
    assert created == 3 and len(calls) == 2
    failures = [row for row in result["sources"].values() if row["state"] == "error"]
    assert len(failures) == 1 and failures[0]["error_code"] == "source_store_unavailable"
    assert "synthetic private Store detail" not in json.dumps(result)


def test_core_interrupt_propagates_without_join_and_retains_cycle_lock(store, monkeypatch):
    drive_started, release_drive = Event(), Event()
    pools = []
    shutdown_calls = []

    class InterruptResult:
        def __init__(self, future):
            self.future = future

        def add_done_callback(self, callback):
            self.future.add_done_callback(callback)

        def result(self):
            assert drive_started.wait(5)
            raise KeyboardInterrupt("synthetic interrupt")

    class InterruptingPool(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.submitted = 0
            pools.append(self)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submitted += 1
            return InterruptResult(future) if self.submitted == 1 else future

        def shutdown(self, wait=True, *, cancel_futures=False):
            shutdown_calls.append((wait, cancel_futures))
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def provider(own, platform):
        if platform == "drive":
            drive_started.set()
            assert release_drive.wait(5)
        return successful(platform)

    monkeypatch.setattr(refresh_module, "ThreadPoolExecutor", InterruptingPool)
    install_providers(monkeypatch, provider)
    try:
        with pytest.raises(KeyboardInterrupt, match="synthetic interrupt"):
            refresh_module.refresh(store, CONFIG, distill_limit=0)
        assert shutdown_calls == [(False, True)]
        assert not release_drive.is_set()
        assert refresh_module.refresh(store, CONFIG, distill_limit=0)["state"] == "already_running"
        with (store.home / "refresh.lock").open("a") as other_cycle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other_cycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release_drive.set()
        for pool in pools:
            pool.shutdown(wait=True)
    with (store.home / "refresh.lock").open("a") as after_workers:
        fcntl.flock(after_workers, fcntl.LOCK_EX | fcntl.LOCK_NB)
