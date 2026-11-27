# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""JSON 持久化公共基础：数据目录解析、旧位置迁移、原子写入。

打包形态曾把配置写在 exe 同目录，装进 Program Files 等受保护位置后
写入失败且被静默吞掉，表现为设置每次启动重置；现统一写入
%LOCALAPPDATA%\\MijiaDesk\\，并把旧位置的已有文件一次性迁移过来。
开发形态仍为仓库根目录（文件均已 gitignore）。
"""

import json
import os
import sys
from pathlib import Path

_APP_DIR_NAME = "MijiaDesk"
# 改名史：MiHome-Windows → MijiaDeck → MijiaDesk，旧目录里的配置都要搬过来
_LEGACY_APP_DIR_NAMES = ("MijiaDeck", "MiHome-Windows")
# 旧版写在 exe 同目录的全部数据文件，发现即迁移
_KNOWN_FILES = ("settings.json", "tray.json", "workbench.json", "devices_cache.json", "account_profiles.json")
# 设备图标缓存子目录，改名时同样要搬（见 _migrate_icons）
_ICONS_SUBDIR = "icons"
_migrated = False


def data_dir() -> Path:
    from app import is_packaged

    if is_packaged():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / _APP_DIR_NAME
    return Path(__file__).resolve().parents[2]


def data_file(filename: str) -> Path:
    """数据文件完整路径，首次访问时尝试从旧的 exe 同目录迁移。"""
    global _migrated
    from app import is_packaged
    if is_packaged() and not _migrated:
        _migrated = True
        _migrate_legacy_files()
    return data_dir() / filename


def _migrate_legacy_files() -> None:
    target_dir = data_dir()
    local_app = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    legacy_dirs = [local_app / name for name in _LEGACY_APP_DIR_NAMES]
    legacy_dirs += [Path(sys.argv[0]).resolve().parent, Path(sys.executable).resolve().parent]
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for legacy_dir in dict.fromkeys(legacy_dirs):
            if legacy_dir == target_dir:
                continue
            for name in _KNOWN_FILES:
                legacy = legacy_dir / name
                target = target_dir / name
                if legacy.is_file() and not target.exists():
                    # Copy only: preserve old data and never overwrite new preferences.
                    target.write_bytes(legacy.read_bytes())
            _migrate_icons(legacy_dir, target_dir)
    except OSError:
        pass


def _migrate_icons(legacy_dir: Path, target_dir: Path) -> None:
    """把旧目录里的设备图标一起搬过来。

    改名时这里只搬了 json 配置，icons 子目录被落下，卡片只能重新联网
    下载；而 get_icon 接口对批量型号有限流，实际常常只下到一两张，其余
    卡片退化成灰色线条图标——用户看到的就是「图标不清楚」。
    """
    legacy_icons = legacy_dir / _ICONS_SUBDIR
    if not legacy_icons.is_dir():
        return
    target_icons = target_dir / _ICONS_SUBDIR
    target_icons.mkdir(parents=True, exist_ok=True)
    for image in legacy_icons.glob("*.png"):
        target = target_icons / image.name
        if not target.exists():
            try:
                target.write_bytes(image.read_bytes())
            except OSError:
                continue


def read_json(path: Path, default: dict) -> dict:
    """读取 JSON dict；文件缺失、损坏或类型不对时返回 default。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    return raw


def write_json(path: Path, data: dict) -> None:
    """原子写入：先写同目录临时文件再 os.replace，进程被杀不会留半截文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
