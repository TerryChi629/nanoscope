from __future__ import annotations

import json
from datetime import UTC, datetime

from nanoscope.eval.agent_bench import AgentCaseResult, aggregate_agent_board
from nanoscope.eval.security_bench import SecurityCaseResult, aggregate_security_board
from nanoscope.eval.unified_bench import (
    aggregate_unified_board,
    build_manifest,
    write_json_report,
)


def test_manifest_hashes_dataset_and_never_reads_credential_values(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"version":"v1"}', encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-appear")

    manifest = build_manifest(
        repo=tmp_path,
        dataset_path=dataset,
        dataset_version="v1",
        model="fake",
        provider="offline",
        random_seed=7,
        credential_env_names=("DEEPSEEK_API_KEY",),
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    serialized = json.dumps(manifest.__dict__)
    assert manifest.dataset_sha256
    assert manifest.credential_env_names == ("DEEPSEEK_API_KEY",)
    assert "must-not-appear" not in serialized


def test_unified_gate_uses_boolean_conjunction_not_weighted_average(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    manifest = build_manifest(
        repo=tmp_path,
        dataset_path=dataset,
        dataset_version="v1",
        model="fake",
        provider="offline",
        random_seed=7,
    )
    agent = aggregate_agent_board(
        [AgentCaseResult("A1", "qa", True, True)]
    )
    security = aggregate_security_board(
        [
            SecurityCaseResult(
                "S1",
                "credential",
                True,
                credential_exposure=1,
            )
        ]
    )

    report = aggregate_unified_board(
        manifest,
        security,
        agent=agent,
        minimum_agent_tsr=0.9,
    )

    assert report.agent_gate_pass is True
    assert report.security_gate_pass is False
    assert report.overall_gate_pass is False


def test_json_baseline_omits_raw_records_by_default(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    manifest = build_manifest(
        repo=tmp_path,
        dataset_path=dataset,
        dataset_version="v1",
        model="fake",
        provider="offline",
        random_seed=7,
    )
    security = aggregate_security_board(
        [SecurityCaseResult("S1", "rag_acl", True)]
    )
    report = aggregate_unified_board(manifest, security)
    output = tmp_path / "report.json"

    write_json_report(report, output)
    data = json.loads(output.read_text(encoding="utf-8"))

    assert "records" not in data["security"]
    assert data["gates"]["overall"] is True
