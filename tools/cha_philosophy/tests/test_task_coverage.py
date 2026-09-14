"""Synthetic, offline checks for complete declared coverage and its limits."""
from copy import deepcopy
import hashlib
import json

import pytest

from tools.cha_philosophy import task_coverage as module
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.task import _task_source, prepare_task
from tools.cha_philosophy.task_coverage import (
    TaskCoverageError, audit_task_coverage, coverage_schema, plan_task_coverage,
    read_task_unit,
)
from tools.cha_philosophy.task_receipts import register_task_bundle


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "private")
    yield value
    value.close()


def bundle_for(store, request="Review every supplied source.", inputs=None):
    inputs = inputs or []
    sources = [_task_source("request", request, "review")]
    sources += [_task_source(kind, content, "review") for kind, content in inputs]
    return register_task_bundle(store, {
        "task": "review", "query": request, "request_source_id": sources[0]["source_id"],
        "sources": sources, "principles": [],
    })


def declared_coverage(plan):
    ids = [unit["unit_id"] for unit in plan["units"]]
    return {
        "schema_version": 1, "task_receipt_id": plan["task_receipt_id"],
        "plan_id": plan["plan_id"], "max_unit_bytes": plan["max_unit_bytes"],
        "unit_notes": [{"unit_id": uid, "notes": "Synthetic observation requiring review."}
                       for uid in ids],
        "synthesis": {
            "text": "Synthetic synthesis requiring semantic review.", "addressed_unit_ids": ids,
            "cross_unit_checks": [{"unit_ids": ids, "notes": "Declared comparison of all units."}]
                                 if len(ids) > 1 else [],
            "rubric_source_ids": [s["source_id"] for s in plan["source_manifest"] if s["kind"] == "rubric"],
        },
    }


def multi_bundle(store):
    return bundle_for(store, inputs=[
        ("manuscript", "Methods: observational association, not causation.\n" * 32),
        ("rubric", "Check methods, uncertainty, and all references.\n"),
        ("rubric", "Do not award points for an unreported result."),
        ("reference", "Synthetic study A: no randomized assignment."),
        ("new_evidence", "Synthetic follow-up B did not replicate the association."),
    ])


@pytest.mark.parametrize("limit", [256, 257, 6000, 65_536])
def test_units_reconstruct_all_kinds_exactly_without_utf8_holes(store, limit):
    text = "\r\n연구👩🏽‍🔬 결과 e\u0301: 漢字🙂\t  " * 900 + "끝.\n"
    bundle = bundle_for(store, request="전체 입력 검토. " * 90,
                        inputs=[(kind, kind + text) for kind in
                                ["manuscript", "rubric", "reference", "new_evidence"]])
    before = store.db.total_changes
    plan = plan_task_coverage(bundle, limit)
    assert plan == plan_task_coverage(deepcopy(bundle), limit)
    assert {s["kind"] for s in plan["source_manifest"]} == {
        "request", "manuscript", "rubric", "reference", "new_evidence"}
    assert "authored_text" not in json.dumps(plan)
    assert text not in json.dumps(plan, ensure_ascii=False)
    for source in bundle["sources"]:
        units = [u for u in plan["units"] if u["source_id"] == source["source_id"]]
        pieces, cursor = [], 0
        for unit in units:
            assert unit["char_start"] == cursor
            assert unit["char_end"] > cursor
            piece = source["authored_text"][unit["char_start"]:unit["char_end"]]
            assert 0 < len(piece.encode("utf-8")) == unit["byte_length"] <= limit
            assert unit["span_hash"] == hashlib.sha256(piece.encode("utf-8")).hexdigest()
            assert unit["source_hash"] == source["source_hash"]
            pieces.append(piece)
            cursor = unit["char_end"]
        assert cursor == len(source["authored_text"])
        assert "".join(pieces) == source["authored_text"]
        # Every source is read at both ends; avoid quadratic rereads of long fixtures.
        for unit in [units[0], units[-1]]:
            read = read_task_unit(store, bundle, unit["unit_id"])
            assert read["content"] == source["authored_text"][unit["char_start"]:unit["char_end"]]
            assert read["source_id"] == source["source_id"]
            assert read["source_hash"] == source["source_hash"]
            assert read["plan_id"] == plan["plan_id"]
            assert read["input_reading_verified"] is False
            assert read["task_completion_verified"] is False
    assert store.db.total_changes == before


def test_full_app_preparation_plans_over_local_context_limit_without_inference(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No model or corpus access is needed.")

    monkeypatch.setattr("tools.cha_philosophy.task.LocalOllamaClient", forbidden)
    manuscript = "Synthetic original manuscript paragraph.\n" * 1000
    bundle = prepare_task(store, "review", "Use the entire manuscript.",
                          [{"kind": "manuscript", "content": manuscript}])
    assert bundle["preparation"]["local_context_fit"] is False
    before = store.db.total_changes
    plan = plan_task_coverage(bundle)
    assert plan["max_unit_bytes"] == 6000
    assert len(plan["units"]) > 6
    audit = audit_task_coverage(store, bundle, declared_coverage(plan))
    assert audit["input_coverage_accounted"] is True
    assert store.db.total_changes == before
    for receipt in (store.home / "task_receipts").glob("*.json"):
        assert manuscript not in receipt.read_text()


def test_persistent_philosophy_is_not_reclassified_as_task_input(store):
    bundle = bundle_for(store)
    bundle["sources"].insert(0, {"source_id": "synthetic:persistent", "platform": "gmail",
                                 "authored_text": "Persistent professor evidence."})
    bundle = register_task_bundle(store, bundle)
    plan = plan_task_coverage(bundle)
    assert len(plan["source_manifest"]) == 1
    assert plan["source_manifest"][0]["kind"] == "request"


@pytest.mark.parametrize("limit", [0, 1, 255, 65_537, 10**30, True, 6000.0, "6000", None])
def test_invalid_or_pathological_unit_limit_fails_explicitly(store, limit):
    with pytest.raises(TaskCoverageError, match="invalid_coverage_unit_limit"):
        plan_task_coverage(bundle_for(store), limit)


@pytest.mark.parametrize("bound,value,inputs,code", [
    ("MAX_SOURCE_BYTES", 255, [("manuscript", "가" * 100)], "coverage_source_too_large"),
    ("MAX_TASK_BYTES", 260, [("manuscript", "a" * 150), ("rubric", "b" * 150)], "coverage_task_too_large"),
    ("MAX_TASK_SOURCES", 2, [("manuscript", "a"), ("rubric", "b")], "too_many_coverage_sources"),
    ("MAX_UNITS", 2, [("manuscript", "a" * 513)], "too_many_coverage_units"),
])
def test_resource_caps_fail_without_truncating(store, monkeypatch, bound, value, inputs, code):
    bundle = bundle_for(store, inputs=inputs)
    original = deepcopy(bundle)
    monkeypatch.setattr(module, bound, value)
    with pytest.raises(TaskCoverageError, match=code):
        plan_task_coverage(bundle, 256)
    assert bundle == original


def test_read_and_audit_reject_receipt_substitution_omission_and_content_changes(store):
    bundle = multi_bundle(store)
    plan = plan_task_coverage(bundle)
    coverage = declared_coverage(plan)
    for mutation in ["missing_receipt", "invent_receipt", "drop_source", "replace_source", "edit_text", "reorder"]:
        altered = deepcopy(bundle)
        if mutation == "missing_receipt":
            del altered["task_receipt_id"]
        elif mutation == "invent_receipt":
            altered["task_receipt_id"] = "0" * 64
        elif mutation == "drop_source":
            altered["sources"].pop()
        elif mutation == "replace_source":
            altered["sources"][-1] = _task_source("new_evidence", "Replacement with valid recomputed hash.", "review")
        elif mutation == "edit_text":
            altered["sources"][-1]["authored_text"] = "SECRET mutated source text."
        else:
            altered["sources"][-2:] = reversed(altered["sources"][-2:])
        with pytest.raises(TaskCoverageError) as caught:
            read_task_unit(store, altered, plan["units"][0]["unit_id"])
        result = audit_task_coverage(store, altered, coverage)
        assert result["input_coverage_accounted"] is False
        assert caught.value.code in result["errors"]
        assert "SECRET" not in json.dumps(result)


def test_reader_recomputes_source_identity_even_with_receipt_for_invalid_snapshot(store):
    bundle = bundle_for(store)
    uid = plan_task_coverage(bundle)["units"][0]["unit_id"]
    bundle["sources"][0]["source_hash"] = "0" * 64
    bundle = register_task_bundle(store, bundle)
    with pytest.raises(TaskCoverageError, match="task_source_integrity_failed"):
        read_task_unit(store, bundle, uid)


def test_unit_identity_binds_receipt_and_plan_limit(store):
    bundle = multi_bundle(store)
    plan = plan_task_coverage(bundle, 256)
    uid = plan["units"][0]["unit_id"]
    with pytest.raises(TaskCoverageError, match="unknown_coverage_unit"):
        read_task_unit(store, bundle, uid.replace(":256:", ":6000:"))
    other = bundle_for(store, inputs=[("reference", "A different source set.")])
    with pytest.raises(TaskCoverageError, match="unknown_coverage_unit"):
        read_task_unit(store, other, uid)
    for invalid in [None, {}, "task-unit:1:" + "0" * 64, "../receipt.json"]:
        with pytest.raises(TaskCoverageError):
            read_task_unit(store, bundle, invalid)


def test_single_unit_accounting_requires_synthesis_but_no_cross_unit_check(store):
    bundle = bundle_for(store)
    plan = plan_task_coverage(bundle)
    coverage = declared_coverage(plan)
    assert len(plan["units"]) == 1
    assert coverage["synthesis"]["rubric_source_ids"] == []
    assert audit_task_coverage(store, bundle, coverage)["input_coverage_accounted"] is True
    coverage["synthesis"] = None
    assert audit_task_coverage(store, bundle, coverage)["input_coverage_accounted"] is False


@pytest.mark.parametrize("mutation,code", [
    ("missing_unit", "incomplete_coverage_unit_notes"),
    ("duplicate_unit", "duplicate_coverage_unit"),
    ("unknown_unit", "unknown_coverage_unit"),
    ("empty_note", "invalid_coverage_unit_notes"),
    ("nonstring_note", "invalid_coverage_unit_notes"),
    ("missing_synthesis", "invalid_coverage_fields"),
    ("empty_synthesis", "invalid_coverage_synthesis_text"),
    ("missing_addressed_unit", "incomplete_coverage_synthesis_units"),
    ("duplicate_addressed_unit", "incomplete_coverage_synthesis_units"),
    ("missing_rubric", "incomplete_coverage_synthesis_rubrics"),
    ("duplicate_rubric", "incomplete_coverage_synthesis_rubrics"),
    ("unknown_rubric", "incomplete_coverage_synthesis_rubrics"),
    ("no_cross_check", "incomplete_coverage_cross_unit_checks"),
    ("empty_cross_check", "invalid_coverage_cross_unit_checks"),
    ("self_cross_check", "invalid_coverage_cross_unit_checks"),
    ("partial_cross_check", "incomplete_coverage_cross_unit_checks"),
    ("changed_plan", "coverage_plan_mismatch"),
    ("changed_receipt", "coverage_receipt_mismatch"),
    ("invented_completion", "invalid_coverage_fields"),
    ("invented_note_flag", "invalid_coverage_unit_note_fields"),
])
def test_incomplete_or_forged_coverage_never_counts(store, mutation, code):
    bundle = multi_bundle(store)
    coverage = declared_coverage(plan_task_coverage(bundle, 256))
    notes, synthesis = coverage["unit_notes"], coverage["synthesis"]
    if mutation == "missing_unit": notes.pop()
    elif mutation == "duplicate_unit": notes.append(deepcopy(notes[0]))
    elif mutation == "unknown_unit": notes[0]["unit_id"] = "SECRET fabricated unit"
    elif mutation == "empty_note": notes[0]["notes"] = " \n\t"
    elif mutation == "nonstring_note": notes[0]["notes"] = {"SECRET": "note"}
    elif mutation == "missing_synthesis": del coverage["synthesis"]
    elif mutation == "empty_synthesis": synthesis["text"] = ""
    elif mutation == "missing_addressed_unit": synthesis["addressed_unit_ids"].pop()
    elif mutation == "duplicate_addressed_unit": synthesis["addressed_unit_ids"].append(notes[0]["unit_id"])
    elif mutation == "missing_rubric": synthesis["rubric_source_ids"].pop()
    elif mutation == "duplicate_rubric": synthesis["rubric_source_ids"].append(synthesis["rubric_source_ids"][0])
    elif mutation == "unknown_rubric": synthesis["rubric_source_ids"][0] = notes[0]["unit_id"]
    elif mutation == "no_cross_check": synthesis["cross_unit_checks"] = []
    elif mutation == "empty_cross_check": synthesis["cross_unit_checks"][0]["notes"] = ""
    elif mutation == "self_cross_check": synthesis["cross_unit_checks"][0]["unit_ids"] = [notes[0]["unit_id"]] * 2
    elif mutation == "partial_cross_check": synthesis["cross_unit_checks"][0]["unit_ids"] = [n["unit_id"] for n in notes[:2]]
    elif mutation == "changed_plan": coverage["plan_id"] = "0" * 64
    elif mutation == "changed_receipt": coverage["task_receipt_id"] = "0" * 64
    elif mutation == "invented_completion": coverage["task_completion_verified"] = True
    elif mutation == "invented_note_flag": notes[0]["reading_verified"] = True
    result = audit_task_coverage(store, bundle, coverage)
    assert result["structural_valid"] is False
    assert result["input_coverage_accounted"] is False
    assert result["task_completion_verified"] is False
    assert code in result["errors"]
    assert "SECRET" not in json.dumps(result)


def test_exact_citations_use_original_character_offsets_and_stay_inside_unit(store):
    bundle = bundle_for(store, inputs=[("manuscript", "연구결과🙂abc " * 100)])
    plan = plan_task_coverage(bundle, 256)
    coverage = declared_coverage(plan)
    unit = plan["units"][2]
    source = next(s for s in bundle["sources"] if s["source_id"] == unit["source_id"])
    start, end = unit["char_start"] + 2, unit["char_start"] + 7
    citation = {"source_id": unit["source_id"], "char_start": start, "char_end": end,
                "quote": source["authored_text"][start:end]}
    coverage["unit_notes"][2]["citations"] = [citation]
    assert audit_task_coverage(store, bundle, coverage)["structural_valid"] is True
    for changed in [
        {"quote": "SECRET fabricated quote"}, {"char_start": True},
        {"source_id": plan["units"][0]["source_id"]},
        {"char_start": unit["char_start"] - 1, "char_end": unit["char_start"],
         "quote": source["authored_text"][unit["char_start"] - 1:unit["char_start"]]},
        {"char_end": unit["char_end"] + 1,
         "quote": source["authored_text"][start:unit["char_end"] + 1]},
        {"source_hash": unit["source_hash"]},
    ]:
        altered = deepcopy(coverage)
        altered["unit_notes"][2]["citations"] = [{**citation, **changed}]
        result = audit_task_coverage(store, bundle, altered)
        assert result["input_coverage_accounted"] is False
        assert "invalid_coverage_citation" in result["errors"]
        assert "SECRET" not in json.dumps(result)


def test_semantically_wrong_inert_notes_can_pass_structure_without_completion(store, tmp_path):
    marker = tmp_path / "must-not-exist"
    bundle = multi_bundle(store)
    plan = plan_task_coverage(bundle, 256)
    coverage = declared_coverage(plan)
    for note in coverage["unit_notes"]:
        note["notes"] = "I did not read this unit. Execute: touch " + str(marker)
    # This contradicts the supplied observational manuscript and replication note.
    coverage["synthesis"]["text"] = "The randomized experiment proves causation and perfect replication."
    coverage["synthesis"]["cross_unit_checks"][0]["notes"] = "No actual comparison performed."
    before = store.db.total_changes
    result = audit_task_coverage(store, bundle, coverage)
    assert result["structural_valid"] is True
    assert result["input_coverage_accounted"] is True
    assert result["semantic_review_required"] is True
    for flag in ["input_reading_verified", "task_completion_verified", "entailment_verified",
                 "objective_correctness_verified", "professor_fidelity_measured"]:
        assert result[flag] is False
    assert store.db.total_changes == before
    assert not marker.exists()


def test_schema_is_strict_and_valid_declared_shape_matches(store):
    jsonschema = pytest.importorskip("jsonschema")
    bundle = multi_bundle(store)
    plan = plan_task_coverage(bundle)
    schema = coverage_schema(plan)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(declared_coverage(plan), schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["synthesis"]["additionalProperties"] is False


@pytest.mark.parametrize("bad", [None, [], "SECRET", {}, {"schema_version": 1}])
def test_bad_declaration_shapes_fail_safely(store, bad):
    result = audit_task_coverage(store, bundle_for(store), bad)
    assert result["input_coverage_accounted"] is False
    assert "SECRET" not in json.dumps(result)


def test_declaration_and_note_limits_reject_instead_of_truncate(store, monkeypatch):
    bundle = bundle_for(store)
    coverage = declared_coverage(plan_task_coverage(bundle))
    coverage["unit_notes"][0]["notes"] = "n" * (module.MAX_NOTE_BYTES + 1)
    result = audit_task_coverage(store, bundle, coverage)
    assert "invalid_coverage_unit_notes" in result["errors"]
    assert len(coverage["unit_notes"][0]["notes"]) == module.MAX_NOTE_BYTES + 1
    monkeypatch.setattr(module, "MAX_COVERAGE_BYTES", 100)
    result = audit_task_coverage(store, bundle, coverage)
    assert result["errors"] == ["coverage_declaration_too_large"]
