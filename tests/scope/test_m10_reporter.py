"""M10 统一 A/B 报告 (PRD §11-M10)。

验证 Reporter 把三条证据线（隔离安全 / 召回质量 / 并发公平）的改前 vs 改后
汇成一份自包含 HTML：
- 隔离安全：基线 forbidden_prompt_exposure > 0，隔离态翻转为 0。
- 召回质量：规模曲线返回、grep 随 N 跌破 SLA（拐点存在）、BM25 守住。
- 并发公平：U1-U5 每个用例都有基线/改后两份 LoadReport；U5 触发有界拒绝。
- 渲染：HTML 自包含（含 DOCTYPE、三段小节、翻转结论），无外部依赖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoscope.eval.reporter import (
    build_report,
    collect_concurrency,
    collect_isolation,
    generate,
    render_html,
)


def test_collect_isolation_flips_exposure_to_zero(tmp_path: Path):
    iso = collect_isolation(tmp_path / "ws", tmp_path / "mem.db")
    assert iso.n_attacks > 0
    # 记忆通道：基线洞真实存在（他人私聊 canary 进了攻击者 prompt），隔离态翻转为 0。
    assert iso.baseline_exposure > 0
    assert iso.isolated_exposure == 0
    # NanoScope (PRD_v4 §M11.4)：Recent History 通道同样双通道验收，改前 >0 改后翻转 0。
    assert iso.history_baseline_exposure > 0
    assert iso.history_isolated_exposure == 0


async def test_collect_concurrency_covers_all_cases_and_bounds_overload():
    cases = await collect_concurrency()
    names = {ab.case for ab in cases}
    assert {"U1_ramp", "U3_slow_isolation", "U5_overload"} <= names
    # 每个用例都有基线/改后两份报告。
    for ab in cases:
        assert ab.baseline.n_total == ab.improved.n_total
    # U5 洪峰：基线无界从不拒绝，改后有界背压触发拒绝。
    u5 = next(ab for ab in cases if ab.case == "U5_overload")
    assert u5.baseline.n_rejected == 0
    assert u5.improved.n_rejected > 0


async def test_build_report_carries_three_evidence_lines(tmp_path: Path):
    report = await build_report(tmp_path / "ws", tmp_path / "mem.db")
    assert report.isolation.isolated_exposure == 0
    assert report.retrieval  # 规模曲线非空
    # grep 在某个规模跌破 SLA（拐点存在）；BM25 更高。
    assert any(p.grep.recall_at_k < 0.8 for p in report.retrieval)
    last = report.retrieval[-1]
    assert last.bm25.recall_at_k >= last.grep.recall_at_k
    assert report.concurrency


async def test_render_html_is_self_contained(tmp_path: Path):
    report = await build_report(tmp_path / "ws", tmp_path / "mem.db")
    doc = render_html(report)
    assert doc.startswith("<!DOCTYPE html>")
    # 三条线小节都在。
    assert "隔离安全 A/B" in doc
    assert "召回质量 A/B" in doc
    assert "并发公平 A/B" in doc
    # 翻转结论可见。
    assert "安全不变量成立" in doc
    # NanoScope (PRD_v4 §M11.4)：双通道隔离段（含 Recent History）渲染出来。
    assert "Recent History 通道" in doc
    # 每个压测用例标题都渲染出来。
    for ab in report.concurrency:
        assert ab.case in doc


def test_generate_writes_html_file(tmp_path: Path):
    out = generate(tmp_path / "out" / "report.html", tmp_path / "ws", tmp_path / "mem.db")
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "NanoScope 统一 A/B 报告" in content
    assert "forbidden_prompt_exposure" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
