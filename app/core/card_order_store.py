# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""主页设备卡片自定义顺序的持久化。

用户在主页拖动卡片重排后保存一份 did 有序列表；排序时自定义
顺序优先，未出现过的设备按默认规则追加在尾部。文件损坏或设备
增删都安全：读取时只保留当前列表里存在的 did 即可。
"""

from . import _json_store

_FILE = "card_order.json"
_cache: list[str] | None = None


def _load() -> list[str]:
    global _cache
    if _cache is None:
        data = _json_store.read_json(_json_store.data_file(_FILE), {})
        raw = data.get("order")
        if isinstance(raw, list):
            _cache = [str(item) for item in raw if isinstance(item, str)]
        elif isinstance(raw, dict):
            # 兼容历史上可能的 {did: index} 形态
            pairs = sorted((int(v), str(k)) for k, v in raw.items())
            _cache = [did for _, did in pairs]
        else:
            _cache = []
    return _cache


def _save(order: list[str]) -> None:
    _json_store.write_json(_json_store.data_file(_FILE), {"order": order})


def order_index(did: str) -> int:
    """排序键：自定义顺序优先；未排序设备统一排在尾部（按默认规则）。"""
    try:
        return _load().index(str(did))
    except ValueError:
        return 1 << 30


def is_customized() -> bool:
    return bool(_load())


def reposition(did: str, target_did: str) -> None:
    """把 did 移到 target_did 当前所在的位置，其余设备相对顺序不变。"""
    order = _load()
    did, target_did = str(did), str(target_did)
    if did == target_did:
        return
    if did not in order:
        order.append(did)
    if target_did not in order:
        order.append(target_did)
    order.remove(did)
    order.insert(order.index(target_did), did)
    _save(order)


def clear() -> None:
    global _cache
    _cache = []
    _save([])
