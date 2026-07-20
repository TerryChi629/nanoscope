"""M9 应用层公平调度 (PRD §15)。

不加机器、不拆多进程，只在单进程 asyncio 内做应用层调度优化：
- 全局有界 admission 背压（缺陷③）
- per-principal in-flight 上限，防单人霸占（缺陷②）
- per-principal 公平出队（least-in-flight round-robin），缓解慢会话拖垮快会话（缺陷①）
"""

from nanoscope.concurrency.admission import (
    AdmissionController,
    AdmissionRejectedError,
    BaselineGate,
    FairAdmissionController,
)

__all__ = [
    "AdmissionController",
    "AdmissionRejectedError",
    "BaselineGate",
    "FairAdmissionController",
]
