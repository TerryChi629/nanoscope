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
from typing import Any

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

# M7 (PRD §14 / P1-A.1)：FTS5 trigram 影子索引，仅承担"可见集合内的相关性排序"。
# id UNINDEXED 只做 join 键、不进倒排；content 用 trigram 分词对中文（CJK）友好。
# 红线：BM25 只排序，绝不决定可见性——可见性仍由 search_visible 的确定性 WHERE 强制。
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  id UNINDEXED,
  tokenize='trigram'
);
"""


def _build_trigram_match(query: str) -> str | None:
    """把自由文本 query 拆成 3-gram 的 OR 匹配式（trigram 子串召回）。

    trigram 索引以 3 字符片段建倒排，故 query 需拆成 ≥3 字的片段才能命中。
    每个 gram 用双引号包裹并转义内部双引号，避免 FTS5 语法注入/报错；
    query 去重保序。<3 字符（无 trigram）返回 None，由上层回退时间序。
    """
    text = query.strip()
    if len(text) < 3:
        return None
    grams: list[str] = []
    seen: set[str] = set()
    for i in range(len(text) - 2):
        gram = text[i : i + 3]
        if gram.isspace() or gram in seen:
            continue
        seen.add(gram)
        grams.append('"' + gram.replace('"', '""') + '"')
    if not grams:
        return None
    return " OR ".join(grams)


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
        # M7：FTS5 影子索引。若本机 SQLite 未编译 FTS5，降级为无排序（时间序），
        # 隔离正确性不受影响（BM25 只排序、不决定可见性）。
        try:
            self._conn.executescript(_FTS_SCHEMA)
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False
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
        # M7：同步影子倒排。FTS 只用于排序，与主表授权无关。
        if self._fts_enabled:
            self._conn.execute(
                "INSERT INTO memories_fts (id, content) VALUES (?, ?)",
                (rec.id, rec.content),
            )
        self._conn.commit()
        return rec

    def _visibility_where(self, ctx: SecurityContext) -> tuple[str, tuple]:
        """构造可见性 WHERE（PRD §6 授权谓词）。DM 门在此强制。

        这是唯一决定"能不能看"的地方——检索排序绝不参与。
        """
        if ctx.audience_type == AUDIENCE_DM:
            where = (
                "tenant_id = ? AND status = 'active' AND ("
                "scope = 'org' OR (scope = 'user' AND owner_id = ?)"
                ")"
            )
            return where, (ctx.tenant_id, ctx.principal_id)
        return "tenant_id = ? AND status = 'active' AND scope = 'org'", (ctx.tenant_id,)

    def search_visible(
        self,
        ctx: SecurityContext,
        query: str | None = None,
        top_k: int = 20,
        *,
        ranker: Any | None = None,
        candidate_k: int | None = None,
    ) -> list[MemoryRecord]:
        """在 ctx 可见范围内检索记忆。授权谓词在此内部强制（PRD §6）。

        WHERE tenant_id = ctx.tenant_id
          AND ( scope='org'
                OR (scope='user' AND owner_id=ctx.principal_id AND ctx.is_dm) )

        M7 (PRD §14)：给定 query 时在可见集合内做 BM25 相关性排序。红线：
        可见性由主表 WHERE 确定性强制，FTS 仅 JOIN 进来做排序——即便倒排里混入
        他人条目，也会被主表授权 WHERE 过滤掉，RAG 永不决定"能不能看"。
        无 query / FTS 不可用 / query 无 trigram 时回退时间序（MVP-0 行为）。
        """
        where, params = self._visibility_where(ctx)
        match = _build_trigram_match(query) if (query and self._fts_enabled) else None
        limit = max(top_k, candidate_k or top_k) if ranker is not None else top_k

        if match is not None:
            # BM25 排序：主表(授权 WHERE) JOIN 影子倒排(MATCH)，按 bm25 升序（越小越相关）。
            rows = self._conn.execute(
                "SELECT m.id, m.tenant_id, m.scope, m.owner_id, m.audience_id, "
                "m.content, m.source_type, m.source_ref, m.created_at "
                "FROM memories m JOIN memories_fts f ON m.id = f.id "
                f"WHERE ({where}) AND f.memories_fts MATCH ? "
                "ORDER BY bm25(f.memories_fts) LIMIT ?",
                (*params, match, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, tenant_id, scope, owner_id, audience_id, content, "
                "source_type, source_ref, created_at "
                f"FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        records = [
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
        if ranker is not None and query:
            return ranker.rank(query, records, top_k=top_k)
        return records[:top_k]
