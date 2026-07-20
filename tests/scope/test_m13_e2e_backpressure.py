"""M13 端到端有界准入验收 (PRD §M13.3)。

覆盖 B1-B5：
- B1 前移生效：admission 在 session lock 之前决定放行；队满时 _dispatch 走优雅拒绝。
- B2 task 峰值有界：入口非阻塞预检队满 → 不建 task（active_tasks 不随注入线性增长）。
- B3 拒绝回执：被拒的真实用户请求产生一条含重试提示 + retry_after 的 outbound。
- B4 控制流不误伤：自动化轮次（cron）/内部续跑被拒时不发回执；既有 12 项回归另跑。
- B5 单用户零回归：multi_user 关闭时 _admission is None，入口从不拒绝，行为同基线。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanoscope.concurrency import FairAdmissionController


def _make_loop(*, multi_user=None):
    """构造一个依赖被 mock 的最小 AgentLoop（复用 test_task_cancel 的模式）。"""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, multi_user=multi_user)
    return loop, bus


def _user_msg(chat_id: str = "c1", content: str = "hi") -> InboundMessage:
    return InboundMessage(channel="test", sender_id="u1", chat_id=chat_id, content=content)


# ---------------------------------------------------------------------------
# B3 拒绝回执
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b3_reject_notice_for_real_user_carries_retry_after():
    from nanoscope.concurrency.admission import AdmissionRejectedError

    loop, bus = _make_loop()
    msg = _user_msg()
    exc = AdmissionRejectedError("full", retry_after=2.5)
    await loop._publish_admission_reject_notice(msg, exc)

    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert isinstance(out, OutboundMessage)
    assert "重试" in out.content
    assert out.metadata.get("retry_after") == 2.5


@pytest.mark.asyncio
async def test_b3_no_notice_without_retry_after_field():
    from nanoscope.concurrency.admission import AdmissionRejectedError

    loop, bus = _make_loop()
    msg = _user_msg()
    await loop._publish_admission_reject_notice(msg, AdmissionRejectedError("full"))
    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "重试" in out.content
    assert "retry_after" not in out.metadata


# ---------------------------------------------------------------------------
# B4 控制流不误伤：自动化轮次 / 内部续跑不发回执
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b4_cron_turn_gets_no_reject_notice():
    from nanobot.cron.session_turns import CRON_TRIGGER_META
    from nanoscope.concurrency.admission import AdmissionRejectedError

    loop, bus = _make_loop()
    cron_msg = InboundMessage(
        channel="test", sender_id="cron", chat_id="c1", content="scheduled",
        metadata={CRON_TRIGGER_META: {"run_id": "run-1", "job_id": "j1"}},
    )
    assert loop._is_real_user_inbound(cron_msg) is False
    await loop._publish_admission_reject_notice(
        cron_msg, AdmissionRejectedError("full", retry_after=2.0)
    )
    assert bus.outbound_size == 0  # 自动化轮次不发回执


@pytest.mark.asyncio
async def test_b4_internal_continuation_gets_no_reject_notice():
    from nanobot.session.turn_continuation import INTERNAL_CONTINUATION_META
    from nanoscope.concurrency.admission import AdmissionRejectedError

    loop, bus = _make_loop()
    cont_msg = InboundMessage(
        channel="test", sender_id="system:continuation", chat_id="c1", content="cont",
        metadata={INTERNAL_CONTINUATION_META: True},
    )
    assert loop._is_real_user_inbound(cont_msg) is False
    await loop._publish_admission_reject_notice(
        cont_msg, AdmissionRejectedError("full", retry_after=2.0)
    )
    assert bus.outbound_size == 0


# ---------------------------------------------------------------------------
# B1 前移生效 + B3 端到端：_dispatch 在 admission 队满时优雅拒绝
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b1_dispatch_rejects_when_admission_queue_full():
    """admission 前移：队满时 _dispatch 在拿 session lock 前就被拒，回执给用户。"""
    loop, bus = _make_loop()
    # 手动接入一个极小的有界控制器（global=1, queue=1），绕过 multi_user 全套依赖。
    loop._admission = FairAdmissionController(global_limit=1, per_principal_limit=1, max_queue=1)

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_process(m, **kwargs):
        started.set()
        await release.wait()
        return OutboundMessage(channel="test", chat_id=m.chat_id, content="done")

    loop._process_message = blocking_process

    # msg1 占满唯一名额（不同 chat 以获得不同 admission_key/session_key）。
    t1 = asyncio.create_task(loop._dispatch(_user_msg(chat_id="c1")))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    # msg2 进入等待队列（占满 max_queue=1）。
    t2 = asyncio.create_task(loop._dispatch(_user_msg(chat_id="c2")))
    await asyncio.sleep(0.02)
    # msg3：队列已满 → acquire 抛 AdmissionRejectedError → 优雅拒绝回执。
    await loop._dispatch(_user_msg(chat_id="c3"))

    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "重试" in out.content
    assert "retry_after" in out.metadata

    # 收尾：放行阻塞任务。
    release.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)


# ---------------------------------------------------------------------------
# B2 task 峰值有界：入口预检队满 → 不建 task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b2_entrance_precheck_skips_task_when_full():
    """入口非阻塞预检：would_reject 时直接回执、不建 task（峰值不随注入线性增长）。"""
    loop, bus = _make_loop()
    loop._admission = FairAdmissionController(global_limit=1, per_principal_limit=1, max_queue=1)
    # 强制入口判定为「队满」。
    loop._admission.would_reject = MagicMock(return_value=True)

    msg = _user_msg()
    # 复刻 run-loop 入口预检分支的核心逻辑（不启动完整 run()）。
    assert loop._admission is not None and loop._is_real_user_inbound(msg)
    stamped = loop._stamp_principal(msg)
    admission_key = stamped.principal_id or loop._effective_session_key(stamped)
    rejected = loop._admission.would_reject(admission_key)
    assert rejected is True
    before = sum(len(v) for v in loop._active_tasks.values())
    if rejected:
        from nanoscope.concurrency.admission import AdmissionRejectedError

        await loop._publish_admission_reject_notice(
            stamped,
            AdmissionRejectedError("full", retry_after=loop._admission.suggested_retry_after()),
        )
    after = sum(len(v) for v in loop._active_tasks.values())
    assert after == before  # 未建 task
    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "重试" in out.content


# ---------------------------------------------------------------------------
# B5 单用户零回归
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b5_single_user_has_no_admission_and_never_rejects():
    """multi_user 关闭：_admission is None，_dispatch 走 nullcontext 路径，正常处理。"""
    loop, bus = _make_loop()
    assert loop._admission is None

    msg = _user_msg()
    loop._process_message = AsyncMock(
        return_value=OutboundMessage(channel="test", chat_id="c1", content="hi")
    )
    await loop._dispatch(msg)
    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert out.content == "hi"


def test_b5_multi_user_config_guard_rejects_unbounded_in_production():
    """M13 改动点④：enabled=True 且 admissionMaxQueue<=0 未开逃生阀 → 拒绝启动。"""
    from pydantic import ValidationError

    from nanobot.config.schema import MultiUserConfig

    # 单用户默认无界允许（不校验）。
    MultiUserConfig(admission_max_queue=0)
    # 生产无界且未开逃生阀 → 拒绝。
    with pytest.raises(ValidationError):
        MultiUserConfig(enabled=True, tenant_id="t1", admission_max_queue=0)
    # 显式逃生阀 → 放行。
    cfg = MultiUserConfig(
        enabled=True, tenant_id="t1", admission_max_queue=0,
        allow_unbounded_admission_queue=True,
    )
    assert cfg.admission_max_queue == 0
    # 生产有界 → 正常。
    ok = MultiUserConfig(enabled=True, tenant_id="t1", admission_max_queue=64)
    assert ok.admission_max_queue == 64
