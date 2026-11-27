# SPDX-License-Identifier: GPL-3.0-or-later
"""双摄设备的双画面窗口：云台与枪机两路同时出图。

实测（2026-11-26，isa.camera.hlmax / 固件 5.3.2_0336）：

- 同一台设备可以同时承载两路视频会话——主码流请求通道 0（云台），第二路
  走 `{"videoquality":-1,"videoquality2":N}`（枪机），两路各约 20 / 15 fps；
- 但两路**不能同时去鉴权**：并发开两会话时设备只认后到的那个，先到的那路
  会被踢掉（表现为一路有画面、另一路一直停在「等待画面」）。所以只在
  云台真的出帧后才开枪机；云台迟迟不出帧时续等，约 24 秒后兜底放行；
- 两路各自 `prepare_camera_source`，共用同一份授权参数也会互相顶掉；
- 第二路要求枪机镜头电源是开的，否则能连上但没画面。

代价是解码量翻倍：默认沿用调用方画质（九宫格里是 480P），要全分辨率时由
用户显式切一次——软件解码两路 2560×1440 对核显压力不小。
"""

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QVBoxLayout, QWidget)
import shiboken6
import threading
import time

from app.core.camera_preview import CameraSession, lens_power_on
from app.ui.camera_canvas import AspectVideoLayout, CameraCanvas
from app.ui.popup_menu import style_popup_menu
from app.ui.si_theme import SiColors

_LENS_SIID = 6
_LENS_SWITCH = {0: 25, 1: 26}      # 通道 → 镜头电源开关（云台 / 枪机）
_SECONDARY_DELAY = 6000            # 每轮检查云台是否已出帧（最多续等 3 轮）


class _LensPane(QWidget):
    """一路镜头：小标题 + 画面。控件条关掉，避免两套按钮抢操作。"""

    def __init__(self, channel, title, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.session = None
        self.player = None
        self.sink = None
        self.frames = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.caption = QLabel(title)
        self.caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.caption)
        self.canvas = CameraCanvas(self)
        self.canvas.set_controls_enabled(False)
        # 双画面各占一半宽度，不再按单画面留最小高度
        self.canvas.setMinimumSize(180, 80)
        self.video = AspectVideoLayout()
        # 单画面窗口留 49px 侧边是为了不贴控制条；双画面各占半宽，
        # 留那么多会把两路都挤成小方块，缩到 8px 让画面尽量大。
        self.video.setContentsMargins(8, 4, 8, 0)
        self.video.addWidget(self.canvas)
        layout.addLayout(self.video, 1)
        self.retheme()

    def retheme(self):
        self.caption.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")


class DualCameraDialog(QDialog):
    """云台 + 枪机并排。构造后调用 start()。"""

    _ready = Signal(int, int, str)
    _failed = Signal(int, int, str)
    _progress = Signal(int, int, str)

    def __init__(self, service, jobs, device, quality=1, parent=None):
        super().__init__(parent)
        self._service, self._jobs, self._device = service, jobs, device
        self._quality = quality
        self._generation = 0
        self.setWindowTitle(f'双画面 · {getattr(device, "name", "摄像头")}')
        self.setModal(False)
        self.setMinimumSize(720, 300)
        self.resize(1080, 460)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 12)
        self._layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel(f'{getattr(device, "name", "摄像头")} · 双画面')
        header.addWidget(self._title, 1)
        self._retry_btn = QPushButton('重试')
        self._retry_btn.setFixedSize(56, 26)
        self._retry_btn.setCursor(Qt.PointingHandCursor)
        self._retry_btn.setToolTip('两路都断开重连')
        self._retry_btn.clicked.connect(self.start)
        header.addWidget(self._retry_btn)
        self._quality_btn = QPushButton({1: '480P', 3: '全分辨率'}[quality])
        self._quality_btn.setFixedSize(74, 26)
        self._quality_btn.setCursor(Qt.PointingHandCursor)
        self._quality_btn.setToolTip('两路画面一起切换画质（全分辨率下解码压力翻倍）')
        self._quality_btn.clicked.connect(self._show_quality)
        header.addWidget(self._quality_btn)
        self._close_btn = QPushButton('关闭')
        self._close_btn.setFixedSize(56, 26)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self.close)
        header.addWidget(self._close_btn)
        self._layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(10)
        self._panes = [_LensPane(0, '云台镜头（可转向）', self),
                       _LensPane(1, '枪机镜头（固定广角）', self)]
        for pane in self._panes:
            body.addWidget(pane, 1)
        self._layout.addLayout(body, 1)

        button_style = (
            f"QPushButton {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY};"
            f" border: none; border-radius: 6px; font-size: 9pt; }}"
            f" QPushButton:hover {{ background: {SiColors.CARD_HOVER}; }}")
        for button in (self._retry_btn, self._quality_btn, self._close_btn):
            button.setStyleSheet(button_style)
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 10pt; font-weight: 600;")

        self._secondary_timer = QTimer(self)
        self._secondary_timer.setSingleShot(True)
        self._secondary_timer.timeout.connect(self._check_secondary)
        self._secondary_waits = 0

        self._ready.connect(self._on_ready)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)

    # ---- 生命周期 ----

    def start(self):
        if not getattr(self._device, 'online', True):
            for pane in self._panes:
                pane.canvas.message('摄像头已离线')
            return
        self.stop()
        self._generation += 1
        self._secondary_waits = 0
        for pane in self._panes:
            pane.canvas.message('正在申请视频连接…')
        # 先开云台，枪机等它出帧后再开（并发鉴权会互相顶掉）
        self._start_pane(0)
        self._secondary_timer.start(_SECONDARY_DELAY)

    def _check_secondary(self):
        """云台出帧后才开枪机；没出帧就续等，最多约 24 秒。

        原来是 6 秒到点就无条件开第二路。云台中继慢一点时，这 6 秒正好
        撞上它还在鉴权，两路并发的结果是设备只认后到的那路，于是永远
        只有一格出画面——这正是「双画面还是不能同时显示」的典型成因。
        """
        primary = self._pane(0)
        if (primary is not None and primary.frames == 0
                and self._secondary_waits < 3):
            self._secondary_waits += 1
            self._secondary_timer.start(_SECONDARY_DELAY)
            return
        self._start_pane(1)

    def _start_pane(self, channel):
        pane = self._pane(channel)
        if pane is None or pane.session is not None:
            return
        pane.canvas.message('正在申请视频连接…')
        threading.Thread(target=self._connect, args=(self._generation, pane),
                         daemon=True, name=f'camera-dual-{channel}').start()

    def _connect(self, generation, pane):
        session = CameraSession()
        pane.session = session
        try:
            if pane.channel == 1:
                self._ensure_lens_power(1)
            # 每路各自取一次授权参数：共用同一份参数时两会话会互相顶掉
            source = self._service.prepare_camera_source(self._device)
            url = session.start(
                source, quality=self._quality, channel=pane.channel,
                on_progress=lambda message: self._progress.emit(generation, pane.channel, message)
                if shiboken6.isValid(self) else None)
        except Exception:
            session.stop()
            pane.session = None
            if shiboken6.isValid(self):
                self._failed.emit(generation, pane.channel, '这一路连接失败，可点「重试」')
            return
        if session.cancelled or not shiboken6.isValid(self):
            session.stop()
            return
        self._ready.emit(generation, pane.channel, url)

    def _ensure_lens_power(self, channel):
        piid = _LENS_SWITCH.get(channel)
        if piid is None:
            return
        state = self._service.read_prop_raw(self._device.did, _LENS_SIID, piid)
        if lens_power_on(state):
            return
        self._service.set_prop_raw(self._device.did, _LENS_SIID, piid, True)
        time.sleep(1.2)

    def _on_ready(self, generation, channel, url):
        if generation != self._generation:
            return
        pane = self._pane(channel)
        if pane is None:
            return
        pane.canvas.message('正在载入画面…')
        sink = QVideoSink(self)
        player = QMediaPlayer(self)
        player.setVideoOutput(sink)
        sink.videoFrameChanged.connect(lambda frame, p=pane: self._on_frame(p, frame))
        player.setSource(QUrl(url))
        player.play()
        pane.player, pane.sink = player, sink

    def _on_failed(self, generation, channel, message):
        if generation != self._generation:
            return
        pane = self._pane(channel)
        if pane is not None:
            pane.canvas.message(message)

    def _on_progress(self, generation, channel, message):
        if generation != self._generation:
            return
        pane = self._pane(channel)
        if pane is None:
            return
        text = {'PHASE RELAY': '正在连接公网中继…', 'PHASE AUTH': '正在验证视频会话…',
                'PHASE MEDIA': '正在等待摄像头画面…'}.get(message)
        if text:
            pane.canvas.message(text)
        elif message.startswith('ERROR '):
            pane.canvas.message('这一路连接中断了，点「重试」可重连')

    def _on_frame(self, pane, frame):
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        pane.frames += 1
        pane.canvas.set_frame(image)
        pane.video.set_ratio(image.width(), image.height())
        if pane.channel == 0 and self._secondary_timer.isActive():
            # 云台已经出画面了，不用等满 6 秒
            self._secondary_timer.stop()
            self._start_pane(1)

    def _pane(self, channel):
        for pane in self._panes:
            if pane.channel == channel:
                return pane
        return None

    def _stop_sessions(self):
        self._secondary_timer.stop()
        for pane in self._panes:
            if pane.session is not None:
                pane.session.stop()
                pane.session = None
            if pane.player is not None:
                pane.player.stop()
                pane.player.setSource(QUrl())
                pane.player.deleteLater()
                pane.player = None
            if pane.sink is not None:
                pane.sink.deleteLater()
                pane.sink = None
            pane.frames = 0

    def stop(self):
        self._generation += 1
        self._stop_sessions()

    # ---- 画质 ----

    def _show_quality(self):
        menu = QMenu(self)
        style_popup_menu(menu)
        for value, label in [(1, '480P'), (3, '全分辨率')]:
            action = menu.addAction(label)
            action.setFont(menu.font())
            if value == self._quality:
                try:
                    from qtawesome import icon as qta_icon
                    action.setIcon(qta_icon('mdi.check', color=SiColors.THEME_DIM))
                except Exception:
                    pass
            action.triggered.connect(lambda checked=False, q=value: self._select_quality(q))
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._quality_btn.mapToGlobal(
            QPoint(0, self._quality_btn.height() + 4)))
        menu.aboutToHide.connect(menu.deleteLater)

    def _select_quality(self, quality):
        if quality == self._quality:
            return
        self._quality = quality
        self._quality_btn.setText({1: '480P', 3: '全分辨率'}[quality])
        self.start()

    # ---- 窗口 ----

    def closeEvent(self, event):  # noqa: N802
        self.stop()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):  # noqa: N802
        # 最小化不算关闭；真正关掉窗口才断流
        if not self.isMinimized():
            self.stop()
        super().hideEvent(event)
