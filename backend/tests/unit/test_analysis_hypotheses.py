"""Hypothesis management: ids, evidence linking, status, and round-tripping (Phase 7)."""

from __future__ import annotations

from src.core.analysis.hypotheses import Hypothesis, HypothesisSet


def test_add_assigns_per_kind_sequential_ids() -> None:
    hypotheses = HypothesisSet()

    first = hypotheses.add("primary", "the promo drove the spike")
    second = hypotheses.add("primary", "seasonality drove the spike")
    third = hypotheses.add("null", "there was no real change")

    assert first == "primary-0"
    assert second == "primary-1"
    assert third == "null-0"


def test_new_hypothesis_starts_untested_with_no_evidence() -> None:
    hypotheses = HypothesisSet()
    hyp_id = hypotheses.add("exploratory", "region matters")

    hypothesis = hypotheses.get(hyp_id)

    assert hypothesis.status == "untested"
    assert hypothesis.evidence_for == []
    assert hypothesis.evidence_against == []


def test_record_evidence_files_into_for_or_against() -> None:
    hypotheses = HypothesisSet()
    hyp_id = hypotheses.add("primary", "the promo drove the spike")

    hypotheses.record_evidence(hyp_id, "execution-0", supports=True)
    hypotheses.record_evidence(hyp_id, "execution-1", supports=False)

    hypothesis = hypotheses.get(hyp_id)
    assert hypothesis.evidence_for == ["execution-0"]
    assert hypothesis.evidence_against == ["execution-1"]


def test_record_evidence_does_not_duplicate_the_same_node() -> None:
    hypotheses = HypothesisSet()
    hyp_id = hypotheses.add("primary", "x")

    hypotheses.record_evidence(hyp_id, "execution-0", supports=True)
    hypotheses.record_evidence(hyp_id, "execution-0", supports=True)

    assert hypotheses.get(hyp_id).evidence_for == ["execution-0"]


def test_record_evidence_on_an_unknown_hypothesis_is_a_no_op() -> None:
    hypotheses = HypothesisSet()

    hypotheses.record_evidence("primary-0", "execution-0", supports=True)  # must not raise

    assert hypotheses.get("primary-0") is None


def test_set_status_updates_an_existing_hypothesis() -> None:
    hypotheses = HypothesisSet()
    hyp_id = hypotheses.add("primary", "x")

    hypotheses.set_status(hyp_id, "supported")

    assert hypotheses.get(hyp_id).status == "supported"


def test_set_status_on_an_unknown_hypothesis_is_a_no_op() -> None:
    HypothesisSet().set_status("primary-0", "supported")  # must not raise


def test_hypothesis_round_trips_through_dict() -> None:
    hypothesis = Hypothesis(
        id="primary-0", kind="primary", statement="x", status="refuted", evidence_for=["a"], evidence_against=["b"]
    )

    assert Hypothesis.from_dict(hypothesis.to_dict()) == hypothesis


def test_hypothesis_set_round_trips_and_continues_numbering() -> None:
    hypotheses = HypothesisSet()
    hypotheses.add("primary", "x")
    hypotheses.add("primary", "y")

    restored = HypothesisSet.from_dict(hypotheses.to_dict())
    assert {h.statement for h in restored.items.values()} == {"x", "y"}

    third = restored.add("primary", "z")
    assert third == "primary-2"


def test_empty_hypothesis_set_round_trips() -> None:
    assert HypothesisSet.from_dict(None).items == {}
    assert HypothesisSet.from_dict([]).to_dict() == []
