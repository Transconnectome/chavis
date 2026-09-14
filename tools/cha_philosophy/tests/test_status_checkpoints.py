"""Status exposes resumable progress without checkpoint bodies or mutations."""
import json

import pytest

from tools.cha_philosophy.store import Store


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path / "private")
    yield value
    value.close()


def test_status_reflects_checkpoint_progress_before_coverage_is_updated(store):
    scope="drive:professor@example.invalid:0123456789ab"
    store.coverage(scope,{"scope_status":"active","state":"partial","cycle_files":3})
    store.checkpoint(scope,{"in_progress":True,"files":4,"pages":1,"pending":["private-file-a","private-file-b"]})
    first=store.status()
    assert first["checkpoints"][0]["files"]==4
    assert first["checkpoints"][0]["pending_count"]==2
    assert first["checkpoints"][0]["scope_status"]=="active"
    store.checkpoint(scope,{"in_progress":True,"files":5,"pages":1,"pending":["private-file-b"]})
    before=list(store.db.execute("SELECT scope,updated_at,data FROM checkpoints"))
    changes=store.db.total_changes
    second=store.status()
    item=second["checkpoints"][0]
    assert item["files"]==5 and item["pending_count"]==1
    assert item["scope"]==scope and item["updated_at"]==before[0]["updated_at"]
    assert second["coverage"][0]["cycle_files"]==3
    assert list(store.db.execute("SELECT scope,updated_at,data FROM checkpoints"))==before
    assert store.db.total_changes==changes


def test_status_checkpoint_summary_uses_only_safe_fields_and_supported_scopes(store):
    private="SYNTHETIC_PRIVATE_MARKER"
    state={"in_progress":True,"threads":12,"files":0,"pages":2,"channels":3,"chats":4,
           "pending":[private,{"id":private}],"query":private,"page_token":private,
           "nextLink":"https://example.invalid/"+private,"raw_next_links":[private],
           "message_ids":[private],"body":private,"pending_users":[private],
           "user_cursors":{private:private},"message_states":{private:{"cursor":private}},
           "inventoried_scopes":[private],"unrecognized_field":private}
    for scope in ("gmail:account:scope","drive:account:scope","teams:scope:chats"):
        store.checkpoint(scope,state)
    for scope in ("refresh:private","teams_archive:private","extraction_policy:private",private):
        store.checkpoint(scope,state)
    result=store.status()
    assert len(result["checkpoints"])==3
    allowed={"scope","updated_at","in_progress","threads","files","pages","channels","chats","pending_count"}
    for item in result["checkpoints"]:
        assert set(item)==allowed
        assert item["pending_count"]==2
        assert item["in_progress"] is True
    assert private not in json.dumps(result)


def test_status_distinguishes_superseded_and_active_checkpoint_scopes(store):
    old="gmail:account:old-query-hash"
    current="gmail:account:new-query-hash"
    teams="teams:configured-scope"
    for scope in (old,current,teams):
        store.checkpoint(scope,{"in_progress":True,"pending":[],"pages":1})
    store.coverage(old,{"scope_status":"superseded","state":"superseded"})
    store.coverage(current,{"scope_status":"active","state":"partial"})
    items={item["scope"]:item for item in store.status()["checkpoints"]}
    assert items[old]["scope_status"]=="superseded"
    assert items[current]["scope_status"]=="active"
    assert "scope_status" not in items[teams]
    # The summary reports persisted checkpoint state, not live process state.
    assert items[old]["in_progress"] is True


def test_status_omits_invalid_progress_values_and_nonobject_checkpoint_data(store):
    scope="drive:account:scope"
    store.checkpoint(scope,{"in_progress":"private","threads":True,"files":-1,
                            "pages":1.5,"channels":"private","chats":None,"pending":"private"})
    with store.transaction():
        store.db.execute("INSERT INTO checkpoints VALUES(?,?,?)",("teams:invalid","2026-01-01T00:00:00Z","{invalid"))
        store.db.execute("INSERT INTO checkpoints VALUES(?,?,?)",("gmail:nonobject","2026-01-01T00:00:00Z","[]"))
    items=store.status()["checkpoints"]
    assert len(items)==1
    assert set(items[0])=={"scope","updated_at","in_progress"}
    assert items[0]["in_progress"] is False
