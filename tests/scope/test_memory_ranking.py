from __future__ import annotations

from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory.ranking import MemoryRanker
from nanoscope.memory.repository import SCOPE_USER, MemoryRecord, Repository


def _record(record_id: str, content: str, created_at: int) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        tenant_id="org",
        scope=SCOPE_USER,
        owner_id="org:feishu:alice",
        content=content,
        source_type="test",
        created_at=created_at,
    )


def _context(user: str) -> SecurityContext:
    return SecurityContext(
        tenant_id="org",
        principal_id=f"org:feishu:{user}",
        session_key=f"feishu:{user}",
        audience_type=AUDIENCE_DM,
    )


def test_memory_ranker_prefers_fresh_record_when_relevance_is_equal():
    now = 2_000_000
    ranker = MemoryRanker(
        relevance_weight=0.5,
        freshness_weight=0.5,
        redundancy_weight=0.0,
        half_life_days=1.0,
    )
    records = [
        _record("old", "Python backend preference", now - 10 * 86_400),
        _record("new", "Python backend preference", now - 60),
    ]

    ranked = ranker.rank("Python backend", records, top_k=2, now=now)

    assert [record.id for record in ranked] == ["new", "old"]


def test_memory_ranker_mmr_reduces_duplicate_results():
    now = 2_000_000
    ranker = MemoryRanker(
        relevance_weight=1.0,
        freshness_weight=0.0,
        redundancy_weight=0.8,
    )
    records = [
        _record("dup-a", "Python backend FastAPI", now),
        _record("dup-b", "Python backend FastAPI", now - 1),
        _record("diverse", "Python backend uses PostgreSQL database", now - 2),
    ]

    ranked = ranker.rank("Python backend", records, top_k=2, now=now)

    assert ranked[0].id in {"dup-a", "dup-b"}
    assert ranked[1].id == "diverse"


def test_repository_authorization_precedes_optional_memory_ranking(tmp_path):
    repository = Repository(tmp_path / "memory.db")
    alice = _context("alice")
    bob = _context("bob")
    repository.add(
        alice,
        content="Alice private Python preference",
        scope=SCOPE_USER,
        source_type="test",
    )
    repository.add(
        bob,
        content="Bob private Python secret",
        scope=SCOPE_USER,
        source_type="test",
    )

    results = repository.search_visible(
        alice,
        query="Python",
        top_k=5,
        ranker=MemoryRanker(),
        candidate_k=20,
    )

    assert len(results) == 1
    assert results[0].owner_id == alice.principal_id
    assert "Bob" not in results[0].content
    repository.close()
