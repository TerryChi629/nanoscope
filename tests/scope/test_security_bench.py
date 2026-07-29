from __future__ import annotations

from nanoscope.eval.security_bench import (
    SecurityCaseResult,
    aggregate_security_board,
    summarize_facets,
)
from nanoscope.eval.security_cases import collect_security_cases


def test_security_gate_is_zero_tolerance_across_all_facets():
    records = [
        SecurityCaseResult(
            case_id="S1",
            facet="rag_acl",
            attack_executed=True,
        ),
        SecurityCaseResult(
            case_id="S2",
            facet="workspace",
            attack_executed=True,
            workspace_escape=1,
        ),
    ]

    board = aggregate_security_board(records)

    assert board.attack_success_rate == 0.0
    assert board.n_violations == 1
    assert board.totals["workspace_escape"] == 1
    assert board.security_gate_pass is False


def test_fail_closed_failure_is_a_security_violation():
    record = SecurityCaseResult(
        case_id="S1",
        facet="ssrf",
        attack_executed=True,
        fail_closed_expected=True,
        fail_closed_observed=False,
    )

    board = aggregate_security_board([record])

    assert board.fail_closed_rate == 0.0
    assert board.security_gate_pass is False


def test_skips_and_errors_fail_completeness_not_clean_security_execution():
    records = [
        SecurityCaseResult(
            case_id="S1",
            facet="rag_acl",
            attack_executed=True,
        ),
        SecurityCaseResult(
            case_id="S2",
            facet="prompt_injection",
            attack_executed=False,
            skipped=True,
        ),
    ]

    board = aggregate_security_board(records)

    assert board.security_gate_pass is True
    assert board.completeness_gate_pass is False
    assert board.n_executed == 1
    assert board.n_skipped == 1


def test_summarize_facets_counts_only_executed_non_skipped_attacks():
    records = [
        SecurityCaseResult("S1", "rag_acl", True),
        SecurityCaseResult("S2", "rag_acl", True),
        SecurityCaseResult("S3", "ssrf", False, skipped=True),
    ]

    assert summarize_facets(records) == {"rag_acl": 2}


def test_real_security_collectors_cover_all_facets_and_pass():
    records, board = collect_security_cases(".")

    assert len(records) == 8
    assert set(summarize_facets(records)) == {
        "memory_isolation",
        "rag_acl",
        "prompt_injection",
        "workspace",
        "ssrf",
        "shell",
        "credential",
        "resource_exhaustion",
    }
    assert board.security_gate_pass is True
    assert board.completeness_gate_pass is True
    assert board.n_violations == 0
