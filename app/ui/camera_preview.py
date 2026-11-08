# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated camera page: physical power, remote preview, audio and fullscreen."""
import threading
import time
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink, QAudioSink, QAudioFormat
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget
from app.core.camera_preview import CameraPreviewError, CameraSession, ERRORS
from app.core.opus_decoder import OUTPUT_RATE as OPUS_RATE, OpusStreamDecoder
from app.ui.camera_canvas import AspectVideoLayout, CameraCanvas
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors
from app.ui.popup_menu import style_popup_menu
import qtawesome as qta


class CameraPreviewPanel(QFrame):
    _connected = Signal(int, str)
    _failed = Signal(int, object)
    _progress = Signal(int, str)
    _pcm = Signal(int, int, bytes)

    def __init__(self, service, jobs, device, parent=None):
        super().__init__(parent)
        self._service, self._jobs, self._device = service, jobs, device
        self._diagnostic_since = time.monotonic()
        self._diagnostic = {'attempted': False, 'stage': 'idle', 'timeline': []}
        self._session = None
        self._generation = 0
        self._playing = False
        self._loaded = False
        self._read_pending = False
        self._power_busy = False
        self._power_state = None
        self._power_deadline = 0
        self._attempted = False
        self._quality = 2  # 单摄像头页默认高清：360P 子码流在桌面端太糊，仍可在菜单切回流畅
        self._muted = True
        self._audio_sink = None
        self._audio_io = None
        self._audio_rate = 0
        self._opus = None
        self._fullscreen = None
        self._drag_ready_at = 0
        self._url = ''
        self._switching_quality = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(16)
        self.header = QHBoxLayout()
        self.header.setSpacing(14)
        labels = QVBoxLayout()
        labels.setSpacing(4)
        self._title = QLabel(getattr(device, 'name', '摄像头'))
        self._title.setWordWrap(True)
        self._room = QLabel(getattr(device, 'room_name', ''))
        labels.addWidget(self._title)
        labels.addWidget(self._room)
        self.header.addLayout(labels, 1)
        self._diagnostic_button = QPushButton('诊断报告')
        self._diagnostic_button.clicked.connect(self._show_diagnostics)
        self.header.addWidget(self._diagnostic_button)
        self._panel_btn = QPushButton('功能')
        self._panel_btn.setCursor(Qt.PointingHandCursor)
        self._panel_btn.setToolTip('查看这台摄像头的全部可控功能、动作与设备信息')
        self._panel_btn.clicked.connect(self._show_device_panel)
        self.header.addWidget(self._panel_btn)
        self._dual_lens = getattr(device, 'model', '') == 'isa.camera.hlmax'
        self._lens_channel = 0   # 0=主镜头（云台），1=第二路（枪机，通道模式）
        self._lens_value = None  # 最近一次通过属性写入的镜头值
        self._audio_seen = 0.0   # 最近一次收到 PCM 的时刻
        self._audio_tried = 0.0  # 最近一次请求开启声音的时刻
        if self._dual_lens:
            self._lens_btn = QPushButton('切换镜头')
            self._lens_btn.setCursor(Qt.PointingHandCursor)
            self._lens_btn.setToolTip('双摄设备：切换云台/枪机镜头画面')
            self._lens_btn.clicked.connect(self._show_lens_menu)
            self.header.addWidget(self._lens_btn)
        self._power = PowerButton(44, 28, self)
        self._power.set_online(device.online)
        self._power.setToolTip('摄像头开关')
        self._power.clicked.connect(self._toggle_power)
        self.header.insertWidget(0, self._power, alignment=Qt.AlignVCenter)
        self._layout.addLayout(self.header)
        self._canvas = CameraCanvas(self)
        self._status = self._canvas._notice
        self._video_layout = AspectVideoLayout()
        self._video_layout.addWidget(self._canvas)
        self._layout.addLayout(self._video_layout, 1)
        self._drag_hint = QLabel('拖拽画面可调整摄像头角度', self)
        self._drag_hint.setAlignment(Qt.AlignCenter)
        self._drag_hint.setVisible(device.model == 'chuangmi.camera.029a02')
        self._hint_slot = QWidget(self)
        self._hint_slot.setVisible(device.model == 'chuangmi.camera.029a02')
        self._layout.addWidget(self._hint_slot)
        self._canvas.installEventFilter(self)
        self._canvas.retry.connect(self._retry)
        self._canvas.mute_clicked.connect(self._toggle_mute)
        self._canvas.quality_clicked.connect(self._show_quality)
        self._canvas.fullscreen_clicked.connect(self._toggle_fullscreen)
        self._canvas.dragged.connect(self._drag)
        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._sink)
        self._player.errorOccurred.connect(self._on_player_error)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._connected.connect(self._on_connected)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)
        self._pcm.connect(self._on_pcm)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._poll = QTimer(self)
        self._poll.setInterval(5000)
        self._poll.timeout.connect(self._refresh_power)
        QApplication.instance().aboutToQuit.connect(self.stop)
        self.retheme()

    def _show_diagnostics(self):
        from app.ui.diagnostic_dialog import show_device_diagnostics
        show_device_diagnostics(self, self._service, self._jobs, [self._device], self._diagnostic_snapshot)

    def _show_device_panel(self):
        """摄像头的其它功能（夜视、移动侦测、SD 卡、重启、PTZ 校准等）。"""
        from app.ui.device_panel import DevicePanelDialog
        if getattr(self, '_device_panel', None) is not None and shiboken6.isValid(self._device_panel):
            self._device_panel.show()
            self._device_panel.raise_()
            self._device_panel.activateWindow()
            return
        dialog = DevicePanelDialog(self._service, self._jobs, self._device, self)
        self._device_panel = dialog
        dialog.finished.connect(lambda _code: setattr(self, '_device_panel', None))
        dialog.show()

    # ---- 双摄镜头切换（isa.camera.hlmax：dome 云台 + box 枪机）----

    _LENS_SIID = 6        # urn:isa-spec:service:other-functions
    _LENS_PIID = 21       # hl-camera-switch: 0全关 1仅dome 2仅box 3全开

    def _show_lens_menu(self):
        menu = QMenu(self)
        style_popup_menu(menu)
        actions = {}
        # (属性值, 通道, 说明)：hlmax 的镜头既可能由 hl-camera-switch 属性控制，
        # 也可能由 MISS 视频请求的 channel 决定。两种都给出，让用户逐个试。
        options = [(1, 0, '云台镜头（可拖动转向）'),
                   (2, 0, '枪机镜头（固定视角）')]
        for value, channel, label in options:
            action = menu.addAction(label)
            action.setFont(menu.font())
            action.triggered.connect(
                lambda checked=False, v=value, c=channel: self._switch_lens(v, c))
            actions[(value, channel)] = action
        menu.addSeparator()
        for value, channel, label in [(2, 1, '枪机镜头（通道模式）'),
                                      (1, 0, '云台镜头（通道模式）')]:
            action = menu.addAction(label)
            action.setFont(menu.font())
            action.triggered.connect(
                lambda checked=False, v=value, c=channel: self._switch_lens(v, c, prop=False))
            actions[(value, channel)] = action
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._lens_btn.mapToGlobal(QPoint(0, self._lens_btn.height() + 4)))
        menu.aboutToHide.connect(menu.deleteLater)
        self._mark_current_lens(menu, actions, options)

    def _mark_current_lens(self, menu, actions, options):
        """异步读当前镜头再勾选：同步读最坏要等 5 秒，会把界面冻住。"""
        holder = {'value': None, 'done': False}

        def read():
            holder['value'] = self._service.read_prop_raw(
                self._device.did, self._LENS_SIID, self._LENS_PIID)
            holder['done'] = True

        threading.Thread(target=read, daemon=True, name='lens-probe').start()
        timer = QTimer(self)
        timer.setInterval(150)
        waited = {'ms': 0}

        def tick():
            waited['ms'] += 150
            expired = waited['ms'] > 5000
            if not holder['done'] and not expired:
                return
            timer.stop()
            timer.deleteLater()
            value = holder['value']
            if value in (1, 2) and shiboken6.isValid(menu) and menu.isVisible():
                action = actions.get((value, self._lens_channel))
                if action is not None:
                    try:
                        action.setIcon(qta.icon('mdi.check', color=SiColors.THEME_DIM))
                    except Exception:
                        pass

        timer.timeout.connect(tick)
        timer.start()

    def _switch_lens(self, value, channel=0, prop=True):
        self._lens_btn.setEnabled(False)
        self._canvas.message('正在切换镜头…', clear=False)
        name = {1: '云台', 2: '枪机'}.get(value, '')
        self._lens_channel = channel
        self._lens_value = value if prop else None
        if not prop:
            # 通道模式：不动设备属性，直接用新会话请求第二路视频
            self.start(hint=f'正在按通道切换到{name}镜头…')
            self._lens_btn.setEnabled(True)
            return

        def work():
            self._service.set_prop_raw(
                self._device.did, self._LENS_SIID, self._LENS_PIID, value)
            # 镜头模组切换有固件/机械延迟，立刻重连会拿到旧镜头那一路上行
            time.sleep(1.8)
            return self._service.read_prop_raw(
                self._device.did, self._LENS_SIID, self._LENS_PIID)

        def success(readback):
            if not shiboken6.isValid(self):
                return
            self._lens_btn.setEnabled(True)
            if readback is not None and readback != value:
                actual = {0: '已关闭', 1: '云台', 2: '枪机', 3: '双镜头'}.get(readback, '其他状态')
                self._canvas.message(f'设备回报仍是{actual}，切换未生效', clear=False)
                return
            if readback is None:
                self._canvas.message('设备未回报镜头状态，正在按请求重连…', clear=False)
            self.start(hint=f'正在切换到{name}镜头…')

        def failure(error):
            if shiboken6.isValid(self):
                self._lens_btn.setEnabled(True)
                self._canvas.message(f'镜头切换失败：{error}', clear=False)

        self._jobs.submit(work, on_success=success, on_error=failure)

    def _diagnostic_snapshot(self):
        result = dict(self._diagnostic)
        result['timeline'] = list(result['timeline'])
        return result

    def _record_diagnostic(self, stage, code=None, **values):
        self._diagnostic.update(stage=stage, elapsed_ms=round((time.monotonic() - self._diagnostic_since) * 1000), **values)
        if code is not None:
            self._diagnostic['error_code'] = code
        self._diagnostic['timeline'].append({'stage': stage, 'code': code,
                                              'elapsed_ms': self._diagnostic['elapsed_ms']})
        self._diagnostic['timeline'] = self._diagnostic['timeline'][-24:]
        recorder = getattr(self._service, 'record_device_observation', None)
        if callable(recorder):
            recorder(self._device.did, self._diagnostic_snapshot())

    def retheme(self):
        self.setStyleSheet('QFrame { background: transparent; border: none; }')
        self._title.setStyleSheet(f'color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 15pt; font-weight: 600;')
        self._room.setStyleSheet(f'color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;')
        hint_color = QColor(SiColors.TEXT_SECONDARY)
        hint_color.setAlphaF(.65)
        self._drag_hint.setStyleSheet(f'color: {hint_color.name(QColor.HexArgb)}; background: transparent; font-size: 8pt;')
        self._hint_slot.setFixedHeight(self._drag_hint.sizeHint().height())
        QTimer.singleShot(0, self, self._position_drag_hint)
        self._power._apply()
        self._diagnostic_button.setStyleSheet(f'QPushButton {{ background: {SiColors.SURFACE}; '
            f'color: {SiColors.TEXT_SECONDARY}; border: none; border-radius: 8px; padding: 7px 12px; }}')
        if hasattr(self, '_panel_btn'):
            self._panel_btn.setStyleSheet(self._diagnostic_button.styleSheet())
        if self._dual_lens:
            self._lens_btn.setStyleSheet(self._diagnostic_button.styleSheet())

    def _position_drag_hint(self):
        if self._fullscreen is not None or not self._drag_hint.isVisible():
            return
        video_bottom = self._canvas.geometry().bottom() + 1
        window_bottom = self.mapFrom(self.window(), QPoint(0, self.window().height() - 1)).y()
        height = self._drag_hint.sizeHint().height()
        self._drag_hint.setGeometry(0, round((video_bottom + window_bottom - height) / 2),
                                    self.width(), height)
        self._drag_hint.raise_()

    def eventFilter(self, watched, event):
        if watched is self._canvas and event.type() in (QEvent.Resize, QEvent.Move):
            QTimer.singleShot(0, self, self._position_drag_hint)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self, self._position_drag_hint)

    def load(self):
        self._loaded = True
        self._refresh_power()
        if self.isVisible():
            self._poll.start()

    def _refresh_power(self):
        if self._read_pending or self._power_busy or not self._device.online:
            if not self._device.online:
                self._canvas.message('摄像头已离线')
            return
        self._read_pending = True
        def success(state):
            if not shiboken6.isValid(self):
                return
            self._read_pending = False
            if self._power_busy or (time.monotonic() < self._power_deadline and state != self._power_state):
                return
            if state is None:
                if not self._attempted:
                    self._record_diagnostic('power', 'POWER_UNKNOWN', power_readable=False)
                self._continue_without_power_state()
                return
            self._apply_power(bool(state))
        def failure(_):
            if shiboken6.isValid(self):
                self._read_pending = False
                if not self._attempted:
                    self._record_diagnostic('power', 'POWER_READ_FAILED', power_readable=False)
                self._continue_without_power_state()
        self._jobs.submit(lambda: self._service.power_state(self._device.did), on_success=success, on_error=failure)

    def _continue_without_power_state(self):
        # A camera may expose video without a readable/writable MIoT power
        # property. Unknown power must not prevent independent video auth.
        if self._power_state is None:
            self._power.hide()
            if self.isVisible() and not self._attempted and self._session is None:
                self.start()

    def _apply_power(self, state):
        self._diagnostic['power_readable'] = True
        self._power.show()
        previous = self._power_state
        self._power_state = state
        self._power.set_state(state)
        self._power.setToolTip('让摄像头休眠' if state else '唤醒摄像头')
        if not state:
            self.stop()
            self._attempted = False
            self._canvas.message('摄像机已休眠', sleeping=True)
        else:
            if previous is not True:
                self._attempted = False
            if self.isVisible() and not self._attempted and not self._session:
                self.start()

    def _toggle_power(self):
        if self._power_busy:
            return
        if self._power_state is None:
            self._refresh_power()
            return
        target = not self._power_state
        self._power_busy = True
        self._power.set_busy(True)
        def success(_):
            if not shiboken6.isValid(self):
                return
            self._power_busy = False
            self._power.set_busy(False)
            self._power_deadline = time.monotonic() + 5
            self._apply_power(target)
        def failure(_):
            if shiboken6.isValid(self):
                self._power_busy = False
                self._power.set_busy(False)
                self._canvas.message('摄像头开关操作失败，请重试', clear=False)
        self._jobs.submit(lambda: self._service.set_power_state(self._device.did, target), on_success=success, on_error=failure)

    def _retry(self):
        if self._power_state is not False and self._session is None:
            self.start()

    def start(self, hint=None):
        if not self._device.online or self._power_state is False:
            if not self._device.online:
                self._record_diagnostic('idle', 'OFFLINE')
            self._canvas.message('摄像头已离线' if not self._device.online else '摄像机已休眠', sleeping=self._power_state is False)
            return
        self.stop()
        self._diagnostic_since = time.monotonic()
        power = self._diagnostic.get('power_readable')
        self._diagnostic = {'attempted': True, 'video_received': False, 'error_code': 'NONE', 'timeline': []}
        if type(power) is bool:
            self._diagnostic['power_readable'] = power
        self._record_diagnostic('authorization')
        self._attempted = True
        session = CameraSession()
        self._session = session
        generation = self._generation
        self._canvas.message(hint or '正在申请视频连接…')
        self._timeout.start(100000)
        def prepare():
            if session.cancelled:
                raise CameraPreviewError('预览已取消。')
            try:
                return self._service.prepare_camera_source(self._device)
            except CameraPreviewError:
                raise
            except Exception:
                raise CameraPreviewError('摄像头视频授权失败，请重试。') from None
        def success(source):
            if not shiboken6.isValid(self) or generation != self._generation:
                session.stop()
                return
            def connect_relay():
                try:
                    url = session.start(source, quality=self._quality,
                        channel=self._lens_channel,
                        on_progress=lambda value: self._progress.emit(generation, value) if shiboken6.isValid(self) else None,
                        on_audio=lambda rate, data: self._pcm.emit(generation, rate, data) if shiboken6.isValid(self) else None)
                    if session.cancelled or not shiboken6.isValid(self):
                        session.stop()
                    else:
                        self._connected.emit(generation, url)
                except Exception as error:
                    session.stop()
                    if shiboken6.isValid(self):
                        safe = error if isinstance(error, CameraPreviewError) else CameraPreviewError('远程视频连接失败，请重试。')
                        self._failed.emit(generation, safe)
            threading.Thread(target=connect_relay, daemon=True, name='camera-relay-connect').start()
        def failure(error):
            session.stop()
            if shiboken6.isValid(self):
                self._on_failed(generation, error)
        self._jobs.submit(prepare, on_success=success, on_error=failure)

    def _on_connected(self, generation, url):
        if generation != self._generation or self._session is None:
            return
        self._record_diagnostic('decode')
        self._canvas.message('正在载入画面…')
        self._url = url
        self._timeout.start(25000)
        self._player.setSource(QUrl(url))
        self._player.play()
        if not self._muted:
            self._session.command('AUDIO', 1)

    def _on_failed(self, generation, error):
        if generation == self._generation:
            self._record_diagnostic(self._diagnostic.get('stage', 'idle'), getattr(error, 'code', 'UNKNOWN'))
            self.stop()
            safe = str(error) if isinstance(error, CameraPreviewError) else '视频连接失败，请点击画面重试'
            self._canvas.message(safe)

    def _on_progress(self, generation, message):
        if generation != self._generation:
            return
        if message.startswith('ERROR '):
            self._on_failed(generation, CameraPreviewError(ERRORS.get(message[6:], '远程视频连接中断，请重试。'), code=message[6:]))
        elif message in ('INFO VIDEO_CODEC H264', 'INFO VIDEO_CODEC H265'):
            self._record_diagnostic(self._diagnostic.get('stage', 'media'), video_codec=message.rsplit(' ', 1)[1])
        elif message == 'INFO GOP_BOOTSTRAP':
            self._record_diagnostic(self._diagnostic.get('stage', 'media'), gop_bootstrap=True)
        elif message == 'NOTICE AUDIO_UNSUPPORTED':
            self._record_diagnostic(self._diagnostic.get('stage', 'media'), 'AUDIO_UNSUPPORTED')
            self._muted = True
            self._stop_audio()
            self._canvas.set_muted(True)
            self._canvas.mute.setToolTip('此设备的音频编码暂不支持')
        elif message == 'NOTICE CONTROL_FAILED':
            self._record_diagnostic(self._diagnostic.get('stage', 'playing'), 'CONTROL_FAILED')
            self._canvas.message('摄像头未能执行控制指令，请重试', clear=False)
        elif message == 'NOTICE QUALITY_REJECTED':
            # 摄像头明确拒绝了本次画质请求：不要假装切换成功
            self._record_diagnostic(self._diagnostic.get('stage', 'playing'), 'CONTROL_FAILED')
            self._canvas.message('摄像头不接受该画质，已保持原画面', clear=False)
        elif message in ('NOTICE AUDIO_ON', 'NOTICE AUDIO_OFF'):
            # 中继已重启视频流并切换音频开关，等 PCM 到了再给结论
            pass
        else:
            stage = {'PHASE RELAY': 'relay', 'PHASE AUTH': 'session_auth', 'PHASE MEDIA': 'media'}.get(message)
            if stage:
                self._record_diagnostic(stage)
            text = {'PHASE RELAY': '正在连接摄像头公网中继…', 'PHASE AUTH': '中继已连接，正在验证视频会话…',
                    'PHASE MEDIA': '视频授权已完成，正在等待摄像头画面…'}.get(message)
            if text:
                self._canvas.message(text)

    def _on_frame(self, frame):
        if self._session is None or self._switching_quality or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        if (not self._diagnostic.get('video_received') or self._diagnostic.get('width') != frame.width()
                or self._diagnostic.get('height') != frame.height()):
            self._record_diagnostic('playing', video_received=True, width=frame.width(), height=frame.height())
        self._playing = True
        self._timeout.stop()
        self._canvas.set_frame(image)
        self._video_layout.set_ratio(image.width(), image.height())
        self._canvas.set_controls_enabled(True)
        self._canvas.drag_enabled = self._device.model == 'chuangmi.camera.029a02'
        # No motor-completion acknowledgement is available. Require both a
        # settling interval and a subsequent decoded frame before another drag.
        if self._canvas.drag_busy and time.monotonic() >= self._drag_ready_at:
            self._canvas.set_drag_busy(False)
        height = frame.height()
        label = ('自动' if self._quality == 0 else
                 ('2K' if height > 1080 else f'{height}P') if self._quality == 2 else
                 '全分辨率' if self._quality == 3 else f'{height}P')
        self._canvas.quality.setText(label)
        self._canvas.quality.setToolTip(f'切换画质 · 当前 {frame.width()} × {height}')

    def _on_player_error(self, error, _message):
        if self._session is not None and not self._switching_quality and error != QMediaPlayer.Error.NoError:
            self._diagnostic['player_error'] = error.value
            code, message = {
                QMediaPlayer.Error.ResourceError: ('PLAYER_RESOURCE', '播放器未能读取本地视频流，请点击画面重试。'),
                QMediaPlayer.Error.FormatError: ('PLAYER_FORMAT', '播放器无法识别当前视频格式，请点击画面重试。'),
                QMediaPlayer.Error.NetworkError: ('PLAYER_NETWORK', '播放器连接本地视频流失败，请点击画面重试。'),
                QMediaPlayer.Error.AccessDeniedError: ('PLAYER_ACCESS', '播放器访问视频流被拒绝，请点击画面重试。'),
            }.get(error, ('UNKNOWN', '视频播放失败，请点击画面重试。'))
            # Never copy Qt's arbitrary message: it may include a media URL/path.
            self._on_failed(self._generation, CameraPreviewError(message, code=code))

    def _on_timeout(self):
        self._record_diagnostic(self._diagnostic.get('stage', 'idle'), 'TIMEOUT')
        self.stop()
        self._canvas.message('远程预览超时，请点击画面重试')

    def _show_quality(self):
        menu = self._build_quality_menu()
        self._quality_menu = menu
        menu.aboutToHide.connect(menu.deleteLater)
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._canvas.quality.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))

    def _build_quality_menu(self):
        menu = QMenu(self._canvas)
        style_popup_menu(menu)
        known = self._device.model == 'chuangmi.camera.029a02'
        for value, label in [(0, '自动'), (1, '360P 流畅' if known else '流畅'),
                             (2, '2K 高清' if known else '高清'), (3, '全分辨率')]:
            action = menu.addAction(label)
            action.setFont(menu.font())
            action.setData(value)
            if value == self._quality:
                action.setIcon(qta.icon('mdi.check', color=SiColors.THEME_DIM))
            action.triggered.connect(lambda checked=False, q=value: self._select_quality(q))
        return menu

    def _select_quality(self, quality):
        if quality == self._quality:
            return
        self._quality = quality
        self._canvas.quality.setText({0: '自动', 1: '流畅', 2: '高清', 3: '全分辨率'}[quality])
        # 画质是「视频会话级」参数：摄像头只在新建会话时才重新协商分辨率。
        # 旧实现只在原会话里发 QUALITY 再重开本地播放器，服务端仍推原子码流，
        # 于是永远停在 360P。这里改成整段重连（重新授权 → 新中继 → 新会话）。
        if self._session is None and not self._attempted:
            return
        self.start(hint='正在切换画质…')

    def _toggle_mute(self):
        if not self._session or not self._playing:
            return
        target = not self._muted  # True = 本次点击要静音
        if not self._session.command('AUDIO', 0 if target else 1):
            return
        self._muted = target
        self._canvas.set_muted(target)
        if target:
            self._stop_audio()
            self._canvas.message('已静音', clear=False)
            return
        # 开启声音：中继会重启视频流并把 enableaudio 打开，
        # 3.5 秒内没有 PCM 说明这台摄像头根本没推音频（多为 Opus）
        self._audio_tried = time.monotonic()
        self._canvas.message('正在开启声音…', clear=False)
        QTimer.singleShot(3500, self, self._check_audio_arrived)

    def _check_audio_arrived(self):
        if not shiboken6.isValid(self) or self._muted:
            return
        if self._audio_seen > self._audio_tried:
            return
        self._canvas.message('该摄像头未输出声音（麦克风可能已关闭，或该机型不带音频）', clear=False)
        self._muted = True
        self._canvas.set_muted(True)

    def _on_pcm(self, generation, rate, data):
        if generation != self._generation or self._muted or not self._playing:
            return
        if rate == 0:
            # Opus 原始包（中继只透传，codecId 1032）：本机用 libopus 解码。
            if self._opus is None:
                self._opus = OpusStreamDecoder()
            data = self._opus.decode(data)
            if not data:
                if self._opus.error:
                    self._canvas.message('音频解码组件不可用：' + self._opus.error, clear=False)
                    self._muted = True
                    self._canvas.set_muted(True)
                    self._stop_audio()
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
                self._muted = True
                self._canvas.set_muted(True)
                self._stop_audio()
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

    def _drag(self, direction):
        if (self._session and self._playing and self._canvas.drag_enabled
                and not self._canvas.drag_busy and not self._switching_quality):
            if self._session.command('PTZ', direction):
                self._drag_ready_at = time.monotonic() + 1.5
                self._canvas.set_drag_busy(True)

    def _toggle_fullscreen(self):
        if self._fullscreen is not None:
            self._fullscreen.reject()
            return
        window = QDialog(self.window(), Qt.Window | Qt.FramelessWindowHint)
        window.setWindowModality(Qt.WindowModal)
        window.setStyleSheet('background: black;')
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        self._video_layout.removeWidget(self._canvas)
        layout.addWidget(self._canvas)
        self._fullscreen = window
        self._canvas.set_fullscreen(True)
        def restore(_):
            if not shiboken6.isValid(self):
                return
            layout.removeWidget(self._canvas)
            self._canvas.setParent(self)
            self._video_layout.addWidget(self._canvas)
            self._canvas.set_fullscreen(False)
            self._canvas.show()
            self._fullscreen = None
            window.deleteLater()
        window.finished.connect(restore)
        window.showFullScreen()

    def stop(self):
        self._generation += 1
        self._timeout.stop()
        self._playing = False
        self._switching_quality = False
        self._url = ''
        session, self._session = self._session, None
        self._player.stop()
        self._player.setSource(QUrl())
        self._stop_audio()
        if session is not None:
            session.stop()
        self._canvas.drag_enabled = False
        self._drag_ready_at = 0
        self._canvas.set_drag_busy(False)
        self._canvas.message('')
        self._canvas.set_controls_enabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        if self._loaded:
            self._poll.start()
            self._refresh_power()

    def hideEvent(self, event):
        if self.window().isMinimized():
            # Minimizing is still an active viewing session. Keep the relay,
            # decoder and any pending authorization alive for instant restore.
            super().hideEvent(event)
            return
        self._poll.stop()
        self._attempted = False
        if self._fullscreen is not None:
            self._fullscreen.reject()
        self.stop()
        super().hideEvent(event)
