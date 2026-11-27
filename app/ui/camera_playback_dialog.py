# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""摄像头 SD 卡录像回放：按日期/片段选择，播放与返回直播。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from app.core import camera_playback as pb
from app.ui.si_theme import SiColors


class CameraPlaybackDialog(QDialog):
    """片段列表来自摄像头 RDT 索引，画面仍由主窗口的播放器渲染。"""

    play_requested = Signal(object)      # (storage_channel, Clip)
    refresh_requested = Signal()
    live_requested = Signal()
    direct_requested = Signal(object)    # (storage_channel, Clip)，换封装重试

    def __init__(self, parent=None, *, dual=False):
        super().__init__(parent)
        self.setWindowTitle('SD 卡录像回放')
        self.setMinimumWidth(380)
        self.setMinimumHeight(420)
        self._dual = dual
        self._clips: list[tuple[int, pb.Clip]] = []
        self._groups: list[tuple[str, list[tuple[int, pb.Clip]]]] = []
        self._direct_shown = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(QLabel('日期'))
        self._days = QComboBox()
        self._days.currentIndexChanged.connect(self._fill_list)
        top.addWidget(self._days, 1)
        self._refresh_btn = QPushButton('刷新')
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self._refresh_btn)
        layout.addLayout(top)

        self._status = QLabel('正在读取摄像头 SD 卡的录像索引…')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _: self._play())
        layout.addWidget(self._list, 1)

        bottom = QHBoxLayout()
        self._live_btn = QPushButton('返回直播')
        self._live_btn.clicked.connect(self.live_requested.emit)
        bottom.addWidget(self._live_btn)
        bottom.addStretch(1)
        self._direct_btn = QPushButton('换封装重试')
        self._direct_btn.setToolTip('若摄像头没响应回放请求，改用外层命令直发（部分 CS2 固件需要）')
        self._direct_btn.setVisible(False)
        self._direct_btn.clicked.connect(self._retry_direct)
        bottom.addWidget(self._direct_btn)
        self._play_btn = QPushButton('播放所选片段')
        self._play_btn.clicked.connect(self._play)
        bottom.addWidget(self._play_btn)
        layout.addLayout(bottom)
        self.retheme()

    def retheme(self):
        self.setStyleSheet(
            f'QDialog {{ background: {SiColors.SURFACE}; }}'
            f' QLabel {{ color: {SiColors.TEXT_PRIMARY}; }}'
            f' QListWidget {{ background: {SiColors.CARD}; color: {SiColors.TEXT_PRIMARY};'
            f' border: 1px solid {SiColors.LINE}; border-radius: 8px; }}'
            f' QListWidget::item:selected {{ background: {SiColors.THEME};'
            f' color: {SiColors.ON_THEME_TEXT}; }}'
            f' QComboBox {{ background: {SiColors.CARD}; color: {SiColors.TEXT_PRIMARY};'
            f' border: 1px solid {SiColors.LINE}; border-radius: 6px; padding: 4px 8px; }}')

    # ---------- 数据 ----------

    def set_clips(self, clips: list[tuple[int, pb.Clip]]) -> None:
        self._clips = list(clips)
        buckets: dict[str, list[tuple[int, pb.Clip]]] = {}
        for channel, clip in self._clips:
            day = pb.group_by_day([clip])[0][0]
            buckets.setdefault(day, []).append((channel, clip))
        self._groups = sorted(
            ((day, sorted(items, key=lambda pair: pair[1].start))
             for day, items in buckets.items()),
            key=lambda pair: pair[0], reverse=True)
        current = self._days.currentText()
        self._days.blockSignals(True)
        self._days.clear()
        self._days.addItems([day for day, _ in self._groups])
        if current in [day for day, _ in self._groups]:
            self._days.setCurrentText(current)
        self._days.blockSignals(False)
        self._fill_list()
        total = len(self._clips)
        if total:
            self._status.setText(f'共 {total} 段录像，选一段双击或点「播放所选片段」。'
                                 '回放期间实时画面会暂停。')
        else:
            self._status.setText('没有读到录像片段：请确认摄像头已插 SD 卡、开启了录像，'
                                 '且该机型支持通过公网中继读取索引。')

    def _fill_list(self):
        self._list.clear()
        index = self._days.currentIndex()
        if index < 0 or index >= len(self._groups):
            return
        for channel, clip in self._groups[index][1]:
            lens = ' · 第二镜头' if self._dual and channel else ''
            mark = ' · 有侦测事件' if clip.event else ''
            text = f'{pb.format_span(clip)}  ({clip.duration} 秒){lens}{mark}'
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, (channel, clip))
            self._list.addItem(item)

    def current_clip(self):
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    # ---------- 状态 ----------

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_loading(self, loading: bool) -> None:
        self._refresh_btn.setEnabled(not loading)
        if loading:
            self.set_status('正在读取摄像头 SD 卡的录像索引…（首次可能需要几秒）')

    def show_direct_retry(self) -> None:
        self._direct_btn.setVisible(True)
        self._direct_shown = True

    def set_playback_active(self, active: bool) -> None:
        self._play_btn.setText('播放所选片段' if not active else '播放另一段')
        self._live_btn.setEnabled(True)

    # ---------- 操作 ----------

    def _play(self):
        clip = self.current_clip()
        if clip is None:
            self.set_status('请先选中一段录像。')
            return
        self.set_status('正在请求回放…')
        self.play_requested.emit(clip)

    def _retry_direct(self):
        clip = self.current_clip()
        if clip is None:
            self.set_status('请先选中一段录像。')
            return
        self.set_status('正在用外层命令直发重试…')
        self.direct_requested.emit(clip)
