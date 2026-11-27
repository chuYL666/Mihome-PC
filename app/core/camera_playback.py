# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""摄像头 SD 卡录像回放的协议编解码。

实时流走 DRW channel 0/2，历史录像另走 RDT（file-transfer）channel 1：
先用命令 6 取回卡上全部片段的索引，再用 MISS 0x10D 请求某个片段的
起始秒；摄像头随后把回放画面推到和直播相同的 channel 2，因此本地
MPEG-TS 播放器、画质、静音等链路都不用改。

协议字段参考公开实现 xiaomi-camera-viewer（固件逆向得出）：
  https://github.com/lubos-hnanicek/xiaomi-camera-viewer
本模块只做无副作用编解码，方便离线回归；发送与收包在 Java 中继里。
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import time
from datetime import datetime
from typing import Iterable

# ---- MISS 命令字（与上游 MissClient 常量一致）----
MISS_PLAYBACK_REQ = 0x10D
MISS_PLAYBACK_RES = 0x10E

# ---- RDT（channel 1）命令字 ----
RDT_FILE_COMMAND = 1    # 按时间戳下载一个 MP4
RDT_INDEX_COMMAND = 6   # 取回整张卡的片段索引
RDT_EVENT_COMMAND = 11  # 只取有事件标记的片段（暂未使用）

# 索引记录的合法性边界：卡上索引是定长文件，未写过的槽会解出 1970
# 或远未来的时间戳，必须丢掉，否则会列出根本播不了的片段
_MIN_START = 1_577_836_800   # 2020-01-01
_MAX_START = 2_524_608_000   # 2050-01-01
_MAX_DURATION = 3600
_DURATION_MASK = 0xFF
_EVENT_BIT = 0x100
_INDEX_HEADER = 4
_RECORD_SIZE = 8


class Clip:
    """一段录像：起始 Unix 秒 + 时长（秒）+ 是否含侦测事件。"""

    __slots__ = ('start', 'duration', 'event')

    def __init__(self, start: int, duration: int, event: bool = False):
        self.start = int(start)
        self.duration = int(duration)
        self.event = bool(event)

    @property
    def end(self) -> int:
        return self.start + self.duration

    def __eq__(self, other) -> bool:
        return (isinstance(other, Clip) and self.start == other.start
                and self.duration == other.duration and self.event == other.event)

    def __repr__(self) -> str:
        return f"Clip({self.start}, {self.duration}, event={self.event})"


def index_request(channel: int = 0) -> bytes:
    """RDT 命令 6 的 24 字节载荷：只有 [8:12] 是 LE 通道号，其余为 0。

    这里不能放 JSON：固件把这段字节直接当通道号传给
    record_get_index_all，文本的 ASCII 会被当成几亿的通道号而拒绝。
    """
    payload = bytearray(24)
    struct.pack_into('<I', payload, 8, int(channel))
    return bytes(payload)


def file_request(timestamp: int, channel: int = 0) -> bytes:
    """RDT 命令 1 的 12 字节载荷：偏移 0 是 LE 起始秒，偏移 8 是 LE 通道号。"""
    payload = bytearray(12)
    struct.pack_into('<I', payload, 0, int(timestamp))
    struct.pack_into('<I', payload, 8, int(channel))
    return bytes(payload)


def parse_index(payload: bytes | bytearray | None) -> list[Clip]:
    """解析索引回复：4 字节头 + N×8 字节记录。

    每条记录是 LE uint32 起始秒 + LE uint32 时长/标志字；标志字低字节是
    秒数，bit 8 是「有侦测事件」而不是时长加 256 秒。
    """
    if not payload or len(payload) < _INDEX_HEADER + _RECORD_SIZE:
        return []
    data = bytes(payload)
    clips: list[Clip] = []
    seen: set[int] = set()
    for offset in range(_INDEX_HEADER, len(data) - _RECORD_SIZE + 1, _RECORD_SIZE):
        start, packed = struct.unpack_from('<II', data, offset)
        if not (_MIN_START <= start <= _MAX_START):
            continue
        duration = packed & _DURATION_MASK
        if duration <= 0 or duration > _MAX_DURATION:
            continue
        if start in seen:
            continue
        seen.add(start)
        clips.append(Clip(start, duration, bool(packed & _EVENT_BIT)))
    clips.sort(key=lambda c: c.start)
    return clips


def playback_request(session_id: int, start: int, end: int,
                     lenses: Iterable[int] | None = None) -> str:
    """MISS 0x10D 的请求体；字段一个都不能少，缺一个固件会直接丢弃请求。

    双摄机型带 ``channel`` 时，channel/starttime/endtime/offset/speed 必须
    同为等长数组（元素必须是 JSON 整数），sessionid 等三项仍是标量。
    """
    fields = [
        f'"sessionid":{int(session_id)}',
        '"autoswitchtolive":1',
        '"avchannelmerge":1',
    ]
    channels = [int(c) for c in (lenses or [])]
    if not channels:
        fields += [f'"starttime":{int(start)}', f'"endtime":{int(end)}',
                   '"offset":0', '"speed":1']
    else:
        repeat = lambda value: '[' + ','.join(value for _ in channels) + ']'
        fields += [
            f'"starttime":{repeat(str(int(start)))}',
            f'"endtime":{repeat(str(int(end)))}',
            f'"offset":{repeat("0")}',
            f'"speed":{repeat("1")}',
            '"channel":[' + ','.join(str(c) for c in channels) + ']',
        ]
    return '{' + ','.join(fields) + '}'


def stop_request(session_id: int) -> str:
    """starttime/endtime 归零 = 退出回放、切回直播。"""
    return playback_request(session_id, 0, 0)


def parse_status(body: bytes | str | None) -> dict | None:
    """解析 0x10E 回复：status=filefound 才算成功打开片段。"""
    if not body:
        return None
    try:
        text = body.decode('utf-8') if isinstance(body, bytes) else str(body)
        value = json.loads(text)
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        return None
    return value


def status_found(status: dict | None) -> bool:
    return bool(status and status.get('status') == 'filefound')


def group_by_day(clips: Iterable[Clip], *, now: float | None = None
                 ) -> list[tuple[str, list[Clip]]]:
    """按本地自然日分组，最近一天排在最前。"""
    buckets: dict[str, list[Clip]] = {}
    for clip in clips:
        day = datetime.fromtimestamp(clip.start).strftime('%Y-%m-%d')
        buckets.setdefault(day, []).append(clip)
    days = sorted(buckets, reverse=True)
    return [(day, sorted(buckets[day], key=lambda c: c.start)) for day in days]


def format_span(clip: Clip) -> str:
    begin = datetime.fromtimestamp(clip.start).strftime('%H:%M:%S')
    finish = datetime.fromtimestamp(clip.end).strftime('%H:%M:%S')
    return f'{begin} – {finish}'


def day_timestamp(day: str) -> int | None:
    """把 YYYY-MM-DD 转成本地零点 Unix 秒，用于按天筛选。"""
    try:
        return int(datetime.strptime(day, '%Y-%m-%d').timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def clips_near(clips: Iterable[Clip], moment: int, window: int = 0) -> list[Clip]:
    """给「跳到某个时刻」用：优先返回正好覆盖该时刻的片段。

    window>0 时才退而求其次，把前后 window 秒内的相邻片段也列出来。
    """
    items = list(clips)
    covering = [c for c in items if c.start <= moment < c.end]
    if covering or window <= 0:
        return covering
    return [c for c in items if c.start - window <= moment <= c.end + window]


def storage_channels(model: str, channel: int = 0) -> list[int]:
    """回放用的存储通道。双摄两路各有独立索引，先都问一遍。

    直播 channel=1 表示第二镜头，但存储通道不是同一套编号（CW500 用 0/10），
    所以这里对双摄返回候选列表，由中继逐个取索引，谁有数据就用谁。
    """
    dual = 'hlmax' in (model or '') or 'dual' in (model or '')
    if dual and channel:
        return [10, 0]
    return [0, 10] if dual else [0]


def playback_window(clip: Clip) -> tuple[int, int]:
    """片段请求的时间窗：起始秒必须精确，结束秒给足整段时长。"""
    return clip.start, clip.end


def parse_relay_message(message: str) -> tuple[str, object] | None:
    """把中继 stdout 里的回放消息解析成 ``(kind, value)``。

    kind: index / file / status / notice；无关消息返回 None。
    中继只上报已经解密重组好的 RDT 载荷，这里统一做 base64 与索引解析。
    """
    if not isinstance(message, str):
        return None
    if message.startswith('RDT '):
        try:
            _, cmd, encoded = message.split(' ', 2)
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None
        if cmd == str(RDT_INDEX_COMMAND):
            return 'index', parse_index(raw)
        if cmd == str(RDT_FILE_COMMAND):
            return 'file', raw
        return None
    if message.startswith('PLAYBACK '):
        try:
            raw = base64.b64decode(message[9:], validate=True)
        except (ValueError, binascii.Error):
            return None
        return 'status', parse_status(raw)
    if message.startswith('NOTICE '):
        code = message[7:].split(' ', 1)[0]
        if code in {'INDEX_SENT', 'INDEX_FAILED', 'PLAYBACK_SENT',
                    'PLAYBACK_FAILED', 'LIVE'}:
            return 'notice', code
    return None


def now_seconds() -> int:
    return int(time.time())
