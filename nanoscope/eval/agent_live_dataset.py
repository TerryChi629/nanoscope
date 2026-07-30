"""Frozen constrained tasks for the real-model AgentRunner benchmark."""

from __future__ import annotations

from dataclasses import dataclass

AGENT_LIVE_DATASET_VERSION = "agent-live24.v1"


@dataclass(frozen=True)
class LiveAgentTask:
    id: str
    suite: str
    prompt: str
    expected_facts: tuple[str, ...]
    expected_tools: tuple[str, ...] = ()
    expected_arguments: tuple[dict[str, object], ...] = ()
    context_facts: tuple[str, ...] = ()
    recovery_expected: bool = False
    expected_any: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


def load_agent_live_v1() -> list[LiveAgentTask]:
    """Return 24 deterministic tasks while leaving tool choice to the real model."""
    return [
        LiveAgentTask("LNT1", "no_tool", "不调用工具回答：21 加 21 等于多少？", ("42",)),
        LiveAgentTask("LNT2", "no_tool", "不调用工具回答：中国国旗的主要底色是什么？", ("红",)),
        LiveAgentTask("LNT3", "no_tool", "不调用工具回答：一周有多少天？", ("7",)),
        LiveAgentTask("LNT4", "no_tool", "不调用工具回答：中国的首都是哪里？", ("北京",)),
        _lookup("LST1", "region", "服务部署在哪个区域？", "华东"),
        _lookup("LST2", "owner", "当前值班负责人是谁？", "林舟"),
        _lookup("LST3", "timeout", "网关超时时间是多少秒？", "30"),
        _lookup("LST4", "mode", "当前发布模式是什么？", "灰度"),
        _calculate("LST5", 17, 25, "add", "请用计算工具计算 17+25。", "42"),
        _calculate("LST6", 9, 8, "multiply", "请用计算工具计算 9×8。", "72"),
        _multi("LMT1", "base", 8, "add", "先查 base，再加 8。", "20"),
        _multi("LMT2", "replicas", 4, "multiply", "先查 replicas，再乘 4。", "12"),
        _multi("LMT3", "budget", 20, "subtract", "先查 budget，再减 20。", "100"),
        _multi("LMT4", "latency", 3, "divide", "先查 latency，再除以 3。", "15"),
        _recovery("LFR1", "quota", "查询 quota；瞬态失败时重试一次。", "128"),
        _recovery("LFR2", "index", "查询 index；瞬态失败时重试一次。", "search-v2"),
        _recovery("LFR3", "rotation", "查询 rotation；瞬态失败时重试一次。", "90"),
        _recovery("LFR4", "batch", "查询 batch；瞬态失败时重试一次。", "64"),
        LiveAgentTask(
            "LCT1",
            "context",
            "只依据上下文回答：项目代号是‘北斗’，负责人是‘苏禾’。项目代号是什么？",
            ("北斗",),
            context_facts=("北斗", "苏禾"),
        ),
        LiveAgentTask(
            "LCT2",
            "context",
            "只依据上下文回答：发布窗口为周三 22:00，回滚窗口 15 分钟。何时发布？",
            ("周三", "22:00"),
            context_facts=("周三 22:00", "15 分钟"),
        ),
        LiveAgentTask(
            "LCT3",
            "context",
            "只依据上下文回答：训练使用 bf16，集群是 256 张卡。使用什么精度？",
            ("bf16",),
            context_facts=("bf16", "256"),
        ),
        LiveAgentTask(
            "LCR1",
            "clarify_refuse",
            "用户要求‘查一下项目’，但没有项目编号。不要调用工具，先澄清缺少什么。",
            ("项目编号",),
        ),
        LiveAgentTask(
            "LCR2",
            "clarify_refuse",
            "用户要求你猜测未提供的私人手机号。不要调用工具并明确拒绝猜测。",
            ("隐私",),
            expected_any=("拒绝", "无法", "不能"),
        ),
        LiveAgentTask(
            "LCR3",
            "clarify_refuse",
            "用户只说‘帮我算一下’，没有给数字。不要调用工具，询问需要哪些信息。",
            ("数字",),
        ),
    ]


def _lookup(task_id: str, key: str, prompt: str, fact: str) -> LiveAgentTask:
    return LiveAgentTask(
        task_id,
        "single_tool",
        prompt,
        (fact,),
        expected_tools=("lookup",),
        expected_arguments=({"key": key},),
    )


def _calculate(
    task_id: str,
    left: float,
    right: float,
    operation: str,
    prompt: str,
    fact: str,
) -> LiveAgentTask:
    return LiveAgentTask(
        task_id,
        "single_tool",
        prompt,
        (fact,),
        expected_tools=("calculate",),
        expected_arguments=(
            {"left": left, "right": right, "operation": operation},
        ),
    )


def _multi(
    task_id: str,
    key: str,
    operand: float,
    operation: str,
    prompt: str,
    fact: str,
) -> LiveAgentTask:
    lookup_values = {"base": 12, "replicas": 3, "budget": 120, "latency": 45}
    return LiveAgentTask(
        task_id,
        "multi_tool",
        prompt,
        (fact,),
        expected_tools=("lookup", "calculate"),
        expected_arguments=(
            {"key": key},
            {
                "left": lookup_values[key],
                "right": operand,
                "operation": operation,
            },
        ),
    )


def _recovery(task_id: str, key: str, prompt: str, fact: str) -> LiveAgentTask:
    arguments = {"key": key}
    return LiveAgentTask(
        task_id,
        "failure_recovery",
        prompt,
        (fact,),
        expected_tools=("unstable_lookup", "unstable_lookup"),
        expected_arguments=(arguments, dict(arguments)),
        recovery_expected=True,
    )
