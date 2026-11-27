# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated camera page: physical power, remote preview, audio and fullscreen."""
import threading
import time
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink, QAudioSink, QAudioFormat
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget
from app.core import camera_playback as pb, camera_ptz
from app.ui.camera_playback_dialog import CameraPlaybackDialog
from app.core.camera_preview import CameraPreviewError, CameraSession, ERRORS, lens_power_on
from app.core.camera_talk import TalkSession, speaker_codec
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
    _playback = Signal(int, str, object)

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
        self._quality = 3  # 单摄像头窗口默认全分辨率；只要清晰度不够可切 480P 省带宽
        self._muted = True
        self._audio_sink = None
        self._audio_io = None
        self._audio_rate = 0
        self._opus = None
        self._fullscreen = None
        self._drag_ready_at = 0
        self._ptz_ready_at = 0
        self._ptz_cap = camera_ptz.capability(getattr(device, 'model', ''), 0)
        self._talk = None
        self._url = ''
        self._switching_quality = False
        # ---- SD 卡回放 ----
        self._playback_dlg = None
        self._playback_clips: list[tuple[int, pb.Clip]] = []
        self._playback_channels: list[int] = []
        self._playback_index_channel = 0
        self._playback_active = False
        self._playback_index_timer = QTimer(self)
        self._playback_index_timer.setSingleShot(True)
        self._playback_index_timer.timeout.connect(self._playback_next_channel)
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
        self._playback_btn = QPushButton('回放')
        self._playback_btn.setCursor(Qt.PointingHandCursor)
        self._playback_btn.setToolTip('回看摄像头 SD 卡里的历史录像')
        self._playback_btn.clicked.connect(self._open_playback)
        self.header.addWidget(self._playback_btn)
        self._dual_lens = getattr(device, 'model', '') == 'isa.camera.hlmax'
        self._lens_channel = 0   # 0=云台镜头，1=枪机镜头（第二路流）
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
        self._drag_hint = QLabel(
            '按住右侧方向盘调整摄像头角度，也可直接拖拽画面', self)
        self._drag_hint.setAlignment(Qt.AlignCenter)
        self._drag_hint.setVisible(bool(self._ptz_cap))
        self._hint_slot = QWidget(self)
        self._hint_slot.setVisible(bool(self._ptz_cap))
        self._layout.addWidget(self._hint_slot)
        self._canvas.installEventFilter(self)
        self._canvas.retry.connect(self._retry)
        self._canvas.mute_clicked.connect(self._toggle_mute)
        self._canvas.quality_clicked.connect(self._show_quality)
        self._canvas.fullscreen_clicked.connect(self._toggle_fullscreen)
        self._canvas.dragged.connect(self._drag)
        self._canvas.ptz_step.connect(self._ptz_step)
        self._canvas.ptz_released.connect(self._ptz_release)
        self._canvas.talk_clicked.connect(self._toggle_talk)
        self._canvas.set_ptz_capability(self._ptz_cap)
        self._canvas.set_talk_supported(
            speaker_codec(getattr(device, 'model', '')) is not None)
        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._sink)
        self._player.errorOccurred.connect(self._on_player_error)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._connected.connect(self._on_connected)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)
        self._pcm.connect(self._on_pcm)
        self._playback.connect(self._on_playback)
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
    #
    # 实测结论（2026-11-26，固件 5.3.2_0336）：
    #   · 第二路镜头靠 MISS 视频请求体的 {"videoquality":-1,"videoquality2":N}
    #     选中，普通的 "channel" 字段设备完全不认；
    #   · 但目标镜头的开关（siid 6 piid 25 dome-switch / piid 26 box-switch）
    #     必须是开的，关着那一路请求回来只会是「已连上但没画面」；
    #   · hl-camera-switch（piid 21）只是上面两个开关的汇总读数，写它并不选流。
    _LENS_SIID = 6
    _LENS_PIID = 21                          # hl-camera-switch：0全关 1/2 单镜头 3双开
    _LENS_SWITCH = {0: 25, 1: 26}            # 通道 → 镜头电源开关
    _LENS_NAME = {0: '云台', 1: '枪机'}

    def _show_lens_menu(self):
        menu = QMenu(self)
        style_popup_menu(menu)
        for channel, label in [(0, '云台镜头（可转向）'), (1, '枪机镜头（固定广角）')]:
            action = menu.addAction(label)
            action.setFont(menu.font())
            if channel == self._lens_channel:
                try:
                    action.setIcon(qta.icon('mdi.check', color=SiColors.THEME_DIM))
                except Exception:
                    pass
            action.triggered.connect(
                lambda checked=False, c=channel: self._switch_lens(c))
        menu.addSeparator()
        dual = menu.addAction('双画面（云台 + 枪机并排）')
        dual.setFont(menu.font())
        dual.setToolTip('同一台设备开两路视频，两路画面同时显示')
        dual.triggered.connect(lambda checked=False: self._open_dual())
        menu.ensurePolished()
        menu.adjustSize()
        menu.popup(self._lens_btn.mapToGlobal(QPoint(0, self._lens_btn.height() + 4)))
        menu.aboutToHide.connect(menu.deleteLater)

    def _open_dual(self):
        """开双画面窗口：本窗口先断开，把有限的视频连接名额让给两路。"""
        from app.ui.camera_dual_view import DualCameraDialog
        dialog = DualCameraDialog(self._service, self._jobs, self._device,
                                  self._quality, self.window())
        was_active = self._session is not None or self._playing
        self.stop()
        if was_active:
            dialog.finished.connect(lambda _=0: self.start() if shiboken6.isValid(self) else None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.start()

    def _switch_lens(self, channel):
        if channel == self._lens_channel:
            return
        self._lens_btn.setEnabled(False)
        name = self._LENS_NAME.get(channel, '')
        self._canvas.message(f'正在切换到{name}镜头…', clear=False)
        previous = self._lens_channel
        self._lens_channel = channel
        piid = self._LENS_SWITCH.get(channel)

        def work():
            # 目标镜头没上电就先打开它，否则第二路请求会空转
            state = self._service.read_prop_raw(self._device.did, self._LENS_SIID, piid) \
                if piid is not None else True
            if not lens_power_on(state):
                self._service.set_prop_raw(self._device.did, self._LENS_SIID, piid, True)
                time.sleep(1.5)
            return True

        def success(_):
            if not shiboken6.isValid(self):
                return
            self._lens_btn.setEnabled(True)
            self.start(hint=f'正在切换到{name}镜头…')

        def failure(error):
            if not shiboken6.isValid(self):
                return
            self._lens_channel = previous
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
        if hasattr(self, '_playback_btn'):
            self._playback_btn.setStyleSheet(self._diagnostic_button.styleSheet())
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
            if not lens_power_on(previous):
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
                        on_audio=lambda rate, data: self._pcm.emit(generation, rate, data) if shiboken6.isValid(self) else None,
                        on_playback=lambda kind, value: self._playback.emit(generation, kind, value) if shiboken6.isValid(self) else None)
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
        elif message == 'NOTICE TALK_FAILED':
            self._record_diagnostic(self._diagnostic.get('stage', 'playing'), 'TALK_FAILED')
            self._on_talk_error('摄像头未接受对讲请求（该机型或固件可能不支持）')
        elif message == 'NOTICE SPEAKER_UNSUPPORTED':
            self._on_talk_error('该摄像头的对讲编码暂不支持')
        elif message == 'NOTICE MOTOR_FAILED':
            self._canvas.message('云台指令未被接受（该机型可能不支持电机）', clear=False)
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

    # ---- SD 卡录像回放 ----

    def _open_playback(self):
        if self._session is None or not self._playing:
            self._canvas.message('请先连上实时画面，再打开回放', clear=False)
            return
        if self._playback_dlg is not None:
            self._playback_dlg.raise_()
            self._playback_dlg.activateWindow()
            self._request_index()
            return
        dlg = CameraPlaybackDialog(self, dual=self._dual_lens)
        self._playback_dlg = dlg
        dlg.play_requested.connect(self._play_clip)
        dlg.direct_requested.connect(self._play_clip_direct)
        dlg.refresh_requested.connect(self._request_index)
        dlg.live_requested.connect(self._back_to_live)
        dlg.finished.connect(self._forget_playback_dialog)
        dlg.show()
        self._request_index()

    def _forget_playback_dialog(self):
        self._playback_dlg = None
        self._playback_index_timer.stop()

    def _request_index(self):
        if self._session is None:
            return
        self._playback_clips = []
        self._playback_channels = list(pb.storage_channels(
            getattr(self._device, 'model', ''), self._lens_channel))
        if self._playback_dlg is not None:
            self._playback_dlg.set_loading(True)
        self._playback_next_channel()

    def _playback_next_channel(self):
        self._playback_index_timer.stop()
        if self._session is None:
            return
        if not self._playback_channels:
            if self._playback_dlg is not None:
                self._playback_dlg.set_clips(self._playback_clips)
            return
        channel = self._playback_channels.pop(0)
        self._playback_index_channel = channel
        self._session.request_index(channel)
        # 卡上没有该存储通道时摄像头会直接沉默，别让用户一直等
        self._playback_index_timer.start(5000)

    def _on_playback(self, generation, kind, value):
        if generation != self._generation:
            return
        dlg = self._playback_dlg
        if kind == 'index':
            self._playback_clips.extend(
                (self._playback_index_channel, clip) for clip in value)
            self._playback_next_channel()
            return
        if dlg is None:
            return
        if kind == 'status':
            if pb.status_found(value):
                self._playback_active = True
                duration = value.get('duration') if isinstance(value, dict) else None
                suffix = f'，时长 {duration} 秒' if isinstance(duration, int) else ''
                dlg.set_status(f'正在回放录像{suffix}。点「返回直播」恢复实时画面。')
                dlg.set_playback_active(True)
            else:
                dlg.set_status('摄像头没找到这段录像（片段边界可能已变化）。'
                               '可刷新索引，或点「换封装重试」。')
                dlg.show_direct_retry()
        elif kind == 'notice':
            if value in ('INDEX_FAILED', 'PLAYBACK_FAILED'):
                dlg.set_status('摄像头没有接受回放指令，可能是该机型或固件不支持。'
                               '可点「换封装重试」再试一次。')
                dlg.show_direct_retry()
            elif value == 'LIVE':
                self._playback_active = False
                dlg.set_status('已请求返回直播，画面恢复需要几秒。')
                dlg.set_playback_active(False)

    def _play_clip(self, pair):
        self._send_playback(pair, direct=False)

    def _play_clip_direct(self, pair):
        self._send_playback(pair, direct=True)

    def _send_playback(self, pair, *, direct):
        if self._session is None or pair is None:
            return
        channel, clip = pair
        start, end = pb.playback_window(clip)
        # 存储通道不是直播通道号；单摄留空让摄像头自己选主镜头
        channels = [channel] if (self._dual_lens or channel) else []
        method = self._session.play_direct if direct else self._session.play
        if not method(start, end, channels):
            if self._playback_dlg is not None:
                self._playback_dlg.set_status('回放指令发送失败，请重试。')

    def _back_to_live(self):
        if self._session is not None:
            self._session.back_to_live()
        self._playback_active = False
        if self._playback_dlg is not None:
            self._playback_dlg.set_status('已请求返回直播，画面恢复需要几秒。')
            self._playback_dlg.set_playback_active(False)

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
        self._canvas.drag_enabled = bool(self._ptz_cap)
        # No motor-completion acknowledgement is available. Require both a
        # settling interval and a subsequent decoded frame before another drag.
        if self._canvas.drag_busy and time.monotonic() >= self._drag_ready_at:
            self._canvas.set_drag_busy(False)
        height = frame.height()
        label = '全分辨率' if self._quality == 3 else f'{height}P'
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
        # 只保留两档：480P 子码流（省带宽）与全分辨率（原始清晰度）
        for value, label in [(1, '480P'), (3, '全分辨率')]:
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
        self._canvas.quality.setText(
            {0: '自动', 1: '480P', 2: '720P', 3: '全分辨率'}.get(quality, f'{quality}P'))
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
            ok = (self._session.motor(camera_ptz.DIRECTION_NAMES[direction], 5)
                  if self._ptz_cap == camera_ptz.CONTINUOUS
                  else self._session.command('PTZ', direction))
            if ok:
                self._drag_ready_at = time.monotonic() + 1.5
                self._canvas.set_drag_busy(True)

    def _ptz_step(self, direction):
        """方向盘：按住连发（间隔由 PtzPad 控制），这里只做会话级节流。"""
        if (not self._session or not self._playing or not self._ptz_cap
                or self._switching_quality or time.monotonic() < self._ptz_ready_at):
            return
        if self._ptz_cap == camera_ptz.CONTINUOUS:
            ok = self._session.motor(camera_ptz.DIRECTION_NAMES.get(direction, 'stop'), 5)
        else:
            ok = self._session.command('PTZ', direction)
        if ok:
            self._ptz_ready_at = (time.monotonic() +
                                  camera_ptz.step_interval(self._ptz_cap) / 1000.0 * 0.75)

    def _ptz_release(self):
        self._ptz_ready_at = 0
        if self._ptz_cap == camera_ptz.CONTINUOUS and self._session:
            self._session.motor('stop', 0)

    # ---- 对讲（电脑麦克风 → 摄像头扬声器）----
    def _toggle_talk(self):
        if self._talk is not None:
            self._stop_talk()
            return
        if not self._session or not self._playing:
            self._canvas.message('画面加载完成后才能通话', clear=False)
            return
        talk = TalkSession(self._session, getattr(self._device, 'model', ''), self)
        talk.error.connect(self._on_talk_error)
        if talk.start():
            self._talk = talk
            self._canvas.set_talking(True)
            self._canvas.message('正在通话 · 再点麦克风结束', clear=False)
        else:
            talk.deleteLater()

    def _stop_talk(self):
        talk, self._talk = self._talk, None
        if talk is not None:
            talk.stop()
            talk.deleteLater()
        self._canvas.set_talking(False)

    def _on_talk_error(self, text):
        self._stop_talk()
        self._canvas.message(text, clear=False)

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
        self._playback_index_timer.stop()
        if self._playback_dlg is not None:
            dlg, self._playback_dlg = self._playback_dlg, None
            dlg.close()
        self._playback_active = False
        self._playback_clips = []
        self._playback_channels = []
        self._playing = False
        self._switching_quality = False
        self._url = ''
        self._stop_talk()
        session, self._session = self._session, None
        self._player.stop()
        self._player.setSource(QUrl())
        self._stop_audio()
        if session is not None:
            session.stop()
        self._canvas.drag_enabled = False
        self._drag_ready_at = self._ptz_ready_at = 0
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
