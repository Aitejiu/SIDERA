"""Versioned, provider-free closure sample used by the real acceptance path."""

from __future__ import annotations

import subprocess
from pathlib import Path

SAMPLE_VERSION = "closure-sample-v1"
BRIEF_TEXT = """在当前样例仓库实现一个离线任务状态汇总，不联网，不安装依赖。
请规划两个可并行的实现 WorkerItem source-a、source-b，以及依赖二者成果的 integration。
source-a 只负责 out/source-a.json：marker 为 SIDERA_CLOSURE_SOURCE_A_V1，
items 为 [{"id":"api","status":"done"},{"id":"ui","status":"queued"}]。
source-b 只负责 out/source-b.json：marker 为 SIDERA_CLOSURE_SOURCE_B_V1，
items 为 [{"id":"tests","status":"running"},{"id":"docs","status":"blocked"}]。
integration 必须读取经控制面选定的这两份上游成果，保留两文件，并生成 out/summary.json：
total=4；counts 为 done/queued/running/blocked 各 1；source_markers 按 source-a、source-b
顺序保存两份 marker。JSON 除上述字段外不得增加字段；数组顺序保持本需求的顺序。
每个 Worker 只提交自己负责的成果，不修改 README。方案先交给我确认，确认前不要执行。"""

README = "# SIDERA closure sample v1\n\nGenerated outputs belong in out/.\n"


def prepare_sample_repository(path: Path) -> str:
    """Create only the seed README and return its local baseline commit."""

    path = path.expanduser()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError("sample repository must be missing or empty")
    else:
        path.mkdir(parents=True)
    (path / "README.md").write_text(README, encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "sidera-closure@example.invalid")
    _git(path, "config", "user.name", "SIDERA closure sample")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "seed closure sample v1")
    return _git_output(path, "rev-parse", "HEAD")


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


__all__ = ["BRIEF_TEXT", "README", "SAMPLE_VERSION", "prepare_sample_repository"]
