"""A curated project address broadens correspondence, never the member roster."""
from copy import deepcopy
import json

import pytest

from tools.cha_philosophy.connectors import (
    ConnectorError, GogClient, normalize_gmail, source_key, validate_lab_project_routes,
)
from tools.cha_philosophy.store import digest, source_hash
from tools.cha_philosophy.sync import _gmail_query, refresh_supported_evidence, sync_gmail
from tools.cha_philosophy.tests.test_sync import (
    CONFIG, GmailFixture, add_supported_gmail, expire_refresh_time, opened, thread,
)


ROUTE = "project-team@lists.example.invalid"
OTHER_ROUTE = "other-project@lists.example.invalid"
MEMBER = CONFIG["lab_recipients"][0]
PROFESSOR = CONFIG["account"]


def route_config(**changes):
    return {**CONFIG, "lab_project_routes": [ROUTE], **changes}


def message(mid, *, to=ROUTE, sender=PROFESSOR, labels=None, **fields):
    return {"id": mid, "from": sender, "to": to, "labelIds": ["SENT"] if labels is None else labels,
            "internalDate": "1700000000000", "body": "Use evidence when interpreting research.", **fields}


def raw(*messages, tid="route-thread"):
    return {"_account_id": PROFESSOR, "thread": {"id": tid, "messages": list(messages)}}


def normalized(value, routes=None):
    return normalize_gmail(value, {PROFESSOR}, {MEMBER}, lab_project_routes=routes)


def test_route_only_and_member_plus_route_have_separate_recipient_provenance():
    rows = normalized(raw(message("group"), message("both", to=MEMBER, cc=ROUTE)), [ROUTE])
    group, both = rows
    assert group["authorship"] == both["authorship"] == "direct"
    assert group["metadata"]["lab_recipients"] == []
    assert group["metadata"]["lab_project_routes"] == [ROUTE]
    assert group["metadata"]["authorship_basis"] == "sent_verified_from_exact_lab_project_route"
    assert both["metadata"]["lab_recipients"] == [MEMBER]
    assert both["metadata"]["lab_project_routes"] == [ROUTE]
    assert both["metadata"]["authorship_basis"] == "sent_verified_from_exact_lab_recipient"
    assert group["source_id"] == source_key("gmail", PROFESSOR, "route-thread", "", "group")


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_exact_current_recipient_header_supports_route(field):
    msg = message("route", to="unscoped@example.invalid")
    msg[field] = "Project Team <" + ROUTE.upper() + ">"
    row = normalized(raw(msg), [ROUTE])[0]
    assert row["authorship"] == "direct"
    assert row["metadata"]["lab_project_routes"] == [ROUTE]


@pytest.mark.parametrize("unqualified", [
    message("wrong", sender="other@example.invalid"),
    message("not-sent", labels=["INBOX"]),
    message("suffix", to=ROUTE + ".attacker.invalid"),
    message("quoted", to="unscoped@example.invalid", body="Forwarded conversation:\nTo: " + ROUTE),
    message("subject", to="unscoped@example.invalid", subject="Send to " + ROUTE),
    message("reply-to", to="unscoped@example.invalid", **{"reply-to": ROUTE}),
    message("ambiguous-author", sender=PROFESSOR + ", other@example.invalid"),
])
def test_group_address_does_not_override_authorship_or_current_header_requirement(unqualified):
    assert normalized(raw(unqualified), [ROUTE]) == []
    rows = normalized(raw(message("eligible"), unqualified), [ROUTE])
    assert rows[0]["authorship"] == "direct"
    assert rows[1]["authorship"] != "direct"
    assert rows[1]["authored_text"] == ""
    assert rows[1]["metadata"]["authorship_basis"] == "thread_context"


def test_membership_is_not_inferred_from_group_context_sender():
    student = message("student", sender="unlisted-attendee@example.invalid", labels=["INBOX"])
    rows = normalized(raw(message("prof"), student), [ROUTE])
    assert rows[1]["authorship"] == "context"
    assert rows[1]["metadata"]["lab_recipients"] == []
    assert rows[1]["metadata"]["lab_project_routes"] == [ROUTE]


def test_omitted_and_empty_routes_preserve_existing_row_and_source_hash():
    value = raw(message("member", to=MEMBER))
    original = normalize_gmail(value, {PROFESSOR}, {MEMBER})
    for routes in (None, []):
        assert normalized(value, routes) == original
    with_routes = normalized(value, [ROUTE])[0]
    assert with_routes["metadata"]["lab_project_routes"] == []
    assert source_hash(with_routes) == source_hash(original[0])
    assert normalized(raw(message("only-group"))) == []


@pytest.mark.parametrize("invalid", [
    ROUTE, {"address": ROUTE}, {ROUTE}, [None], [1], [True],
    [""], ["not-an-email"], ["Project <" + ROUTE + ">"], [" " + ROUTE],
    [ROUTE + "\n"], [ROUTE + " OR from:attacker@example.invalid"],
    ["route@example.invalid} {in:anywhere"], ["route@example.invalid,other@example.invalid"],
    ["route@@example.invalid"], [".route@example.invalid"], ["route..name@example.invalid"],
    ["route@example..invalid"], ["route@-example.invalid"], ["route@localhost"],
    ["x" * 65 + "@example.invalid"],
])
def test_invalid_route_configuration_rejected_before_query_or_normalization(invalid):
    with pytest.raises(ConnectorError, match="^gmail_lab_project_routes_invalid$"):
        _gmail_query(route_config(lab_project_routes=invalid))
    with pytest.raises(ConnectorError, match="^gmail_lab_project_routes_invalid$"):
        normalized(raw(message("member", to=MEMBER)), invalid)


def test_query_retains_original_order_then_appends_unique_routes():
    cfg = {**CONFIG, "identities": ["z@example.invalid", PROFESSOR],
           "lab_recipients": ["z-member@example.invalid", MEMBER, MEMBER]}
    original = "in:sent {from:z@example.invalid from:" + PROFESSOR + "} {" + " ".join(
        field + ":" + address for address in cfg["lab_recipients"] for field in ("to", "cc", "bcc")) + "}"
    assert _gmail_query(cfg) == original
    assert _gmail_query({**cfg, "lab_project_routes": []}) == original
    expanded = {**cfg, "lab_project_routes": [ROUTE.upper(), MEMBER.upper(), ROUTE, OTHER_ROUTE]}
    suffix = " ".join(field + ":" + address for address in (ROUTE, OTHER_ROUTE) for field in ("to", "cc", "bcc"))
    assert _gmail_query(expanded) == original[:-1] + " " + suffix + "}"
    assert validate_lab_project_routes(expanded["lab_project_routes"]) == [ROUTE, MEMBER, OTHER_ROUTE]
    assert digest(_gmail_query({**cfg, "lab_project_routes": [MEMBER.upper()]})) == digest(original)


def test_route_only_configuration_has_an_explicit_bounded_query():
    cfg = route_config(lab_recipients=[])
    assert _gmail_query(cfg) == "in:sent {from:" + PROFESSOR + "} {to:" + ROUTE + " cc:" + ROUTE + " bcc:" + ROUTE + "}"
    row = normalize_gmail(raw(message("route")), {PROFESSOR}, set(), lab_project_routes=[ROUTE])[0]
    assert row["metadata"]["authorship_basis"] == "sent_verified_from_exact_lab_project_route"


class RouteFixture(GmailFixture):
    def get_gmail_thread(self, tid):
        self.thread_calls.append(tid)
        return raw(message("message-" + tid), tid=tid)


def test_backfill_reread_and_due_priority_refresh_keep_same_route_authorship(tmp_path):
    with opened(tmp_path / "store") as store:
        client = RouteFixture({"": {"threads": [{"id": "curated"}]}})
        result = sync_gmail(store, route_config(), client, 10)
        assert result["state"] == "complete_for_query"
        sid, pid = add_supported_gmail(store, "curated")
        before = store.get_source(sid)
        assert before["metadata"]["authorship_basis"] == "sent_verified_from_exact_lab_project_route"
        expired = expire_refresh_time(store, sid)
        new_client = RouteFixture({"": {"threads": [{"id": "backlog"}]}})
        sync_gmail(store, route_config(), new_client, 1)
        assert new_client.thread_calls == ["curated", "backlog"]
        refreshed = store.get_source(sid)
        assert refreshed["verified_at"] != expired
        assert refreshed["source_hash"] == before["source_hash"]
        assert refreshed["metadata"]["lab_recipients"] == []
        assert refreshed["metadata"]["lab_project_routes"] == [ROUTE]
        assert store.get_principle(pid)["status"] == "evidence_supported"
        expire_refresh_time(store, sid)
        summary = refresh_supported_evidence(store, route_config(), new_client)
        assert summary["refreshed_threads"] == 1 and summary["withdrawn_sources"] == 0
        assert store.get_source(sid)["source_hash"] == before["source_hash"]


def test_scope_expansion_preserves_previous_checkpoint_sources_and_holds(tmp_path):
    with opened(tmp_path / "store") as store:
        previous = sync_gmail(store, CONFIG, GmailFixture({"": {"threads": [{"id": "old"}, {"id": "pending"}]}}), 1)
        sid = next(store.sources(platform="gmail"))["source_id"]
        store.hold_for_evaluation(sid, "synthetic hold")
        holds = [tuple(row) for row in store.db.execute("SELECT * FROM evaluation_holds")]
        checkpoint = deepcopy(store.checkpoint(previous["sync_scope"]))
        old_source = deepcopy(store.get_source(sid))
        updated = sync_gmail(store, route_config(), RouteFixture({"": {"threads": [{"id": "new-route"}]}}), 10)
        assert updated["sync_scope"] != previous["sync_scope"]
        assert store.checkpoint(previous["sync_scope"]) == checkpoint
        assert store.get_source(sid) == old_source
        assert [tuple(row) for row in store.db.execute("SELECT * FROM evaluation_holds")] == holds
        prior = json.loads(store.db.execute("SELECT data FROM coverage WHERE scope=?", (previous["sync_scope"],)).fetchone()[0])
        assert prior["scope_status"] == "superseded" and prior["superseded_by"] == updated["sync_scope"]


def test_iterator_route_classification_uses_same_normalizer_and_query_scope():
    class Client(GogClient):
        def __init__(self):
            super().__init__(PROFESSOR)
            self.searches = []
        def _pages(self, command, args, key, **kwargs):
            self.searches.append(args[0])
            yield [{"id": "only-group"}], None
        def get_thread(self, tid):
            return raw(message("route"), tid=tid)
    client = Client()
    assert len(list(client.iter_sent_threads({PROFESSOR}, {MEMBER}, lab_project_routes=[ROUTE]))) == 1
    assert client.searches == [_gmail_query(route_config())]
    assert list(client.iter_sent_threads({PROFESSOR}, {MEMBER})) == []
    assert client.searches[-1] == _gmail_query(CONFIG)


def test_invalid_routes_leave_store_coverage_and_checkpoint_untouched(tmp_path):
    with opened(tmp_path / "store") as store:
        result = sync_gmail(store, CONFIG, GmailFixture({"": {"threads": [{"id": "a"}, {"id": "b"}]}}), 1)
        before = deepcopy(store.checkpoint(result["sync_scope"]))
        coverage = [tuple(row) for row in store.db.execute("SELECT * FROM coverage")]
        client = RouteFixture({})
        with pytest.raises(ConnectorError, match="^gmail_lab_project_routes_invalid$"):
            sync_gmail(store, route_config(lab_project_routes=ROUTE), client, 1)
        assert store.checkpoint(result["sync_scope"]) == before
        assert [tuple(row) for row in store.db.execute("SELECT * FROM coverage")] == coverage
        assert client.page_calls == client.thread_calls == []
