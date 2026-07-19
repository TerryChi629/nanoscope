"""M5 闭合 Dream 后门测试 (PRD §7/§11-M5 验收)。

验收点：
- multi_user 下 Dream 工具集的可写集合不含 MEMORY/USER/SOUL，写入被拒。
- multi_user 下 USER.md 停止注入 system prompt；SOUL.md/AGENTS.md 仍注入。
- 单用户（未隔离）保持基线：Dream 可写三文件、USER.md 正常注入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore


def _isolated_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    store.multi_user_isolation = True
    return store


@pytest.mark.asyncio
async def test_dream_cannot_write_durable_files_under_isolation(tmp_path: Path):
    store = _isolated_store(tmp_path)
    store.memory_file.write_text("# facts\n- Project X active\n", encoding="utf-8")
    store.soul_file.write_text("Helpful\n", encoding="utf-8")
    store.user_file.write_text("name: Alice\n", encoding="utf-8")

    tools = store.build_dream_tools()

    memory_result = await tools.execute(
        "apply_patch",
        {
            "edits": [
                {
                    "path": "memory/MEMORY.md",
                    "action": "replace",
                    "old_text": "Project X active",
                    "new_text": "LEAKED",
                }
            ]
        },
    )
    soul_result = await tools.execute(
        "edit_file",
        {"path": "SOUL.md", "old_text": "Helpful", "new_text": "LEAKED"},
    )

    # 写入被拒（不在可写集），三文件内容原样。
    assert "LEAKED" not in store.memory_file.read_text(encoding="utf-8")
    assert "LEAKED" not in store.soul_file.read_text(encoding="utf-8")
    assert getattr(memory_result, "is_error", False) or "LEAKED" not in memory_result
    assert getattr(soul_result, "is_error", False) or "Successfully edited" not in soul_result


@pytest.mark.asyncio
async def test_dream_can_still_write_skills_under_isolation(tmp_path: Path):
    """隔离只闭合三文件后门，skills 仍可写（Dream 蒸馏能力不整体废掉）。"""
    store = _isolated_store(tmp_path)
    tools = store.build_dream_tools()
    result = await tools.execute(
        "write_file",
        {
            "path": "skills/demo/SKILL.md",
            "content": "---\nname: demo\ndescription: Demo.\n---\n\nUse when needed.\n",
        },
    )
    assert "Successfully wrote" in result


def test_baseline_dream_can_write_durable_files(tmp_path: Path):
    """未隔离（单用户）保持基线：三文件在可写集内。"""
    store = MemoryStore(tmp_path)
    tools = store.build_dream_tools()
    assert set(tools.tool_names) == {"apply_patch", "edit_file", "read_file", "write_file"}


def test_user_md_not_injected_under_isolation(tmp_path: Path):
    """multi_user 下 USER.md 停止注入；SOUL.md 仍注入。"""
    (tmp_path / "USER.md").write_text("SECRET-USER-FACT\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("SOUL-PERSONA\n", encoding="utf-8")

    builder = ContextBuilder(tmp_path)
    builder.multi_user_isolation = True
    prompt = builder.build_system_prompt(channel="feishu", scoped_memory="")

    assert "SECRET-USER-FACT" not in prompt
    assert "SOUL-PERSONA" in prompt


def test_user_md_injected_without_isolation(tmp_path: Path):
    """单用户保持基线：USER.md 注入。"""
    (tmp_path / "USER.md").write_text("USER-FACT-BASELINE\n", encoding="utf-8")

    builder = ContextBuilder(tmp_path)
    prompt = builder.build_system_prompt(channel="feishu")
    assert "USER-FACT-BASELINE" in prompt
