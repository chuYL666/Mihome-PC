# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑端对讲：采集麦克风、编码成摄像头能播的格式、送到中继上行通道。

链路（MISS 固件，参考米家 P2P 文档与 go2rtc / lalmax-nvr 的实现）：

1. ``TALK 1`` -> 0x106 SPEAKER_START_REQ，通知摄像头打开扬声器；
2. 按型号选编码，连续把音频包通过 DRW channel 3 发给摄像头：
   * 1027 PCMA（G.711 A-law，8 kHz）：40 ms / 320 字节，绝大多数机型；
   * 1024 PCM16LE（8 kHz）：40 ms / 640 字节，大方、小方；
   * 1032 Opus（48 kHz）：20 ms / 960 采样，C300 等；
3. ``TALK 0`` -> 0x108 SPEAKER_STOP。

采集统一按 48 kHz 单声道 Int16 请求（Windows/WASAPI 普遍支持），
PCMA/PCM16 在桌面端先做低通抽取到 8 kHz，避免额外引入重采样库。
音频包由独立线程写中继 stdin，绝不阻塞 Qt 界面线程。
"""
from __future__ import annotations

import ctypes
import queue
import threading

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

CODEC_PCM16 = 1024
CODEC_PCMA = 1027
CODEC_OPUS = 1032

# 需要用 Opus 对讲的机型（下行音频同样是 Opus）
_OPUS_MODELS = {
    'chuangmi.camera.72ac1',      # 小米 C300
    'isa.camera.500dh',
}
# 只用 16 位 PCM 对讲的机型
_PCM16_MODELS = {
    'isa.camera.df3',
    'isa.camera.isc5c1',
    'isa.camera.hlc6',
}

_SEG_END = np.array([0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF],
                    dtype=np.int32)


def speaker_codec(model: str | None) -> int | None:
    """按型号返回对讲编码；返回 None 表示该机型暂不支持。"""
    m = (model or '').strip().lower()
    if m in _OPUS_MODELS:
        return CODEC_OPUS
    if m in _PCM16_MODELS:
        return CODEC_PCM16
    if not m or '.camera.' not in m:
        return None
    return CODEC_PCMA


def to_8k(pcm_48k: bytes) -> bytes:
    """48 kHz -> 8 kHz：6 点平均兼作抗混叠低通，再抽取。"""
    if not pcm_48k:
        return b''
    a = np.frombuffer(pcm_48k, dtype='<i2').astype(np.float32)
    usable = len(a) // 6 * 6
    if usable == 0:
        return b''
    return a[:usable].reshape(-1, 6).mean(axis=1).astype('<i2').tobytes()


def pcm16_to_alaw(pcm: bytes) -> bytes:
    """16 位线性 PCM -> G.711 A-law（ITU-T G.711 的 linear2alaw）。"""
    if not pcm:
        return b''
    a = np.frombuffer(pcm, dtype='<i2').astype(np.int32)
    negative = a < 0
    # 负样本取反后减 1（Sun/sox 的 linear2alaw），不能减 8：
    # -1 会变成负数，seg/shift 全错，静音都会被编成明显的噪声。
    value = np.where(negative, -a - 1, a)
    mask = np.where(negative, 0x55, 0xD5).astype(np.int32)
    seg = np.searchsorted(_SEG_END, value, side='left')
    overflow = seg >= 8
    seg = np.clip(seg, 0, 7)
    shift = np.where(seg < 2, 4, seg + 3)
    aval = (seg << 4) | ((value >> shift) & 0xF)
    out = (aval ^ mask) & 0xFF
    out = np.where(overflow, 0x7F ^ mask, out)
    return out.astype(np.uint8).tobytes()


class _OpusEncoder:
    """复用下行解码用的 opus.dll 做编码；DLL 缺失时返回错误而不是崩溃。"""

    def __init__(self):
        self.error = ''
        self._lib = None
        self._enc = None
        try:
            from app.core import opus_decoder
            lib, reason = opus_decoder._load_library()
            if lib is None:
                raise RuntimeError(reason or 'opus.dll 不可用')
            lib.opus_encoder_create.restype = ctypes.c_void_p
            lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                                ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                        ctypes.c_int32, ctypes.c_char_p, ctypes.c_int32]
            lib.opus_encode.restype = ctypes.c_int32
            lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
            err = ctypes.c_int(0)
            # OPUS_APPLICATION_VOIP = 2048
            enc = lib.opus_encoder_create(48000, 1, 2048, ctypes.byref(err))
            if not enc or err.value != 0:
                raise RuntimeError(f'opus_encoder_create 失败 ({err.value})')
            self._lib, self._enc = lib, enc
        except Exception as exc:
            self.error = f'Opus 编码组件不可用：{exc}'

    def encode(self, pcm_48k: bytes) -> list[bytes]:
        """每 960 采样（20 ms）出一个原始 Opus 包。"""
        if self._enc is None:
            return []
        packets = []
        a = np.frombuffer(pcm_48k, dtype='<i2')
        out = ctypes.create_string_buffer(4000)
        for offset in range(0, len(a) - 959, 960):
            frame = a[offset:offset + 960].astype('<i2').tobytes()
            n = self._lib.opus_encode(self._enc, frame, 960, out, 4000)
            if n > 0:
                packets.append(out.raw[:n])
        return packets

    def close(self):
        if self._enc and self._lib:
            self._lib.opus_encoder_destroy(self._enc)
        self._enc = self._lib = None


# 一帧 40 ms：8 kHz × 320 采样 = 48 kHz × 1920 采样 = 3840 字节 Int16
_FRAME_48K_BYTES = 320 * 6 * 2
# Opus 一帧 20 ms：48 kHz × 960 采样 = 1920 字节
_OPUS_FRAME_BYTES = 960 * 2


class TalkEncoder:
    """把 48 kHz 单声道 PCM 切成摄像头要求的上行音频包。"""

    def __init__(self, codec: int):
        self.codec = codec
        self.error = ''
        self._rest = b''
        self._opus = _OpusEncoder() if codec == CODEC_OPUS else None
        if self._opus and self._opus.error:
            self.error = self._opus.error

    def feed(self, pcm_48k: bytes) -> list[bytes]:
        data = self._rest + pcm_48k
        if self.codec == CODEC_OPUS:
            use = len(data) // _OPUS_FRAME_BYTES * _OPUS_FRAME_BYTES
            self._rest, chunk = data[use:], data[:use]
            return self._opus.encode(chunk) if self._opus else []
        # 严格按「一帧」切，尾部不足一帧的字节留到下次，保证每包都是 40 ms
        use = len(data) // _FRAME_48K_BYTES * _FRAME_48K_BYTES
        self._rest, chunk = data[use:], data[:use]
        frames = []
        for offset in range(0, use, _FRAME_48K_BYTES):
            pcm8 = to_8k(chunk[offset:offset + _FRAME_48K_BYTES])
            frames.append(pcm8 if self.codec == CODEC_PCM16 else pcm16_to_alaw(pcm8))
        return frames

    def close(self):
        if self._opus:
            self._opus.close()


class TalkSession(QObject):
    """一次对讲会话：QAudioSource 采音 -> TalkEncoder -> 中继 stdin。

    只负责采集与投递，不直接持有 Qt 窗口；调用方在会话结束/窗口关闭时
    务必调用 stop()，否则麦克风会一直被占用。
    """
    error = Signal(str)
    level = Signal(float)

    def __init__(self, camera_session, model, parent=None):
        super().__init__(parent)
        self._session = camera_session
        self.codec = speaker_codec(model)
        self._encoder = None
        self._source = None
        self._io = None
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._worker = None
        self._running = False
        self._rate = 48000
        self._channels = 1

    def start(self) -> bool:
        if self.codec is None:
            self.error.emit('该设备型号暂不支持对讲')
            return False
        self._encoder = TalkEncoder(self.codec)
        if self._encoder.error:
            self.error.emit(self._encoder.error)
            return False
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            self.error.emit('系统没有可用的麦克风设备')
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
            fmt.setSampleRate(48000)
            if not device.isFormatSupported(fmt):
                self.error.emit('麦克风不支持 48 kHz 单声道采样，无法对讲')
                return False
        self._rate = fmt.sampleRate()
        self._channels = fmt.channelCount()
        self._source = QAudioSource(device, fmt, self)
        self._io = self._source.start()
        if self._io is None:
            self.error.emit('无法打开麦克风（可能被其它程序占用或被系统禁用）')
            return False
        # 先让摄像头打开扬声器，再开始采音，避免开头几十毫秒的包被中继丢掉
        self._session.set_speaker_codec(self.codec)
        if not self._session.talk(True):
            self.error.emit('对讲通道未就绪，请等画面出现后重试')
            self.stop()
            return False
        self._io.readyRead.connect(self._on_ready_read)
        self._running = True
        self._worker = threading.Thread(target=self._pump, daemon=True,
                                        name='camera-talk-send')
        self._worker.start()
        return True

    def _on_ready_read(self):
        data = bytes(self._io.readAll())
        if not data:
            return
        if self._channels > 1:
            arr = np.frombuffer(data, dtype='<i2').reshape(-1, self._channels)
            data = arr.mean(axis=1).astype('<i2').tobytes()
        if self._rate != 48000:
            arr = np.frombuffer(data, dtype='<i2').astype(np.float32)
            indices = (np.arange(int(len(arr) * 48000 / self._rate)) *
                       self._rate / 48000).astype(np.int32)
            indices = np.clip(indices, 0, max(0, len(arr) - 1))
            data = arr[indices].astype('<i2').tobytes()
        try:
            peak = float(np.abs(np.frombuffer(data, dtype='<i2')).max()) / 32768.0
        except ValueError:
            peak = 0.0
        self.level.emit(min(1.0, peak))
        for packet in self._encoder.feed(data):
            try:
                self._queue.put_nowait(packet)
            except queue.Full:
                pass  # 中继写不过来就丢新包，绝不堆积延迟

    def _pump(self):
        while self._running or not self._queue.empty():
            try:
                packet = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._session.send_audio(packet)
            except Exception:
                pass

    def stop(self):
        was_running = self._running
        self._running = False
        if self._io is not None:
            try:
                self._io.readyRead.disconnect(self._on_ready_read)
            except (RuntimeError, TypeError):
                pass
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
            self._source = None
        self._io = None
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        if was_running:
            try:
                self._session.talk(False)
            except Exception:
                pass
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=0.5)
        self._worker = None
