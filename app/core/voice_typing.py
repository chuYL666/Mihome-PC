# SPDX-License-Identifier: GPL-3.0-or-later
# MijiaDesk: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MijiaDesk contributors
"""唤起 Windows 自带语音输入（Win+H）。

Qt 的 Windows 平台插件只走 IMM32，没有实现 TSF 文本服务，
系统听写能不能把文字塞进 QLineEdit 取决于 Windows 侧的注入方式；
这里只负责把 Win+H 组合键发出去，识别、界面、文字注入全交给系统。
调用方在约定的时间内没等到文字时要给出可操作提示，而不是静默失败。
"""

from __future__ import annotations

import ctypes
import sys

VK_LWIN = 0x5B
VK_H = 0x48
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

if sys.platform == "win32":
    from ctypes import wintypes

    _ULONG_PTR = (ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
                  else ctypes.c_ulong)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def supported() -> bool:
    """当前平台能否唤起系统语音输入。"""
    return sys.platform == "win32"


def trigger() -> bool:
    """发送 Win+H。返回按键是否被系统接受（不代表听写成功）。

    SendInput 的 INPUT 结构必须按指针宽度对齐，结构体大小不对时它
    直接返回 0 且不报错，表现为「点了没反应」。
    """
    if not supported():
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [
            ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
        user32.SendInput.restype = ctypes.c_uint

        presses = [(VK_LWIN, 0), (VK_H, 0), (VK_H, KEYEVENTF_KEYUP),
                   (VK_LWIN, KEYEVENTF_KEYUP)]
        events = (_INPUT * len(presses))()
        for index, (vk, flags) in enumerate(presses):
            events[index].type = INPUT_KEYBOARD
            events[index].union.ki.wVk = vk
            events[index].union.ki.wScan = 0
            events[index].union.ki.dwFlags = flags
            events[index].union.ki.time = 0
            events[index].union.ki.dwExtraInfo = 0
        sent = user32.SendInput(len(presses), events, ctypes.sizeof(_INPUT))
        return sent == len(presses)
    except Exception:
        return False
