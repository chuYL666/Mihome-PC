# SPDX-License-Identifier: GPL-3.0-or-later
"""Opus 音频解码。

部分米家机型（如 chuangmi.camera.039a01）只推 Opus 音频（中继里
codecId 1032）。中继运行在 JVM 里，没有现成的 Opus 解码器，硬塞
一个纯 Java 解码实现要额外几十 KB 代码和一堆测试；这里改由中继
原样透传 Opus 包，桌面端用 libopus 解码——libopus 是 BSD 授权的
独立 DLL，ctypes 直接调用，不引入 Python 依赖。

解码统一输出 48 kHz 单声道 16 位 PCM：libopus 自带重采样与下混，
摄像头实际是 16 kHz 单声道时也能正确还原。
"""

import ctypes
import sys
import threading

# 一帧最长 120 ms，48 kHz 下 5760 个采样点，覆盖所有合法的 Opus 帧长。
MAX_FRAME_SAMPLES = 5760
OUTPUT_RATE = 48000
OUTPUT_CHANNELS = 1

_LIB = None
_LIB_ERROR = None
_LIB_LOCK = threading.Lock()


def _candidate_paths():
    from app import resource_path
    yield resource_path("app/native/opus.dll")
    if sys.platform == "win32":
        # 开发环境下 pyogg 里也带一份，省去手工放文件。
        try:
            import pyogg
            from pathlib import Path
            yield Path(pyogg.__file__).with_name("opus.dll")
        except Exception:
            pass
    yield "opus.dll"


def _load_library():
    """加载 libopus 并声明函数签名；失败时返回 (None, 原因)。"""
    global _LIB, _LIB_ERROR
    with _LIB_LOCK:
        if _LIB is not None or _LIB_ERROR is not None:
            return _LIB, _LIB_ERROR
        last = "未找到 opus.dll"
        for path in _candidate_paths():
            try:
                lib = ctypes.CDLL(str(path))
            except OSError as exc:
                last = str(exc)
                continue
            lib.opus_decoder_create.restype = ctypes.c_void_p
            lib.opus_decoder_create.argtypes = [
                ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.opus_decode.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
            lib.opus_decode.restype = ctypes.c_int
            lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
            _LIB = lib
            return _LIB, None
        _LIB_ERROR = last
        return None, _LIB_ERROR


class OpusStreamDecoder:
    """把连续的 Opus 包解成 48 kHz 单声道 s16le PCM。

    每次播放会话用一个实例；解码器内部带状态（Opus 是有状态的编码），
    所以不能跨流复用。
    """

    def __init__(self):
        self._state = None
        self._error = None
        self._buf = (ctypes.c_int16 * MAX_FRAME_SAMPLES)()

    @property
    def error(self):
        return self._error

    def _ensure(self):
        if self._state is not None or self._error is not None:
            return self._state is not None
        lib, error = _load_library()
        if lib is None:
            self._error = error
            return False
        code = ctypes.c_int(0)
        state = lib.opus_decoder_create(
            OUTPUT_RATE, OUTPUT_CHANNELS, ctypes.byref(code))
        if not state or code.value != 0:
            self._error = "Opus 解码器初始化失败"
            return False
        self._state = state
        return True

    def decode(self, packet):
        """解一个 Opus 包，返回 PCM 字节；坏包返回空 bytes 而不抛异常。"""
        if not packet or not self._ensure():
            return b""
        lib, _ = _load_library()
        count = lib.opus_decode(
            self._state, packet, len(packet), self._buf, MAX_FRAME_SAMPLES, 0)
        if count <= 0:
            return b""
        return bytes(memoryview(self._buf).cast("B")[:count * 2])

    def close(self):
        if self._state is not None:
            lib, _ = _load_library()
            if lib is not None:
                try:
                    lib.opus_decoder_destroy(self._state)
                except Exception:
                    pass
            self._state = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
