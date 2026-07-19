"""M2 记忆数据模型 + Repository 单入口 (PRD §6)。

安全红线：
- 可见性 = 确定性硬过滤（SQL WHERE + DB CHECK），RAG 只排序、绝不决定能不能看。
- 检索唯一入口 `Repository.search_visible(ctx, ...)`，上层禁止自拼 WHERE。
- 写入时打标 tenant_id + scope + owner_id；缺字段被 DB CHECK 拒绝（fail-closed）。
- 个人记忆仅 `audience_type='dm'` 时召回（DM 门，谓词见 §6）。

安全不变量（PRD §8）：任何返回给 (Principal, Audience) 的记忆，必须满足授权谓词
can_read_memory；写入必须满足 can_write_memory。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from nanoscope.identity import AUDIENCE_DM, SecurityContext

SCOPE_USER = "user"
SCOPE_ORG = "org"
_VALID_SCOPES = {SCOPE_USER, SCOPE_ORG}

# fail-closed：非法组合直接写不进（PRD §6 SQL CHECK）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  scope        TEXT NOT NULL CHECK (scope IN ('user','org')),
  owner_id     TEXT,
  audience_id  TEXT,
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  source_type  TEXT NOT NULL,
  source_ref   TEXT,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  CHECK ((scope='user' AND owner_id IS NOT NULL) OR (scope='org' AND owner_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id, scope, owner_id);
"""


@dataclass(frozen=True)
class MemoryRecord:
    """一条结构化长期记忆（从 SQLite 读回的可见项）。"""

    id: str
    tenant_id: str
    scope: str
    owner_id: str | None
    content: str
    source_type: str
    created_at: int
    audience_id: str | None = None
    source_ref: str | None = None


class Repository:
    """结构化长期记忆的唯一动态写入口 + 唯一检索入口。

    `search_visible` 内部强制授权谓词，上层拿不到拼 WHERE 的机会。
    `add` 从 SecurityContext 注入 tenant/owner，模型无法伪造。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _content_hash(tenant_id: str, scope: str, owner_id: str | None, content: str) -> str:
        raw = f"{tenant_id}\x00{scope}\x00{owner_id or ''}\x00{content}".encode()
        return hashlib.sha256(raw).hexdigest()

    def add(
        self,
        ctx: SecurityContext,
        *,
        content: str,
        scope: str,
        source_type: str,
        source_ref: str | None = None,
    ) -> MemoryRecord:
        """写入一条记忆，tenant/owner 由 ctx 运行时注入（不进工具 schema）。

        - scope='user'：owner_id = ctx.principal_id（个人记忆）。
        - scope='org'：owner_id 恒为 NULL（tenant 内共享）。
        非法 scope 直接抛错；非法组合由 DB CHECK 兜底 fail-closed。
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"非法 scope: {scope!r}")
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        owner_id = ctx.principal_id if scope == SCOPE_USER else None
        now = int(time.time())
        rec = MemoryRecord(
            id=uuid.uuid4().hex,
            tenant_id=ctx.tenant_id,
            scope=scope,
            owner_id=owner_id,
            content=content,
            source_type=source_type,
            created_at=now,
            source_ref=source_ref,
        )
        content_hash = self._content_hash(ctx.tenant_id, scope, owner_id, content)
        # 非法组合（user 缺 owner / org 带 owner）在此被 DB CHECK 拒绝。
        self._conn.execute(
            "INSERT INTO memories "
            "(id, tenant_id, scope, owner_id, audience_id, content, content_hash, "
            " status, source_type, source_ref, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.id, rec.tenant_id, rec.scope, rec.owner_id, None,
                rec.content, content_hash, "active", rec.source_type,
                rec.source_ref, now, now,
            ),
        )
        self._conn.commit()
        return rec

    def search_visible(
        self,
        ctx: SecurityContext,
        query: str | None = None,
        top_k: int = 20,
    ) -> list[MemoryRecord]:
        """在 ctx 可见范围内检索记忆。授权谓词在此内部强制（PRD §6）。

        WHERE tenant_id = ctx.tenant_id
          AND ( scope='org'
                OR (scope='user' AND owner_id=ctx.principal_id AND ctx.is_dm) )

        MVP：全量返回本人可见项，相关性排序（BM25）留 P1。`query` 目前仅占位。
        """
        # DM 门：非私聊时个人记忆整条被挡在召回之前。
        if ctx.audience_type == AUDIENCE_DM:
            where = (
                "tenant_id = ? AND status = 'active' AND ("
                "scope = 'org' OR (scope = 'user' AND owner_id = ?)"
                ")"
            )
            params: tuple = (ctx.tenant_id, ctx.principal_id)
        else:
            where = "tenant_id = ? AND status = 'active' AND scope = 'org'"
            params = (ctx.tenant_id,)
        rows = self._conn.execute(
            "SELECT id, tenant_id, scope, owner_id, audience_id, content, "
            "source_type, source_ref, created_at "
            f"FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, top_k),
        ).fetchall()
        return [
            MemoryRecord(
                id=r["id"],
                tenant_id=r["tenant_id"],
                scope=r["scope"],
                owner_id=r["owner_id"],
                content=r["content"],
                source_type=r["source_type"],
                created_at=r["created_at"],
                audience_id=r["audience_id"],
                source_ref=r["source_ref"],
            )
            for r in rows
        ]
