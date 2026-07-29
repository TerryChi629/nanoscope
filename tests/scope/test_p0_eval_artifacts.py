from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest


def test_atomic_jsonl_keeps_previous_artifact_when_serialization_fails(tmp_path: Path):
    from nanoscope.eval.artifacts import atomic_write_jsonl

    output = tmp_path / "live.jsonl"
    output.write_text('{"stable": true}\n', encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_write_jsonl(output, [{"not_serializable": object()}])

    assert output.read_text(encoding="utf-8") == '{"stable": true}\n'
    assert not output.with_suffix(".jsonl.tmp").exists()


def test_explicit_level_plan_runs_only_requested_levels(monkeypatch):
    from nanoscope.eval.rag_live import build_level_plan

    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")

    plan = build_level_plan(("L1", "L3"))

    assert tuple(level.name for level in plan) == ("L1", "L3")


@dataclass(frozen=True)
class _Scenario:
    id: str


def test_resume_skips_completed_scenarios_and_preserves_partial_evidence(tmp_path: Path):
    from nanoscope.eval.rag_live import run_scenarios_resumable

    checkpoint = tmp_path / "checkpoints" / "L1.jsonl"
    scenarios = [_Scenario("S1"), _Scenario("S2")]
    calls: list[str] = []

    def fail_second(scenario: _Scenario) -> dict:
        calls.append(scenario.id)
        if scenario.id == "S2":
            raise RuntimeError("provider unavailable")
        return {"id": scenario.id, "success": True}

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_scenarios_resumable(
            level="L1",
            scenarios=scenarios,
            run_one=fail_second,
            checkpoint_path=checkpoint,
            resume=True,
        )

    partial = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert partial == [{"id": "S1", "success": True}]

    def succeed(scenario: _Scenario) -> dict:
        calls.append(scenario.id)
        return {"id": scenario.id, "success": True}

    records = run_scenarios_resumable(
        level="L1",
        scenarios=scenarios,
        run_one=succeed,
        checkpoint_path=checkpoint,
        resume=True,
    )

    assert calls == ["S1", "S2", "S2"]
    assert [record["id"] for record in records] == ["S1", "S2"]


def test_complete_summaries_register_hashes_without_credential_values(tmp_path: Path):
    from nanoscope.eval.summary_writers import write_evaluation_artifacts

    secret = "must-not-appear"
    outputs = write_evaluation_artifacts(
        output_dir=tmp_path,
        agent={"strict_tsr": 1.0, "records": [{"id": "A1"}]},
        security={"security_gate_pass": True, "records": [{"id": "S1"}]},
        rag={"label": "L3", "security_gate_pass": True, "records": [{"id": "R1"}]},
        unified={"gates": {"overall": True}},
        manifest={"credential_env_names": ["DEEPSEEK_API_KEY"], "secret": secret},
    )

    assert set(outputs) == {"agent", "security", "rag", "unified", "manifest"}
    for name in ("agent", "security", "rag", "unified"):
        artifact = json.loads(outputs[name].read_text(encoding="utf-8"))
        assert artifact["schema_version"]
        assert "records" not in artifact["board"]

    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    serialized = json.dumps(manifest)
    assert secret not in serialized
    assert set(manifest["artifacts"]) == {"agent", "security", "rag", "unified"}
    assert all(item["sha256"] for item in manifest["artifacts"].values())
