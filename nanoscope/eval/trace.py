"""最小 TraceCollector：把评测事实以 JSONL 落盘 (PRD §9)。

职责边界：只采集"事实"（run/retrieval/exposure 等事件），不计算任何指标。
指标计算（Recall/MRR/Jain/forbidden-hit 汇总）是 P1 的 Evaluator/LoadAnalyzer 职责。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class TraceCollector:
    """把结构化事件追加写入 JSONL。线程安全（简单锁）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """追加一条事件，返回落盘的记录。"""
        event: dict[str, Any] = {"ts": time.time(), "event": event_type, **fields}
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return event

    def read_all(self) -> list[dict[str, Any]]:
        """读回全部事件（评测/断言用）。"""
        events: list[dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except FileNotFoundError:
            pass
        return events
