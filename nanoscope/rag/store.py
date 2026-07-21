"""M18 · 文档 chunk 存储与授权单入口 (PRD_v4 §M18.2)。

对齐 `Repository`（M2）的授权范式，但服务不同数据（长文档 chunk）、不同 scope 分级
（新增 `project` 部门 ACL 维度）。红线：

- chunk 的 `tenant/scope/owner_id/acl_group` 标签由 **ingestion 运行时注入**，
  不进任何工具 schema、模型不可伪造（复用 M3 做法）。
- scope CHECK fail-closed：非法组合直接写不进。
- `visible_where(ctx)` 是唯一决定"能不能看"的地方——检索排序绝不参与。

新增 scope 分级（相对记忆的 {user,org}）：
- user   ：个人文档，owner_id = principal，仅本人可见（DM 语境）。
- org    ：租户内全员共享，owner_id/acl_group 均 NULL。
- project：部门/项目文档，acl_group = 项目标识，仅该 project 成员可见（部门 ACL）。
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
SCOPE_PROJECT = "project"
_VALID_SCOPES = {SCOPE_USER, SCOPE_ORG, SCOPE_PROJECT}

# fail-closed：scope 与归属字段的合法组合由 DB CHECK 强制。
# - user   : owner_id 非空、acl_group 空
# - org    : owner_id 空、  acl_group 空
# - project: owner_id 空、  acl_group 非空
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  title       TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_chunks (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  scope        TEXT NOT NULL CHECK (scope IN ('user','org','project')),
  owner_id     TEXT,
  acl_group    TEXT,
  source_doc_id TEXT NOT NULL,
  chunk_index  INTEGER NOT NULL,
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  CHECK (
    (scope='user'    AND owner_id IS NOT NULL AND acl_group IS NULL) OR
    (scope='org'     AND owner_id IS NULL     AND acl_group IS NULL) OR
    (scope='project' AND owner_id IS NULL     AND acl_group IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant
  ON doc_chunks(tenant_id, scope, owner_id, acl_group);
"""


@dataclass(frozen=True)
class DocChunkRecord:
    """一条文档 chunk（隔离后可见项）。"""

    id: str
    tenant_id: str
    scope: str
    owner_id: str | None
    acl_group: str | None
    source_doc_id: str
    chunk_index: int
    content: str
    created_at: int


class ChunkStore:
    """文档 chunk 的唯一写入口 + 唯一授权入口（与记忆检索平行，不替换）。

    `add_chunk` 从 SecurityContext + 显式 scope/acl_group 注入归属，模型无法伪造。
    `visible_where` 内部强制授权谓词，上层拿不到自拼 WHERE 的机会。
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
    def _content_hash(tenant_id: str, scope: str, key: str, content: str) -> str:
        raw = f"{tenant_id}\x00{scope}\x00{key}\x00{content}".encode()
        return hashlib.sha256(raw).hexdigest()

    def register_document(self, ctx: SecurityContext, *, title: str) -> str:
        """登记一篇文档，返回 doc_id。"""
        doc_id = uuid.uuid4().hex
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO documents (id, tenant_id, title, created_at) VALUES (?,?,?,?)",
            (doc_id, ctx.tenant_id, title, now),
        )
        self._conn.commit()
        return doc_id

    def add_chunk(
        self,
        ctx: SecurityContext,
        *,
        source_doc_id: str,
        chunk_index: int,
        content: str,
        scope: str,
        acl_group: str | None = None,
    ) -> DocChunkRecord:
        """写入一条 chunk，tenant/owner 由 ctx 运行时注入（不进工具 schema）。

        - scope='user'   ：owner_id = ctx.principal_id，acl_group 强制 None。
        - scope='org'    ：owner_id / acl_group 均 None（租户共享）。
        - scope='project'：acl_group = 传入项目标识（必需），owner_id 强制 None。
        非法 scope 抛错；缺 project 的 acl_group、或非法组合由 DB CHECK 兜底 fail-closed。
        """
        if scope not in _VALID_SCOPES:
            raise ValueError(f"非法 scope: {scope!r}")
        if not content or not content.strip():
            raise ValueError("content 不能为空")
        if scope == SCOPE_PROJECT and not acl_group:
            raise ValueError("scope='project' 必须提供 acl_group（fail-closed）")

        owner_id = ctx.principal_id if scope == SCOPE_USER else None
        group = acl_group if scope == SCOPE_PROJECT else None
        now = int(time.time())
        rec = DocChunkRecord(
            id=uuid.uuid4().hex,
            tenant_id=ctx.tenant_id,
            scope=scope,
            owner_id=owner_id,
            acl_group=group,
            source_doc_id=source_doc_id,
            chunk_index=chunk_index,
            content=content,
            created_at=now,
        )
        key = owner_id or group or ""
        content_hash = self._content_hash(ctx.tenant_id, scope, key, content)
        self._conn.execute(
            "INSERT INTO doc_chunks "
            "(id, tenant_id, scope, owner_id, acl_group, source_doc_id, chunk_index, "
            " content, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rec.id, rec.tenant_id, rec.scope, rec.owner_id, rec.acl_group,
                rec.source_doc_id, rec.chunk_index, rec.content, content_hash, now,
            ),
        )
        self._conn.commit()
        return rec

    def visible_where(self, ctx: SecurityContext) -> tuple[str, tuple]:
        """构造 chunk 可见性 WHERE（唯一决定"能不能看"的地方，fail-closed）。

        可见集合 = 同 tenant 内：
          - org 共享；
          - 本人的 user 文档（仅 DM 语境，与记忆 DM 门一致）；
          - 本人所属 project 的 project 文档（acl_group ∈ ctx.roles 视为部门成员）。

        本子包用 `SecurityContext.roles` 承载"用户所属项目/部门集合"（前向兼容字段，
        无需改 identity schema）。缺字段/无归属的 chunk 在授权 WHERE 下天然不命中。
        """
        groups = tuple(ctx.roles) if ctx.roles else ()
        clauses = ["scope = 'org'"]
        params: list = [ctx.tenant_id]
        if ctx.audience_type == AUDIENCE_DM:
            clauses.append("(scope = 'user' AND owner_id = ?)")
            params.append(ctx.principal_id)
        if groups:
            placeholders = ",".join("?" for _ in groups)
            clauses.append(f"(scope = 'project' AND acl_group IN ({placeholders}))")
            params.extend(groups)
        where = "tenant_id = ? AND (" + " OR ".join(clauses) + ")"
        return where, tuple(params)

    def visible_chunks(self, ctx: SecurityContext) -> list[DocChunkRecord]:
        """返回 ctx 可见的全部 chunk（授权 WHERE 硬过滤；检索器只在此集合内排序）。"""
        where, params = self.visible_where(ctx)
        rows = self._conn.execute(
            "SELECT id, tenant_id, scope, owner_id, acl_group, source_doc_id, "
            "chunk_index, content, created_at "
            f"FROM doc_chunks WHERE {where} ORDER BY created_at ASC, id ASC",
            params,
        ).fetchall()
        return [
            DocChunkRecord(
                id=r["id"], tenant_id=r["tenant_id"], scope=r["scope"],
                owner_id=r["owner_id"], acl_group=r["acl_group"],
                source_doc_id=r["source_doc_id"], chunk_index=r["chunk_index"],
                content=r["content"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def all_chunks_unfiltered(self) -> list[DocChunkRecord]:
        """★ 仅用于评测对照：绕过授权 WHERE 取全库 chunk。

        线上检索绝不调用此方法——它专门用于 D1「关掉授权 WHERE 证明泄露 >0」的
        可证伪对照，以及 post-filter 反面策略。生产路径只走 `visible_chunks`。
        """
        rows = self._conn.execute(
            "SELECT id, tenant_id, scope, owner_id, acl_group, source_doc_id, "
            "chunk_index, content, created_at FROM doc_chunks ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [
            DocChunkRecord(
                id=r["id"], tenant_id=r["tenant_id"], scope=r["scope"],
                owner_id=r["owner_id"], acl_group=r["acl_group"],
                source_doc_id=r["source_doc_id"], chunk_index=r["chunk_index"],
                content=r["content"], created_at=r["created_at"],
            )
            for r in rows
        ]
