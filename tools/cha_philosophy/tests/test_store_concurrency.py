"""Real SQLite contention and transaction rollback, using private fixtures."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from tools.cha_philosophy.store import Store


def test_second_writer_waits_before_reading_and_preserves_both_updates(tmp_path):
    home = tmp_path / "private"
    store = Store(home)
    store.coverage("counter", {"value": 0})
    store.close()
    initialized, startup = Barrier(2), Lock()
    first_read, second_attempt, second_entered, release = (Event() for _ in range(4))

    def increment(first):
        with startup:
            worker = Store(home)
        try:
            initialized.wait(timeout=5)
            if not first:
                assert first_read.wait(5)
                second_attempt.set()
            with worker.transaction():
                if not first:
                    second_entered.set()
                value = worker.db.execute("SELECT data FROM coverage WHERE scope='counter'").fetchone()[0]
                import json
                value = json.loads(value)["value"]
                if first:
                    first_read.set()
                    assert release.wait(5)
                worker.coverage("counter", {"value": value + 1})
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(increment, first) for first in (True, False)]
        try:
            assert second_attempt.wait(5)
            assert not second_entered.wait(0.2)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    reopened = Store(home)
    try:
        assert reopened.db.execute("SELECT json_extract(data, '$.value') FROM coverage WHERE scope='counter'").fetchone()[0] == 2
    finally:
        reopened.close()


def test_inner_failure_rolls_back_only_inner_work(tmp_path):
    store = Store(tmp_path / "private")
    try:
        with store.transaction():
            store.coverage("before", {"value": 1})
            with pytest.raises(ValueError):
                with store.transaction():
                    store.coverage("rolled-back", {"value": 2})
                    raise ValueError("synthetic failure")
            store.coverage("after", {"value": 3})
        assert [row[0] for row in store.db.execute("SELECT scope FROM coverage ORDER BY scope")] == ["after", "before"]
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("outer_kind", ["store", "sqlite"])
def test_outer_interrupt_rolls_back_nested_commits(tmp_path, outer_kind):
    store = Store(tmp_path / "private")
    try:
        with pytest.raises(KeyboardInterrupt):
            with store.transaction() if outer_kind == "store" else store.db:
                if outer_kind == "sqlite":
                    store.db.execute("INSERT INTO coverage VALUES('outer', 'synthetic', '{}')")
                store.coverage("nested", {"value": 1})
                raise KeyboardInterrupt
        assert store.db.execute("SELECT count(*) FROM coverage").fetchone()[0] == 0
        assert not store.db.in_transaction
        store.coverage("retry", {"value": 2})
    finally:
        store.close()
