# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-camera wall: every camera in one grid, one independent relay per tile.

Each tile is a self-contained session (auth -> relay -> local TS -> QMediaPlayer),
mirroring the single-camera flow in ui/camera_preview.py but without per-tile
audio, fullscreen or PTZ: the wall is a glanceable overview. Double-click a tile
to open the full single-camera window for controls.
"""
import shiboken6
import threading
import time
from PySide6.QtCore import QSize, Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtMultimedia import (QAudioFormat, QAudioSink, QMediaPlayer, QVideoSink)
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)
import qtawesome as qta

from app.core.camera_preview import CameraPreviewError, CameraSession
from app.core.opus_decoder import OUTPUT_RATE as OPUS_RATE, OpusStreamDecoder
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
    audio_solo = Signal(object)      # 请求独占声音：墙负责把其它格子静音
    fullscreen_changed = Signal(object, bool)  # 单格放大 / 复原，墙据此暂停其它格
    _pcm = Signal(int, int, bytes)   # 中继音频线程 → 主线程

    def __init__(self, device, jobs, prepare, parent=None):
        super().__init__(parent)
        self._device = device
        self._jobs = jobs
        self._prepare = prepare
        self._session = None
        self._generation = 0
        self._quality = 1
        self._switching = False
        self._dual_lens = getattr(device, 'model', '') == 'isa.camera.hlmax'
        self._lens = 0            # 0=云台镜头，1=枪机镜头（第二路流）
        self._url = ''
        self._player = None
        self._sink = None
        self._muted = True
        self._audio_sink = None
        self._audio_io = None
        self._audio_rate = 0
        self._opus = None
        self._audio_seen = 0.0
        self._audio_tried = 0.0
        self._frame_size = None
        self._fullscreen = None
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
        self._quality_btn = QPushButton('480P')
        self._quality_btn.setFixedSize(54, 26)
        self._quality_btn.setCursor(Qt.PointingHandCursor)
        self._quality_btn.setToolTip('切换画质')
        self._quality_btn.setEnabled(False)
        self._quality_btn.clicked.connect(lambda: self._show_quality(self._quality_btn))
        header.addWidget(self._quality_btn)
        self._lens_btn = QPushButton('云台')
        self._lens_btn.setFixedSize(44, 26)
        self._lens_btn.setCursor(Qt.PointingHandCursor)
        self._lens_btn.setToolTip('双摄设备：切换云台/枪机镜头')
        self._lens_btn.setVisible(self._dual_lens)
        self._lens_btn.clicked.connect(self._show_lens_menu)
        header.addWidget(self._lens_btn)
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
        self._canvas.mute_clicked.connect(self._on_mute_clicked)
        self._canvas.quality_clicked.connect(lambda: self._show_quality(self._canvas.quality))
        self._canvas.fullscreen_clicked.connect(self._toggle_fullscreen)
        self._pcm.connect(self._on_pcm)
        self._body = lay
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
        self._lens_btn.setStyleSheet(self._quality_btn.styleSheet())

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
                    url = session.start(
                        source, quality=self._quality, channel=self._lens,
                        on_progress=lambda value: self.progress.emit(generation, value)
                        if shiboken6.isValid(self) else None,
                        on_audio=lambda rate, data: self._pcm.emit(generation, rate, data)
                        if shiboken6.isValid(self) else None)
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
        self._stop_audio()
        if session is not None:
            session.stop()
        self._url = ''
        self._frame_size = None
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
        # 声音是会话级开关：重连（换画质/重试）后要把用户的选择重新下发
        if not self._muted and self._session is not None:
            self._session.command('AUDIO', 1)

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
        elif message == 'NOTICE AUDIO_UNSUPPORTED':
            self._muted = True
            self._stop_audio()
            self._canvas.set_muted(True)
            self._canvas.mute.setToolTip('此设备的音频编码暂不支持')
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
        # 只在分辨率真的变化时改文案：setToolTip 每帧调用会产生大量字符串与样式重算
        size = (frame.width(), frame.height())
        if size != getattr(self, '_frame_size', None):
            self._frame_size = size
            self._canvas.quality.setText({1: '480P', 3: '全分辨率'}[self._quality])
            self._canvas.quality.setToolTip(f'切换画质 · 当前 {size[0]} × {size[1]}')

    def _on_player_error(self, error, _message):
        if self._session is not None and not self._switching and error != QMediaPlayer.Error.NoError:
            self._on_failed(self._generation, CameraPreviewError(
                '播放失败，请点击画面重试'))

    def _on_timeout(self):
        self.stop()
        self._canvas.message('预览超时，请点击画面重试')

    # ---- audio ----
    def _on_mute_clicked(self):
        if self._session is None or not self._url:
            self._canvas.message('画面加载完成后才能开启声音', clear=False)
            return
        if self._muted:
            # 九宫格同时出声会糊成一团：一次只让一路发声，墙负责静音其它格
            self.audio_solo.emit(self)
        else:
            self._set_audio(False)

    def set_audio_enabled(self, enabled):
        self._set_audio(bool(enabled))

    def _set_audio(self, enabled):
        if enabled == (not self._muted):
            return
        if enabled:
            if self._session is None or not self._url:
                return
            if not self._session.command('AUDIO', 1):
                self._canvas.message('无法开启声音，请重试', clear=False)
                return
            self._muted = False
            self._canvas.set_muted(False)
            self._audio_tried = time.monotonic()
            self._canvas.message('正在开启声音…', clear=False)
            # 中继会重启视频流并打开 enableaudio，3.5 秒没 PCM 基本就是没音频
            QTimer.singleShot(3500, self, self._check_audio_arrived)
            return
        self._muted = True
        self._canvas.set_muted(True)
        self._stop_audio()
        if self._session is not None:
            self._session.command('AUDIO', 0)

    def _check_audio_arrived(self):
        if not shiboken6.isValid(self) or self._muted:
            return
        if self._audio_seen > self._audio_tried:
            return
        self._canvas.message('该摄像头未输出声音（麦克风可能已关闭，或该机型不带音频）', clear=False)

    def _on_pcm(self, generation, rate, data):
        if generation != self._generation or self._muted:
            return
        if rate == 0:
            # Opus 原始包（codecId 1032，中继只透传）：本机用 libopus 解码
            if self._opus is None:
                self._opus = OpusStreamDecoder()
            data = self._opus.decode(data)
            if not data:
                if self._opus.error:
                    self._canvas.message('音频解码组件不可用：' + self._opus.error, clear=False)
                    self._set_audio(False)
                return
            rate = OPUS_RATE
        self._audio_seen = time.monotonic()
        if self._audio_sink is None or self._audio_rate != rate:
            self._stop_audio()
            fmt = QAudioFormat()
            fmt.setSampleRate(rate)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.Int16)
            self._audio_sink = QAudioSink(fmt, self)
            self._audio_sink.setBufferSize(rate // 2)
            self._audio_sink.setVolume(.8)
            self._audio_io = self._audio_sink.start()
            self._audio_rate = rate
            if self._audio_io is None:
                self._canvas.message('无法打开系统音频输出设备', clear=False)
                self._set_audio(False)
                return
        if self._audio_io and self._audio_sink.bytesFree() >= len(data):
            self._audio_io.write(data)

    def _stop_audio(self):
        if self._audio_sink is not None:
            self._audio_sink.stop()
            self._audio_sink.deleteLater()
        self._audio_sink = self._audio_io = None
        self._audio_rate = 0
        if self._opus is not None:
            self._opus.close()
            self._opus = None

    # ---- fullscreen（单格放大）----
    def _toggle_fullscreen(self):
        if self._fullscreen is not None:
            self._fullscreen.reject()
            return
        window = QDialog(self.window(), Qt.Window | Qt.FramelessWindowHint)
        window.setWindowModality(Qt.WindowModal)
        window.setStyleSheet('background: black; border: none;')
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._body.removeWidget(self._canvas)
        layout.addWidget(self._canvas)
        self._canvas.show()
        self._fullscreen = window
        self._canvas.set_fullscreen(True)
        self.fullscreen_changed.emit(self, True)

        def restore(_code=0):
            if not shiboken6.isValid(self):
                return
            if shiboken6.isValid(self._canvas):
                layout.removeWidget(self._canvas)
                self._canvas.setParent(self)
                self._body.insertWidget(1, self._canvas, 1)
                self._canvas.set_fullscreen(False)
                self._canvas.show()
            self._fullscreen = None
            if shiboken6.isValid(window):
                window.deleteLater()
            self.fullscreen_changed.emit(self, False)

        window.finished.connect(restore)
        window.showFullScreen()

    def exit_fullscreen(self):
        if self._fullscreen is not None and shiboken6.isValid(self._fullscreen):
            self._fullscreen.reject()

    # ---- quality ----
    def _show_quality(self, anchor=None):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        anchor = anchor if isinstance(anchor, QPushButton) else self._quality_btn
        menu = QMenu(self)
        style_popup_menu(menu)
        for value, label in [(1, '480P'), (3, '全分辨率')]:
            action = menu.addAction(label)
            action.setData(value)
            if value == self._quality:
                from qtawesome import icon as qta_icon
                action.setIcon(qta_icon('mdi.check', color=SiColors.THEME_DIM))
            action.triggered.connect(
                lambda checked=False, q=value: self._select_quality(q))
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(anchor.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))
        menu.aboutToHide.connect(menu.deleteLater)

    def _select_quality(self, quality):
        if quality == self._quality:
            return
        self._quality = quality
        label = {1: '480P', 3: '全分辨率'}[quality]
        self._quality_btn.setText(label)
        self._canvas.quality.setText(label)
        if self._session is None:
            return
        # 画质是会话级参数：必须重开会话才会真正换分辨率（详见单摄像头页）
        self._canvas.message('正在切换画质…', clear=False)
        self.stop()
        QTimer.singleShot(150, self, self.start)

    # ---- dual lens ----
    def _show_lens_menu(self):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        menu = QMenu(self)
        style_popup_menu(menu)
        for channel, label in [(0, '云台镜头（可转向）'), (1, '枪机镜头（固定广角）')]:
            action = menu.addAction(label)
            action.setData(channel)
            if channel == self._lens:
                from qtawesome import icon as qta_icon
                action.setIcon(qta_icon('mdi.check', color=SiColors.THEME_DIM))
            action.triggered.connect(
                lambda checked=False, c=channel: self._select_lens(c))
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._lens_btn.mapToGlobal(
            QPoint(0, -menu.sizeHint().height() - 4)))
        menu.aboutToHide.connect(menu.deleteLater)

    def _select_lens(self, channel):
        if channel == self._lens:
            return
        self._lens = channel
        self._lens_btn.setText('云台' if channel == 0 else '枪机')
        # 第二路镜头要求该镜头电源是开的，先让墙去补一下属性
        if callable(getattr(self, '_ensure_lens', None)):
            self._ensure_lens(self._device, channel)
        if self._session is None:
            return
        self._canvas.message('正在切换镜头…', clear=False)
        self.stop()
        QTimer.singleShot(1800, self, self.start)

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
        self._suspended = False   # 大窗/单格全屏期间，其余格子主动断开
        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(600)
        self._pump_timer.timeout.connect(self._pump)
        self.setWindowTitle('摄像头墙 · MijiaDeck')
        self.setModal(False)
        self.setMinimumSize(680, 420)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 12)
        self._layout.setSpacing(8)
        self._header = QHBoxLayout()
        self._header.setContentsMargins(0, 0, 0, 0)
        self._header.setSpacing(6)
        self._title = QLabel(f'摄像头墙 · {len(devices)} 台')
        self._title.setStyleSheet(f'color: {SiColors.TEXT_PRIMARY}; font-size: 12pt; font-weight: 600;')
        self._header.addWidget(self._title, 1)
        # 整墙全屏：九宫格铺满屏幕，比逐格全屏更适合总览
        self._fs_btn = QPushButton()
        self._fs_btn.setFixedSize(30, 26)
        self._fs_btn.setCursor(Qt.PointingHandCursor)
        self._fs_btn.setIconSize(QSize(18, 18))
        self._fs_btn.setToolTip('全屏显示（Esc 退出）')
        self._fs_btn.clicked.connect(self._toggle_fullscreen)
        self._header.addWidget(self._fs_btn)
        self._layout.addLayout(self._header)
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
            tile.audio_solo.connect(self._solo_audio)
            tile.fullscreen_changed.connect(self._on_tile_fullscreen)
            tile._ensure_lens = self._ensure_lens
            self._tiles[device.did] = tile
            self._grid.addWidget(tile, index // cols, index % cols)
        for col in range(cols):
            self._grid.setColumnStretch(col, 1)
        self.retheme()

    def retheme(self):
        for tile in self._tiles.values():
            if shiboken6.isValid(tile):
                tile.retheme()
        if shiboken6.isValid(self._fs_btn):
            self._fs_btn.setStyleSheet(
                f'QPushButton {{ background: {SiColors.SURFACE}; border: none; border-radius: 7px; }} '
                f'QPushButton:hover {{ background: {SiColors.CARD_HOVER}; }}')
            self._sync_fullscreen_button()

    def _sync_fullscreen_button(self):
        if not shiboken6.isValid(self._fs_btn):
            return
        full = self.isFullScreen()
        self._fs_btn.setIcon(qta.icon('mdi.fullscreen-exit' if full else 'mdi.fullscreen',
                                      color=SiColors.TEXT_SECONDARY))
        self._fs_btn.setToolTip('退出全屏（Esc）' if full else '全屏显示（Esc 退出）')

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self._sync_fullscreen_button()

    def keyPressEvent(self, event):  # noqa: N802
        # 全屏时 Esc 只退全屏，不关掉整面墙
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            self._sync_fullscreen_button()
            event.accept()
            return
        super().keyPressEvent(event)

    def _ensure_lens(self, device, channel):
        """双摄 hlmax：目标镜头的电源开关（siid 6 piid 25/26）必须是开的，
        否则第二路请求回来只有连接、没有画面。"""
        if getattr(device, 'model', '') != 'isa.camera.hlmax':
            return
        piid = {0: 25, 1: 26}.get(channel)
        if piid is None:
            return

        def work():
            state = self._service.read_prop_raw(device.did, 6, piid)
            if state is not True:
                self._service.set_prop_raw(device.did, 6, piid, True)
                time.sleep(1.5)
            return True

        self._jobs.submit(work)

    # ---- 大窗独占：只留看得见的那一路 ----
    def _on_tile_fullscreen(self, tile, active):
        """单格全屏时其它格没人看，断开它们省解码、省上行带宽。"""
        if active:
            self._suspend_tiles(keep=tile)
        else:
            self._resume_tiles()

    def _suspend_tiles(self, keep=None):
        if self._suspended:
            return
        self._suspended = True
        self._pump_timer.stop()
        self._pending.clear()
        self._active.clear()
        for tile in self._tiles.values():
            if not shiboken6.isValid(tile) or tile is keep:
                continue
            if tile._session is None:
                continue
            tile.stop()
            tile._canvas.message('大窗打开中，已暂停')
        self._update_status()

    def _resume_tiles(self):
        if not self._suspended:
            return
        self._suspended = False
        if not self.isVisible():
            return
        self._pending = [t for t in self._tiles.values()
                         if shiboken6.isValid(t) and t._device.online and t._session is None]
        self._pump()
        if self._pending:
            self._pump_timer.start()

    def _solo_audio(self, tile):
        """同一时刻只让一路发声：先静音其它格，再开这一路。"""
        if not shiboken6.isValid(tile):
            return
        for other in self._tiles.values():
            if other is not tile and shiboken6.isValid(other):
                other.set_audio_enabled(False)
        tile.set_audio_enabled(True)

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
        if self._suspended:
            text += '（大窗播放中，其余已暂停）'
        elif waiting or done < total:
            text += f'（已出画面 {done}，排队 {waiting}）'
        self._title.setText(text)

    def _pump(self):
        if self._suspended:
            return
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
            # 大窗自己也占一路视频：九宫格先全断，关掉大窗再自动恢复
            dialog.finished.connect(lambda _code: self._resume_tiles())
            dialog.destroyed.connect(lambda *_: self._resume_tiles())
            self._suspend_tiles()
            dialog.show()
            dialog.activateWindow()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._suspended or self._pending or self._active:
            return
        # 先按网格顺序排队，由 _pump 控制同时最多 _MAX_ACTIVE 路
        self._pending = [t for t in self._tiles.values() if t._device.online]
        self._pump()
        if self._pending:
            self._pump_timer.start()

    def closeEvent(self, event):  # noqa: N802
        self._suspended = False
        self._pump_timer.stop()
        self._pending.clear()
        self._active.clear()
        for tile in self._tiles.values():
            if shiboken6.isValid(tile):
                tile.exit_fullscreen()
                tile.stop()
        super().closeEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._suspended = False
        self._pump_timer.stop()
        self._pending.clear()
        self._active.clear()
        for tile in self._tiles.values():
            if shiboken6.isValid(tile):
                tile.exit_fullscreen()
                tile.stop()
        super().hideEvent(event)
