# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""GitHub Releases 新版本检查：取最新 release 版本号与当前版本对比。

检查走独立后台线程而非 JobExecutor 串行队列：这是与米家无关的第三方
请求，混进设备任务队列会让弱网下最长十几秒的超时连带阻塞设备加载与
用户操作。结果经信号发回主线程，界面线程永不受网络等待影响。
"""

import re
import threading
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import requests
from PySide6.QtCore import QObject, Signal

from app import APP_NAME, UPDATE_REPOSITORY
LATEST_API = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases/latest" if UPDATE_REPOSITORY else ""
RELEASES_PAGE = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest" if UPDATE_REPOSITORY else ""


@dataclass
class ReleaseInfo:
    tag: str
    url: str
    name: str


def fetch_latest_release() -> ReleaseInfo:
    """Prefer the API; fall back to the public latest-release redirect."""
    from app import __version__
    if not LATEST_API:
        raise RuntimeError("暂未开放更新检查")

    headers = {"User-Agent": f"{APP_NAME}/{__version__}"}
    try:
        with requests.get(LATEST_API, timeout=(5, 8),
                          headers={**headers, "Accept": "application/vnd.github+json"}) as resp:
            resp.raise_for_status()
            data = resp.json()
            tag = str(data.get("tag_name") or "").strip()
            if not tag:
                raise ValueError("Missing release tag")
            return ReleaseInfo(tag=tag, url=str(data.get("html_url") or RELEASES_PAGE),
                               name=str(data.get("name") or tag))
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        # Shared public IPs can exhaust the anonymous API quota. Do not retry
        # that endpoint: the normal releases/latest page redirects to the
        # actual published tag without requiring an API token or HTML parsing.
        pass
    try:
        with requests.get(RELEASES_PAGE, timeout=(5, 8), stream=True,
                          headers={**headers, "Accept": "text/html"}) as resp:
            resp.raise_for_status()
            address = urlparse(resp.url)
            prefix = f"/{UPDATE_REPOSITORY}/releases/tag/"
            path = unquote(address.path)
            if (address.scheme != 'https' or address.netloc.lower() != 'github.com'
                    or not path.lower().startswith(prefix.lower())):
                raise ValueError("Unexpected release destination")
            tag = path[len(prefix):]
            if not re.fullmatch(r'[vV]?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', tag):
                raise ValueError("Invalid release tag")
            return ReleaseInfo(tag=tag,
                url=f'https://github.com{prefix}{quote(tag, safe="")}', name=f'{APP_NAME} {tag}')
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        raise RuntimeError('暂时无法连接更新服务，请稍后重试，或前往 GitHub 发布页查看。') from None


def _version_parts(value: str) -> tuple[int, ...]:
    """版本号解析为数字段：容忍 v 前缀、预发布/构建后缀与非数字段。"""
    core = value.strip().lower().lstrip("v")
    core = re.split(r"[-+].*$", core)[0]
    parts: list[int] = []
    for seg in core.split("."):
        m = re.match(r"\d+", seg)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新；位数不等时按 0 补齐再逐段比较。"""
    a = list(_version_parts(latest))
    b = list(_version_parts(current))
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return tuple(a) > tuple(b)


class UpdateChecker(QObject):
    """后台线程查一次最新版本；check_finished 回主线程（info, error）。"""

    check_finished = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)

    def check(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            info = fetch_latest_release()
        except Exception as exc:  # 网络异常全部转回调，绝不打崩进程
            self.check_finished.emit(None, exc)
        else:
            self.check_finished.emit(info, None)
