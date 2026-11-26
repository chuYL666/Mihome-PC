# SPDX-License-Identifier: GPL-3.0-or-later
"""崩溃现场记录。

打包版以 --windowed 运行，stdout/stderr 被重定向到 devnull：Python 未捕获
异常和 C 层致命错误（Qt / FFmpeg 解码线程里的访问冲突）原本完全无痕，
用户只会看到「窗口突然没了」。这里把两类事件都落到
%LOCALAPPDATA%\\MijiaDesk\\logs\\crash.log，事后可以直接拿这个文件定位。

设计约束：
- 只写本地日志，不联网、不上报；
- 任何一步失败都静默降级，绝不能因为记日志把程序拖垮；
- 文件超过 2 MB 轮转为 crash.log.1，避免长期驻留无限增长。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import time
import traceback
from pathlib import Path

from app import __version__

MAX_BYTES = 2 * 1024 * 1024
DIR_NAME = "MijiaDesk"

_state: dict[str, object] = {"path": None, "stream": None}


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / DIR_NAME / "logs"


def crash_log_path() -> Path:
    return log_dir() / "crash.log"


def _rotate(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass


def _write_banner(stream, kind: str) -> None:
    try:
        stream.write("\n===== %s | %s =====\n" % (kind, time.strftime("%Y-%m-%d %H:%M:%S")))
        stream.write("version=%s python=%s exe=%s\n"
                     % (__version__, sys.version.split()[0], sys.executable))
        stream.write("argv=%r\n" % (sys.argv,))
        stream.flush()
    except Exception:
        pass


def _excepthook(exc_type, exc, tb) -> None:
    stream = _state.get("stream")
    if stream is not None:
        try:
            _write_banner(stream, "uncaught exception")
            traceback.print_exception(exc_type, exc, tb, file=stream)
            stream.flush()
        except Exception:
            pass
    sys.__excepthook__(exc_type, exc, tb)


def install() -> Path | None:
    """安装异常钩子与 faulthandler，返回日志路径（失败返回 None）。"""
    if _state.get("stream") is not None:
        return _state["path"]  # type: ignore[return-value]
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "crash.log"
        _rotate(path)
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    _state["path"] = path
    _state["stream"] = stream
    try:
        # all_threads：解码/中继线程里的段错误也能留下栈
        faulthandler.enable(file=stream, all_threads=True)
    except (OSError, ValueError):
        pass
    sys.excepthook = _excepthook
    _write_banner(stream, "session start")
    return path


def record_exception(exc: BaseException, kind: str = "fatal exit") -> None:
    """记录被显式捕获但会导致退出的异常（如 main() 抛出的异常）。"""
    stream = _state.get("stream")
    if stream is None:
        return
    try:
        _write_banner(stream, kind)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=stream)
        stream.flush()
    except Exception:
        pass
