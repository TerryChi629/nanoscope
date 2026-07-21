"""一键统一报告入口 (PRD_v4 §M17.2)：`python -m nanoscope.eval.report`。

采集六条证据线的改前 vs 改后 A/B，渲染成自包含 HTML 写到指定路径（默认
`reports/NANOSCOPE_UNIFIED_REPORT.html`）。数据全部来自 `tests/scope` 同源可复现代码，
不手填任何数字。

用法：
    python -m nanoscope.eval.report                    # 默认输出到 reports/
    python -m nanoscope.eval.report -o /tmp/out.html   # 指定输出路径
    python -m nanoscope.eval.report --workspace <dir> --repo <db>
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from nanoscope.eval.reporter import generate

_DEFAULT_OUT = Path("reports") / "NANOSCOPE_UNIFIED_REPORT.html"


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(
        prog="python -m nanoscope.eval.report",
        description="生成 NanoScope 统一 A/B 报告（六条证据线，自包含 HTML）。",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=_DEFAULT_OUT,
        help=f"报告输出路径（默认 {_DEFAULT_OUT}）",
    )
    parser.add_argument(
        "--workspace", type=Path, default=None,
        help="隔离采集用的临时 workspace（默认用临时目录）",
    )
    parser.add_argument(
        "--repo", type=Path, default=None,
        help="隔离采集用的临时 SQLite 记忆库路径（默认用临时目录）",
    )
    args = parser.parse_args(argv)

    if args.workspace is not None and args.repo is not None:
        out = generate(args.out, args.workspace, args.repo)
    else:
        # 采集态是临时的（隔离 A/B 只需一次性构造攻击语料），用临时目录即可。
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ws = args.workspace or base / "ws"
            repo = args.repo or base / "mem.db"
            out = generate(args.out, ws, repo)
    print(f"NanoScope 统一报告已生成：{out}")
    return out


if __name__ == "__main__":
    main()
