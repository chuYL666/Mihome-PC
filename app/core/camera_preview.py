# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned CS2 remote relay. Only an unguessable loopback stream reaches the player."""
import os
import base64
import queue
import socket
import subprocess
import sys
from pathlib import Path
import threading
import time
import uuid
from app import resource_path

_DLL_SEARCH_LOCK = threading.Lock()


def launch_camera_helper(args, **kwargs):
    """Do not make the independent JVM inherit PyInstaller's DLL search path."""
    frozen = sys.platform == 'win32' and getattr(sys, 'frozen', False)
    environment = dict(kwargs.pop('env', os.environ))
    bundle = getattr(sys, '_MEIPASS', None)
    if frozen and bundle:
        bundle = Path(bundle).resolve()
        environment['PATH'] = os.pathsep.join(
            entry for entry in environment.get('PATH', '').split(os.pathsep)
            if entry and not Path(entry).resolve().is_relative_to(bundle))
    if not frozen:
        return subprocess.Popen(args, env=environment, **kwargs)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    with _DLL_SEARCH_LOCK:
        previous = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(previous), previous)
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return subprocess.Popen(args, env=environment, **kwargs)
        finally:
            kernel.SetDllDirectoryW(previous.value or None)


class CameraPreviewError(Exception):
    def __init__(self, message, *, code='UNKNOWN'):
        super().__init__(message)
        self.code = code


ERRORS = {
    "CONFIG": "远程播放组件初始化失败，请重新安装组件。",
    "RELAY": "未能连接摄像头的公网中继，请确认摄像头在线、当前网络允许 UDP 通信后重试。",
    "AUTH": "已连接中继，但摄像头视频会话授权未通过；请先关闭米家中的观看页面再重试。",
    "MEDIA": "中继和视频授权已完成，但未收到画面；请确认摄像头镜头已开启后重试。",
    "STREAM": "摄像头视频连接已中断，请重试。",
}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _RelayHost:
    """One long-lived JVM serves every camera session.

    Booting a JVM costs 1~2 s and the old design paid it once per tile, so a
    five-camera wall burned several seconds just starting Java. A single host
    process removes that cost from every subsequent open.
    """
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._guard = threading.RLock()
        self._process = None
        self._listeners = {}

    @staticmethod
    def _command_line():
        java = resource_path("tools/camera_relay/runtime/bin/java.exe")
        jar = resource_path("tools/camera_relay/camera-relay.jar")
        if not java.exists() or not jar.exists():
            raise CameraPreviewError("缺少远程摄像头组件，请安装后重试。", code='CONFIG')
        # 一个进程扛多路，堆比单路时放大；线程栈仍压小，通道线程很多
        return [str(java), "-Xmx256m", "-Xss768k", "-XX:TieredStopAtLevel=1",
                "-XX:+UseSerialGC", "-XX:ErrorFile=NUL", "-cp", str(jar), "CameraRelay"]

    def _spawn(self):
        process = launch_camera_helper(
            self._command_line(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        threading.Thread(target=self._read, args=(process,), daemon=True,
                         name="camera-relay-host").start()
        return process

    def _read(self, process):
        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                session_id, _, message = line.partition(" ")
                if not session_id or not message:
                    continue
                with self._guard:
                    listener = self._listeners.get(session_id)
                if listener is not None:
                    try:
                        listener(message)
                    except Exception:
                        pass
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            with self._guard:
                if self._process is process:
                    self._process = None
                listeners, self._listeners = dict(self._listeners), {}
            for listener in listeners.values():
                try:
                    listener("ERROR HELPER_EXIT")
                except Exception:
                    pass

    def listen(self, session_id, listener):
        with self._guard:
            self._listeners[session_id] = listener

    def forget(self, session_id):
        with self._guard:
            self._listeners.pop(session_id, None)

    def _write(self, line):
        with self._guard:
            process = self._process
            if process is None or process.poll() is not None:
                process = self._spawn()
                self._process = process
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
                return True
            except (OSError, ValueError):
                self._process = None
                return False

    def open(self, session_id, fields):
        return self._write("OPEN\t" + "\t".join([session_id] + list(fields)))

    def send(self, session_id, command):
        return self._write(f"CMD\t{session_id}\t{command}")

    def close(self, session_id):
        self.forget(session_id)
        self._write(f"CLOSE\t{session_id}")


class CameraSession:
    def __init__(self):
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._host = None
        self._sid = None
        self._ready = False

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def start(self, source, on_progress=lambda _: None, *, quality=1, on_audio=None,
              channel=0) -> str:
        """Run on a dedicated worker, so relay negotiation cannot block cloud controls."""
        try:
            return self._start(source, on_progress, quality, on_audio, channel)
        except CameraPreviewError:
            self.stop()
            raise
        except Exception:
            self.stop()
            raise CameraPreviewError("远程视频连接启动失败，请稍后重试。", code='CONFIG') from None

    def _start(self, source, on_progress, quality, on_audio, channel=0):
        port, path = _free_port(), "/camera-" + uuid.uuid4().hex + ".ts"
        fields = [str(port), path, source.uid, source.init_string, source.client_public,
                  source.client_private, source.device_public, source.sign, str(quality),
                  str(int(channel or 0))]
        # 3 = 全分辨率：新款 2K/双摄机型必须用 3 才肯推原始分辨率，
        # 旧白名单只放到 2，导致选「全分辨率」直接报错、画面永远是子码流。
        if quality not in (0, 1, 2, 3):
            raise CameraPreviewError("不支持的画质档位。")
        if any(not isinstance(v, str) or not v or "\n" in v or "\r" in v or "\t" in v
               for v in fields):
            raise CameraPreviewError("远程视频授权格式无效，请重新获取。")
        if self.cancelled:
            raise CameraPreviewError("预览已取消。")
        session_id = uuid.uuid4().hex
        host = _RelayHost.instance()
        messages = queue.Queue()

        def listener(message):
            if self.cancelled:
                return
            if message.startswith("AUDIO 8000 ") and on_audio:
                try:
                    pcm = base64.b64decode(message[11:], validate=True)
                    if len(pcm) <= 16384:
                        on_audio(8000, pcm)
                except ValueError:
                    pass
                return
            if message.startswith("AUDIO_OPUS ") and on_audio:
                # Opus 原始包：采样率传 0 表示尚未解码，由播放端用 libopus 解
                try:
                    packet = base64.b64decode(message[11:], validate=True)
                    if len(packet) <= 8192:
                        on_audio(0, packet)
                except ValueError:
                    pass
                return
            if message in {"NOTICE AUDIO_UNSUPPORTED", "NOTICE CONTROL_FAILED",
                           "NOTICE QUALITY_REJECTED", "NOTICE AUDIO_ON",
                           "NOTICE AUDIO_OFF", "NOTICE CHANNEL_SET",
                           "INFO VIDEO_CODEC H264", "INFO VIDEO_CODEC H265", "INFO GOP_BOOTSTRAP"}:
                on_progress(message)
                return
            if message == "READY" or message in {"PHASE RELAY", "PHASE AUTH", "PHASE MEDIA"} or (
                    message.startswith("ERROR ") and message[6:] in ERRORS):
                messages.put(message)
                if message.startswith("ERROR ") and self._ready:
                    on_progress(message)
                return
            if message == "EXIT":
                messages.put("EXIT")
                if self._ready and not self.cancelled:
                    on_progress("ERROR STREAM")

        self._host, self._sid = host, session_id
        # 监听先挂上再 OPEN：JVM 侧响应很快，晚注册会丢掉 READY
        host.listen(session_id, listener)
        if not host.open(session_id, fields):
            host.forget(session_id)
            raise CameraPreviewError("远程播放组件启动失败，请重试。", code='CONFIG')
        deadline = time.monotonic() + 90
        while not self.cancelled and time.monotonic() < deadline:
            try:
                message = messages.get(timeout=.1)
            except queue.Empty:
                continue
            if message == "READY":
                if self.cancelled:
                    break
                self._ready = True
                return f"http://127.0.0.1:{port}{path}"
            if message.startswith("ERROR "):
                raise CameraPreviewError(ERRORS[message[6:]], code=message[6:])
            if message == "EXIT":
                raise CameraPreviewError("远程播放组件意外退出，请重试。", code='HELPER_EXIT')
            on_progress(message)
        raise CameraPreviewError("预览已取消。" if self.cancelled else "远程视频连接超时，请重试。",
                                 code='CANCELLED' if self.cancelled else 'TIMEOUT')

    def command(self, name, value):
        allowed = {"QUALITY": (0, 1, 2, 3), "AUDIO": (0, 1), "PTZ": (1, 2, 3, 4),
                   "CHANNEL": (0, 1, 2, 3)}
        if name not in allowed or value not in allowed[name]:
            return False
        with self._lock:
            host, session_id = self._host, self._sid
        if host is None or session_id is None or self.cancelled:
            return False
        return host.send(session_id, f"{name} {value}")

    def stop(self):
        self._cancelled.set()
        with self._lock:
            host, session_id = self._host, self._sid
            self._host = self._sid = None
        if host is not None and session_id is not None:
            host.close(session_id)
