"""Evidence-backed reports (Phase 12): one shared model built from an immutable `AnalysisRun`,
and three renderers that differ only in presentation, never in the facts they draw from.
"""

from __future__ import annotations

from src.core.analysis.provenance import EvidenceGraph
from src.core.analysis.reports import build, executive, render, research, technical
from src.core.analysis.runs import AnalysisRun, DatasetManifestEntry, ExecutedStep, capture


def _traced_evidence() -> dict:
    graph = EvidenceGraph()
    dataset_id = graph.add_node("dataset", "sales.csv", content_hash="abc123")
    code_id = graph.add_node("code", "compute total")
    execution_id = graph.add_node("execution", "compute total", output="15")
    graph.add_edge(code_id, execution_id, "produced")
    graph.add_edge(dataset_id, execution_id, "derived_from")
    claim_id = graph.add_node("claim", "15")
    graph.add_edge(execution_id, claim_id, "supports")
    return graph.to_dict()


def _fixture_run(**analysis_overrides) -> AnalysisRun:
    analysis = {
        "evidence": _traced_evidence(),
        "objective": {"analytical_type": "descriptive", "ambiguity": []},
        "understanding": {},
        "hypotheses": [
            {
                "id": "primary-0",
                "kind": "primary",
                "statement": "the total is 15",
                "status": "supported",
                "evidence_for": ["execution-0"],
                "evidence_against": [],
            }
        ],
        "validations": [
            {"validator": "computational", "severity": "info", "message": "Recomputation matched.", "detail": ""}
        ],
        "critic_findings": [
            {
                "category": "wrong_test",
                "severity": "error",
                "message": "'independent_t_test' does not fit this data.",
                "detail": "alternative: one_way_anova",
                "suggested_reaction": "revise",
            }
        ],
        "route_comparisons": [
            {
                "verdict": "disagree",
                "routes": ["sub1", "sub2"],
                "agreement_detail": "Routes disagree: sub1=1, sub2=2.",
                "more_appropriate": "spearman_correlation",
                "why": "'spearman_correlation' fits; 'pearson_correlation' does not.",
                "residual_uncertainty": "None beyond what was reported.",
            }
        ],
        "confidence": {
            "verdict": "answerable_with_caveats",
            "components": [{"name": "verification", "level": "high", "reason": "Recomputation matched."}],
            "reasons": ["The chosen method does not fit this data."],
        },
        "plan": {
            "revisions": [
                {"index": 0, "text": "1. compute total", "why": ""},
                {"index": 1, "text": "1. recompute", "why": "verification mismatch"},
            ]
        },
    }
    analysis.update(analysis_overrides)
    return capture(
        session_id="s1",
        message_id=42,
        instruction="What is the total of A?",
        answer="The total is 15.",
        dataset_manifest=[
            DatasetManifestEntry(name="sales.csv", table_key="sales", content_hash="abc123", rows=5, columns=("A",))
        ],
        steps=[ExecutedStep(goal="compute total", code="print(df['A'].sum())")],
        analysis=analysis,
        warnings=["Independent verification disagreed with the analysis."],
    )


# --------------------------------------------------------------------------- #
# build() -- the shared model
# --------------------------------------------------------------------------- #
def test_build_extracts_a_claim_with_its_full_provenance_chain() -> None:
    model = build(_fixture_run())

    assert len(model.claims) == 1
    claim = model.claims[0]
    assert claim.value == "15"
    assert claim.provenance == ["code-0", "dataset-0", "execution-0", "claim-0"]
    assert model.every_claim_has_provenance is True


def test_build_reports_an_untraceable_claim_honestly_rather_than_padding_it() -> None:
    graph = EvidenceGraph()
    graph.add_node("claim", "99")  # no incoming edge -- nothing produced it
    run = _fixture_run(evidence=graph.to_dict())

    model = build(run)

    assert model.claims[0].provenance == ["claim-0"]
    assert model.every_claim_has_provenance is False


def test_build_carries_the_run_s_identity_and_facts_through_untouched() -> None:
    model = build(_fixture_run())

    assert model.session_id == "s1"
    assert model.message_id == 42
    assert model.instruction == "What is the total of A?"
    assert model.answer == "The total is 15."
    assert model.confidence["verdict"] == "answerable_with_caveats"
    assert model.dataset_manifest[0]["name"] == "sales.csv"
    assert model.steps[0]["goal"] == "compute total"
    assert len(model.plan_revisions) == 2


# --------------------------------------------------------------------------- #
# executive
# --------------------------------------------------------------------------- #
def test_executive_report_states_the_answer_and_the_confidence_verdict() -> None:
    report = executive.render(build(_fixture_run()))

    assert "What is the total of A?" in report
    assert "The total is 15." in report
    assert "answerable with caveats" in report


def test_executive_report_lists_warnings_as_caveats() -> None:
    report = executive.render(build(_fixture_run()))

    assert "## Caveats" in report
    assert "Independent verification disagreed with the analysis." in report


def test_executive_report_omits_caveats_section_with_no_warnings() -> None:
    run = _fixture_run()
    bare = AnalysisRun.from_dict({**run.to_dict(), "warnings": []})

    assert "## Caveats" not in executive.render(build(bare))


# --------------------------------------------------------------------------- #
# research
# --------------------------------------------------------------------------- #
def test_research_report_surfaces_objective_hypotheses_and_critic_findings() -> None:
    report = research.render(build(_fixture_run()))

    assert "descriptive" in report
    assert "the total is 15" in report
    assert "wrong_test" in report
    assert "spearman_correlation" in report


def test_research_report_counts_plan_revisions() -> None:
    report = research.render(build(_fixture_run()))

    assert "revised 1 time(s)" in report


# --------------------------------------------------------------------------- #
# technical
# --------------------------------------------------------------------------- #
def test_technical_report_includes_the_manifest_code_and_claim_provenance() -> None:
    report = technical.render(build(_fixture_run()))

    assert "sales.csv" in report
    assert "print(df['A'].sum())" in report
    assert "`15` ← execution-0 ← dataset-0 ← code-0" in report
    assert "computational" in report


def test_technical_report_lists_confidence_components_verbatim() -> None:
    report = technical.render(build(_fixture_run()))

    assert "verification: high" in report


# --------------------------------------------------------------------------- #
# render() dispatch, and the three-modes-one-model property
# --------------------------------------------------------------------------- #
def test_render_dispatches_to_the_named_mode() -> None:
    run = _fixture_run()

    assert render(run, mode="executive") == executive.render(build(run))
    assert render(run, mode="research") == research.render(build(run))
    assert render(run, mode="technical") == technical.render(build(run))


def test_render_defaults_to_executive() -> None:
    run = _fixture_run()

    assert render(run) == executive.render(build(run))


def test_every_mode_states_the_same_confidence_verdict() -> None:
    """Presentation differs; the underlying facts -- like the verdict -- never do."""
    run = _fixture_run()

    for mode in ("executive", "research", "technical"):
        assert "answerable with caveats" in render(run, mode=mode)
