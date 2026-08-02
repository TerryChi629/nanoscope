"""Durable confirmation, evidence, and idempotency ledger for Feishu writes."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feishu_pending_operations (
  id               TEXT PRIMARY KEY,
  token_hash       TEXT NOT NULL UNIQUE,
  tenant_id        TEXT NOT NULL,
  principal_id     TEXT NOT NULL,
  audience_id      TEXT NOT NULL,
  session_key      TEXT NOT NULL,
  channel_instance TEXT NOT NULL,
  instance_identity TEXT NOT NULL,
  operation_type   TEXT NOT NULL CHECK (operation_type='create_task'),
  payload_json     TEXT NOT NULL,
  payload_digest   TEXT NOT NULL,
  idempotency_key  TEXT NOT NULL UNIQUE,
  status           TEXT NOT NULL,
  remote_task_id   TEXT,
  remote_task_url  TEXT,
  error_code       TEXT,
  created_at       INTEGER NOT NULL,
  expires_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feishu_thread_evidence (
  tenant_id        TEXT NOT NULL,
  principal_id     TEXT NOT NULL,
  audience_id      TEXT NOT NULL,
  session_key      TEXT NOT NULL,
  channel_instance TEXT NOT NULL,
  message_id       TEXT NOT NULL,
  root_id          TEXT NOT NULL,
  expires_at       INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, principal_id, audience_id, session_key, message_id)
);
CREATE TABLE IF NOT EXISTS feishu_operation_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL,
  event        TEXT NOT NULL,
  detail       TEXT,
  created_at   INTEGER NOT NULL
);
"""

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class PendingOperationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PendingOperation:
    id: str
    tenant_id: str
    principal_id: str
    audience_id: str
    session_key: str
    channel_instance: str
    instance_identity: str
    payload: dict[str, Any]
    payload_digest: str
    idempotency_key: str
    status: str
    remote_task_id: str | None
    remote_task_url: str | None
    error_code: str | None
    created_at: int
    expires_at: int


def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PendingOperationStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _token() -> str:
        return "FT-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _row(row: sqlite3.Row) -> PendingOperation:
        return PendingOperation(
            id=row["id"],
            tenant_id=row["tenant_id"],
            principal_id=row["principal_id"],
            audience_id=row["audience_id"],
            session_key=row["session_key"],
            channel_instance=row["channel_instance"],
            instance_identity=row["instance_identity"],
            payload=json.loads(row["payload_json"]),
            payload_digest=row["payload_digest"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            remote_task_id=row["remote_task_id"],
            remote_task_url=row["remote_task_url"],
            error_code=row["error_code"],
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
        )

    def _event(self, operation_id: str, event: str, detail: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO feishu_operation_events(operation_id,event,detail,created_at) "
            "VALUES (?,?,?,?)",
            (operation_id, event, detail, int(time.time())),
        )

    def record_evidence(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        audience_id: str,
        session_key: str,
        channel_instance: str,
        root_id: str,
        message_ids: list[str],
        ttl_seconds: int,
    ) -> None:
        expires_at = int(time.time()) + ttl_seconds
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO feishu_thread_evidence "
                "(tenant_id,principal_id,audience_id,session_key,channel_instance,"
                "message_id,root_id,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id,
                        principal_id,
                        audience_id,
                        session_key,
                        channel_instance,
                        message_id,
                        root_id,
                        expires_at,
                    )
                    for message_id in message_ids
                ],
            )
            self._conn.commit()

    def validate_evidence(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        audience_id: str,
        session_key: str,
        channel_instance: str,
        message_ids: list[str],
    ) -> None:
        if not message_ids:
            return
        now = int(time.time())
        placeholders = ",".join("?" for _ in message_ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id FROM feishu_thread_evidence "
                "WHERE tenant_id=? AND principal_id=? AND audience_id=? AND session_key=? "
                "AND channel_instance=? AND expires_at>=? "
                f"AND message_id IN ({placeholders})",
                (
                    tenant_id,
                    principal_id,
                    audience_id,
                    session_key,
                    channel_instance,
                    now,
                    *message_ids,
                ),
            ).fetchall()
        found = {row["message_id"] for row in rows}
        missing = [message_id for message_id in message_ids if message_id not in found]
        if missing:
            raise PendingOperationError(
                "SOURCE_EVIDENCE_INVALID",
                "Task source messages were not read from the current Feishu thread",
            )

    def prepare(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        audience_id: str,
        session_key: str,
        channel_instance: str,
        instance_identity: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> tuple[PendingOperation, str]:
        raw = canonical_payload(payload)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        idempotency_key = hashlib.sha256(
            "\x00".join(
                (tenant_id, principal_id, audience_id, channel_instance, digest)
            ).encode()
        ).hexdigest()
        token = self._token()
        now = int(time.time())
        operation_id = uuid.uuid4().hex
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM feishu_pending_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing and existing["status"] == "succeeded":
                return self._row(existing), ""
            if existing:
                if existing["status"] == "executing":
                    raise PendingOperationError(
                        "OPERATION_NOT_COMMITTABLE",
                        "An identical task operation is already executing",
                    )
                operation_id = str(existing["id"])
                self._conn.execute(
                    "UPDATE feishu_pending_operations SET token_hash=?,status='pending',"
                    "error_code=NULL,expires_at=?,updated_at=? WHERE id=?",
                    (self._token_hash(token), now + ttl_seconds, now, operation_id),
                )
                self._event(operation_id, "reprepared", digest)
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM feishu_pending_operations WHERE id=?",
                    (operation_id,),
                ).fetchone()
                assert row is not None
                return self._row(row), token
            self._conn.execute(
                "INSERT INTO feishu_pending_operations "
                "(id,token_hash,tenant_id,principal_id,audience_id,session_key,"
                "channel_instance,instance_identity,operation_type,payload_json,"
                "payload_digest,idempotency_key,status,created_at,expires_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    self._token_hash(token),
                    tenant_id,
                    principal_id,
                    audience_id,
                    session_key,
                    channel_instance,
                    instance_identity,
                    "create_task",
                    raw,
                    digest,
                    idempotency_key,
                    "pending",
                    now,
                    now + ttl_seconds,
                    now,
                ),
            )
            self._event(operation_id, "prepared", digest)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM feishu_pending_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
            assert row is not None
            return self._row(row), token

    def claim(
        self,
        *,
        token: str,
        tenant_id: str,
        principal_id: str,
        audience_id: str,
        session_key: str,
        channel_instance: str,
        instance_identity: str,
    ) -> PendingOperation:
        now = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM feishu_pending_operations WHERE token_hash=?",
                    (self._token_hash(token),),
                ).fetchone()
                if row is None:
                    raise PendingOperationError(
                        "CONFIRMATION_REQUIRED", "Unknown confirmation token"
                    )
                operation = self._row(row)
                checks = (
                    ("tenant_id", tenant_id, "PRINCIPAL_MISMATCH"),
                    ("principal_id", principal_id, "PRINCIPAL_MISMATCH"),
                    ("audience_id", audience_id, "AUDIENCE_MISMATCH"),
                    ("session_key", session_key, "AUDIENCE_MISMATCH"),
                    ("channel_instance", channel_instance, "AUDIENCE_MISMATCH"),
                    ("instance_identity", instance_identity, "FEISHU_INSTANCE_CHANGED"),
                )
                for field, actual, code in checks:
                    if getattr(operation, field) != actual:
                        raise PendingOperationError(code, f"Confirmation {field} mismatch")
                if operation.status == "succeeded":
                    self._conn.commit()
                    return operation
                if operation.expires_at < now:
                    self._conn.execute(
                        "UPDATE feishu_pending_operations SET status='expired',updated_at=? "
                        "WHERE id=?",
                        (now, operation.id),
                    )
                    self._event(operation.id, "expired")
                    self._conn.commit()
                    raise PendingOperationError(
                        "CONFIRMATION_EXPIRED", "Confirmation token has expired"
                    )
                if operation.status not in {"pending", "failed_retryable"}:
                    raise PendingOperationError(
                        "OPERATION_NOT_COMMITTABLE",
                        f"Operation is currently {operation.status}",
                    )
                changed = self._conn.execute(
                    "UPDATE feishu_pending_operations SET status='executing',"
                    "error_code=NULL,updated_at=? WHERE id=? AND status IN "
                    "('pending','failed_retryable')",
                    (now, operation.id),
                ).rowcount
                if changed != 1:
                    raise PendingOperationError(
                        "OPERATION_NOT_COMMITTABLE", "Operation was claimed concurrently"
                    )
                self._event(operation.id, "executing")
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM feishu_pending_operations WHERE id=?",
                    (operation.id,),
                ).fetchone()
                assert row is not None
                return self._row(row)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def succeed(self, operation_id: str, *, task_id: str, task_url: str) -> PendingOperation:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE feishu_pending_operations SET status='succeeded',"
                "remote_task_id=?,remote_task_url=?,updated_at=? WHERE id=?",
                (task_id, task_url, now, operation_id),
            )
            self._event(operation_id, "succeeded", task_id)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM feishu_pending_operations WHERE id=?",
                (operation_id,),
            ).fetchone()
            assert row is not None
            return self._row(row)

    def fail(self, operation_id: str, *, code: str, retryable: bool) -> None:
        status = "failed_retryable" if retryable else "failed_terminal"
        with self._lock:
            self._conn.execute(
                "UPDATE feishu_pending_operations SET status=?,error_code=?,updated_at=? "
                "WHERE id=?",
                (status, code, int(time.time()), operation_id),
            )
            self._event(operation_id, status, code)
            self._conn.commit()

    def events(self, operation_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event FROM feishu_operation_events WHERE operation_id=? ORDER BY id",
                (operation_id,),
            ).fetchall()
        return [str(row["event"]) for row in rows]
