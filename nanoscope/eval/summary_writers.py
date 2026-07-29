"""Write complete, sanitized Agent/Security/RAG/Unified baseline summaries."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from nanoscope.eval.artifacts import atomic_write_json

SUMMARY_SCHEMA_VERSION = "1.0"
_FILENAMES = {
    "agent": "AGENT_V1.json",
    "security": "SECURITY_V1.json",
    "rag": "RAG_V1.json",
    "unified": "UNIFIED_V1.json",
}
_SAFE_MANIFEST_KEYS = {
    "run_id",
    "started_at",
    "git_commit",
    "dirty_worktree",
    "dataset_version",
    "dataset_sha256",
    "metric_schema_version",
    "model",
    "provider",
    "price_table_version",
    "random_seed",
    "python_version",
    "platform",
    "credential_env_names",
}


def _as_mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    raise TypeError(f"Unsupported summary value: {type(value).__name__}")


def _summary(value: Any) -> dict[str, Any]:
    data = _as_mapping(value)
    return _strip_records(data)


def _strip_records(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _strip_records(item)
            for key, item in value.items()
            if key != "records"
        }
    if isinstance(value, (list, tuple)):
        return [_strip_records(item) for item in value]
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_evaluation_artifacts(
    *,
    output_dir: str | Path,
    agent: Any,
    security: Any,
    rag: Any,
    unified: Any,
    manifest: Any,
) -> dict[str, Path]:
    """Write four independent summaries plus a hash-verifiable manifest."""
    directory = Path(output_dir)
    values = {
        "agent": agent,
        "security": security,
        "rag": rag,
        "unified": unified,
    }
    outputs: dict[str, Path] = {}
    registry: dict[str, dict[str, Any]] = {}
    for name, value in values.items():
        path = directory / _FILENAMES[name]
        board = _summary(value)
        atomic_write_json(
            path,
            {
                "schema_version": SUMMARY_SCHEMA_VERSION,
                "artifact_type": name,
                "board": board,
            },
        )
        outputs[name] = path
        registry[name] = {
            "path": path.name,
            "sha256": _sha256(path),
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "artifact_type": name,
            "gate": _extract_gate(name, board),
            "records_included": False,
        }

    raw_manifest = _as_mapping(manifest)
    safe_manifest = {
        key: raw_manifest[key] for key in _SAFE_MANIFEST_KEYS if key in raw_manifest
    }
    manifest_path = directory / "MANIFEST_V1.json"
    atomic_write_json(
        manifest_path,
        {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "run": safe_manifest,
            "artifacts": registry,
        },
    )
    outputs["manifest"] = manifest_path
    return outputs


def _extract_gate(name: str, board: Mapping[str, Any]) -> bool | None:
    if name == "agent":
        explicit = board.get("agent_gate_pass")
        if explicit is not None:
            return bool(explicit)
        strict_tsr = board.get("strict_tsr")
        if strict_tsr is None:
            return None
        return bool(
            board.get("n_errored", 0) == 0
            and board.get("n_skipped", 0) == 0
            and strict_tsr >= 1.0
        )
    if name == "security":
        return board.get("security_gate_pass")
    if name == "rag":
        live = board.get("live")
        statuses = board.get("level_statuses")
        if isinstance(live, list) and live:
            completed = {
                item.get("level")
                for item in statuses or []
                if isinstance(item, Mapping) and item.get("status") == "completed"
            }
            return bool(
                completed == {"L1", "L2", "L3"}
                and all(
                    isinstance(item, Mapping)
                    and item.get("security_gate_pass")
                    and item.get("reliability_gate_pass")
                    for item in live
                )
            )
        security = board.get("security_gate_pass")
        reliability = board.get("reliability_gate_pass")
        return None if security is None or reliability is None else bool(security and reliability)
    gates = board.get("gates")
    return gates.get("overall") if isinstance(gates, Mapping) else board.get("overall_gate_pass")
