# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-camera wall: every camera in one grid, one independent relay per tile.

Each tile is a self-contained session (auth -> relay -> local TS -> QMediaPlayer),
mirroring the single-camera flow in ui/camera_preview.py but without per-tile
audio, fullscreen or PTZ: the wall is a glanceable overview. Double-click a tile
to open the full single-camera window for controls.
"""
import shiboken6
import threading
from PySide6.QtCore import Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from app.core.camera_preview import CameraPreviewError, CameraSession
from app.ui.camera_canvas import CameraCanvas
from app.ui.si_theme import SiColors
from app.ui.popup_menu import style_popup_menu

_TILE_MIN_WIDTH = 300
_TILE_MIN_HEIGHT = 180


class _CameraTile(QFrame):
    """One camera slot on the wall: independent session, minimal controls."""
    connected = Signal(int, str)
    failed = Signal(int, object)
    progress = Signal(int, str)
    open_requested = Signal(str)

    def __init__(self, device, jobs, prepare, parent=None):
        super().__init__(parent)
        self._device = device
        self._jobs = jobs
        self._prepare = prepare
        self._session = None
        self._generation = 0
        self._quality = 1
        self._switching = False
        self._url = ''
        self._player = None
        self._sink = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self.setObjectName('cameraTile')
        self.setMinimumSize(_TILE_MIN_WIDTH, _TILE_MIN_HEIGHT)
        self.setMouseTracking(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        self._name = QLabel(f'{device.name}' + ('' if device.online else '（离线）'))
        self._name.setStyleSheet(f'color: {SiColors.TEXT_PRIMARY}; background: transparent; font-weight: 600;')
        header.addWidget(self._name, 1)
        self._quality_btn = QPushButton('流畅')
        self._quality_btn.setFixedSize(54, 26)
        self._quality_btn.setCursor(Qt.PointingHandCursor)
        self._quality_btn.setToolTip('切换画质')
        self._quality_btn.setEnabled(False)
        self._quality_btn.clicked.connect(self._show_quality)
        header.addWidget(self._quality_btn)
        self._open_btn = QPushButton('大窗')
        self._open_btn.setFixedSize(44, 26)
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.setToolTip('打开单摄像头控制窗口')
        self._open_btn.clicked.connect(lambda: self.open_requested.emit(device.did))
        header.addWidget(self._open_btn)
        lay.addLayout(header)
        self._canvas = CameraCanvas(self)
        self._canvas.message('等待开始…' if device.online else '摄像头已离线')
        self._canvas.set_controls_enabled(False)
        self._canvas.retry.connect(self.start)
        lay.addWidget(self._canvas, 1)
        self.retheme()

    # ---- theme ----
    def retheme(self):
        self.setStyleSheet(
            f'QFrame#cameraTile {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 10px; }}')
        self._quality_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {SiColors.TEXT_PRIMARY}; '
            f'border: 1px solid {SiColors.LINE}; border-radius: 6px; font-size: 9pt; }}')
        self._open_btn.setStyleSheet(self._quality_btn.styleSheet())

    # ---- session flow (condensed from ui/camera_preview.py) ----
    def start(self):
        if not self._device.online or self._session is not None:
            return
        self._generation += 1
        generation = self._generation
        self._canvas.message('正在申请视频连接…')
        session = CameraSession()
        self._session = session
        self._timeout.start(100000)

        def success(source):
            if not shiboken6.isValid(self) or generation != self._generation:
                session.stop()
                return

            def connect_relay():
                try:
                    url = session.start(source, quality=self._quality)
                    if session.cancelled or not shiboken6.isValid(self):
                        session.stop()
                    else:
                        self.connected.emit(generation, url)
                except Exception as error:
                    session.stop()
                    if shiboken6.isValid(self):
                        safe = error if isinstance(error, CameraPreviewError) else \
                            CameraPreviewError('远程视频连接失败，请重试。')
                        self.failed.emit(generation, safe)

            threading.Thread(target=connect_relay, daemon=True,
                             name='camera-wall-connect').start()

        def failure(error):
            session.stop()
            if shiboken6.isValid(self):
                self._on_failed(generation, error)

        self._jobs.submit(self._prepare, on_success=success, on_error=failure)

    def stop(self):
        self._generation += 1
        self._timeout.stop()
        self._switching = False
        session, self._session = self._session, None
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if session is not None:
            session.stop()
        self._canvas.set_controls_enabled(False)

    def _on_connected(self, generation, url):
        if generation != self._generation or self._session is None:
            return
        self._canvas.message('正在载入画面…')
        self._timeout.start(25000)
        self._url = url
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._sink = QVideoSink(self)
            self._player.setVideoOutput(self._sink)
            self._sink.videoFrameChanged.connect(self._on_frame)
            self._player.errorOccurred.connect(self._on_player_error)
        self._player.setSource(QUrl(url))
        self._player.play()

    def _on_failed(self, generation, error):
        if generation != self._generation:
            return
        self.stop()
        self._canvas.message(str(error) if isinstance(error, CameraPreviewError)
                             else '视频连接失败，请点击画面重试')

    def _on_progress(self, generation, message):
        if generation != self._generation:
            return
        text = {'PHASE RELAY': '正在连接公网中继…', 'PHASE AUTH': '正在验证视频会话…',
                'PHASE MEDIA': '等待摄像头画面…'}
        if message.startswith('ERROR '):
            from app.core.camera_preview import ERRORS
            self._on_failed(generation, CameraPreviewError(
                ERRORS.get(message[6:], '视频连接中断，请重试'), code=message[6:]))
        elif message in text:
            self._canvas.message(text[message])

    def _on_frame(self, frame):
        if self._session is None or self._switching or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._timeout.stop()
        self._canvas.set_frame(image)
        self._canvas.set_controls_enabled(True)
        self._quality_btn.setEnabled(True)

    def _on_player_error(self, error, _message):
        if self._session is not None and not self._switching and error != QMediaPlayer.Error.NoError:
            self._on_failed(self._generation, CameraPreviewError(
                '播放失败，请点击画面重试'))

    def _on_timeout(self):
        self.stop()
        self._canvas.message('预览超时，请点击画面重试')

    # ---- quality ----
    def _show_quality(self):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        menu = QMenu(self)
        style_popup_menu(menu)
        for value, label in [(0, '自动'), (1, '流畅'), (2, '高清'), (3, '全分辨率')]:
            action = menu.addAction(label)
            action.setData(value)
            if value == self._quality:
                from qtawesome import icon as qta_icon
                action.setIcon(qta_icon('mdi.check', color=SiColors.THEME_DIM))
            action.triggered.connect(
                lambda checked=False, q=value: self._select_quality(q))
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._quality_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))
        menu.aboutToHide.connect(menu.deleteLater)

    def _select_quality(self, quality):
        if quality == self._quality:
            return
        self._quality = quality
        self._quality_btn.setText(
            {0: '自动', 1: '流畅', 2: '高清', 3: '全分辨率'}[quality][:5])
        if self._session is None:
            return
        # 画质是会话级参数：必须重开会话才会真正换分辨率（详见单摄像头页）
        self._canvas.message('正在切换画质…', clear=False)
        self.stop()
        QTimer.singleShot(150, self, self.start)

    def mouseDoubleClickEvent(self, event):
        self.open_requested.emit(self._device.did)
        super().mouseDoubleClickEvent(event)


class CameraWallDialog(QDialog):
    """Grid overview of every camera on the account."""

    # 每格都是独立 JVM + 完整会话鉴权，九路同时开会把 CPU/带宽打满，
    # 表现为「连得慢又卡」。限制并发、错峰启动，连上一个再补下一个。
    _MAX_ACTIVE = 3

    def __init__(self, service, jobs, devices, parent=None):
        super().__init__(parent)
        self._service, self._jobs, self._devices = service, jobs, devices
        self._tiles: dict[str, _CameraTile] = {}
        self._pending: list[_CameraTile] = []
        self._active: list[_CameraTile] = []
        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(600)
        self._pump_timer.timeout.connect(self._pump)
        self.setWindowTitle('摄像头墙 · MijiaDeck')
        self.setModal(False)
        self.setMinimumSize(680, 420)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 12)
        self._layout.setSpacing(8)
        self._title = QLabel(f'摄像头墙 · {len(devices)} 台')
        self._title.setStyleSheet(f'color: {SiColors.TEXT_PRIMARY}; font-size: 12pt; font-weight: 600;')
        self._layout.addWidget(self._title)
        host = QWidget(self)
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self._layout.addWidget(host, 1)
        cams = [d for d in devices if '.camera.' in d.model]
        cols = 1 if len(cams) <= 1 else (2 if len(cams) <= 4 else 3)
        for index, device in enumerate(cams):
            tile = _CameraTile(device, jobs,
                               lambda d=device: service.prepare_camera_source(d), host)
            tile.connected.connect(self._on_connected)
            tile.failed.connect(self._on_failed)
            tile.progress.connect(self._on_progress)
            tile.open_requested.connect(self._open_single)
            self._tiles[device.did] = tile
            self._grid.addWidget(tile, index // cols, index % cols)
        for col in range(cols):
            self._grid.setColumnStretch(col, 1)
        self.retheme()

    def retheme(self):
        for tile in self._tiles.values():
            if shiboken6.isValid(tile):
                tile.retheme()

    def _on_connected(self, generation, url):
        sender = self.sender()
        if isinstance(sender, _CameraTile):
            sender._on_connected(generation, url)
            self._release(sender)

    def _on_failed(self, generation, error):
        sender = self.sender()
        if isinstance(sender, _CameraTile):
            sender._on_failed(generation, error)
            self._release(sender)

    def _on_progress(self, generation, message):
        sender = self.sender()
        if isinstance(sender, _CameraTile):
            sender._on_progress(generation, message)

    def _release(self, tile):
        """一格结束（成功或失败）就空出名额，让下一台开始连。"""
        if tile in self._active:
            self._active.remove(tile)
        self._update_status()
        self._pump()

    def _update_status(self):
        if not shiboken6.isValid(self._title):
            return
        total = len(self._tiles)
        done = sum(1 for t in self._tiles.values() if shiboken6.isValid(t) and t._url)
        waiting = len(self._pending)
        text = f'摄像头墙 · {total} 台'
        if waiting or done < total:
            text += f'（已出画面 {done}，排队 {waiting}）'
        self._title.setText(text)

    def _pump(self):
        # 已出画面或已放弃的那一格让出并发名额
        for tile in list(self._active):
            if not shiboken6.isValid(tile) or tile._url or tile._session is None:
                self._active.remove(tile)
        while self._pending and len(self._active) < self._MAX_ACTIVE:
            tile = self._pending.pop(0)
            if not shiboken6.isValid(tile) or not tile._device.online:
                continue
            self._active.append(tile)
            tile.start()
        self._update_status()
        if not self._pending and not self._active:
            self._pump_timer.stop()

    def _open_single(self, did):
        device = next((d for d in self._devices if d.did == did), None)
        if device is not None:
            from app.ui.device_dialog import DeviceDetailDialog
            dialog = DeviceDetailDialog(self._service, self._jobs, device, self)
            dialog.load()
            dialog.show()
            dialog.activateWindow()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._pending or self._active:
            return
        # 先按网格顺序排队，由 _pump 控制同时最多 _MAX_ACTIVE 路
        self._pending = [t for t in self._tiles.values() if t._device.online]
        self._pump()
        if self._pending:
            self._pump_timer.start()

    def closeEvent(self, event):  # noqa: N802
        self._pump_timer.stop()
        self._pending.clear()
        self._active.clear()
        for tile in self._tiles.values():
            tile.stop()
        super().closeEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._pump_timer.stop()
        self._pending.clear()
        self._active.clear()
        for tile in self._tiles.values():
            tile.stop()
        super().hideEvent(event)
