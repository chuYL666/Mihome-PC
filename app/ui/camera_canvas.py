# SPDX-License-Identifier: GPL-3.0-or-later
"""Aspect-preserving camera canvas with controls painted above decoded video."""
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLayout,
                               QPushButton, QWidget)
import qtawesome as qta
import time

from app.core import camera_ptz


class PtzPad(QWidget):
    """半透明方向盘：按住方向键连发，松开通知调用方停止（连续机型用）。

    之前云台只能靠「在画面上拖一下」触发一步，既没有明确入口，也无法
    连续调整。这里给单摄像头窗口和摄像头墙格子共用一个紧凑控件。
    """
    step = Signal(int)
    released = Signal()

    _ARROWS = [
        (camera_ptz.UP, 'mdi.chevron-up', 0, 1),
        (camera_ptz.LEFT, 'mdi.chevron-left', 1, 0),
        (camera_ptz.RIGHT, 'mdi.chevron-right', 1, 2),
        (camera_ptz.DOWN, 'mdi.chevron-down', 2, 1),
    ]

    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self._capability = camera_ptz.NONE
        self._direction = 0
        self._enabled = False
        self._side = 26 if compact else 32
        self.setObjectName('ptzPad')
        self.setFixedSize(self._side * 3 + 12, self._side * 3 + 12)
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        grid = QGridLayout(self)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(2)
        self._buttons = {}
        for direction, icon, row, col in self._ARROWS:
            button = QPushButton(self)
            button.setIcon(qta.icon(icon, color='#f2f5fa'))
            button.setIconSize(QSize(self._side - 10, self._side - 10))
            button.setFixedSize(self._side, self._side)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip({'1': '向左转', '2': '向右转',
                               '3': '向上转', '4': '向下转'}[str(direction)])
            button.pressed.connect(lambda d=direction: self._begin(d))
            button.released.connect(self._end)
            grid.addWidget(button, row, col)
            self._buttons[direction] = button
        home = QPushButton(self)
        home.setIcon(qta.icon('mdi.circle-small', color='#cfd6e2'))
        home.setIconSize(QSize(self._side - 8, self._side - 8))
        home.setFixedSize(self._side, self._side)
        home.setToolTip('云台方向控制')
        home.setFlat(True)
        home.setAttribute(Qt.WA_TransparentForMouseEvents)
        grid.addWidget(home, 1, 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._repeat)
        self._style()
        self.hide()

    def _style(self):
        r = 12
        self.setStyleSheet(
            f'QWidget#ptzPad {{ background: rgba(12, 16, 22, 115); '
            f'border: 1px solid rgba(255,255,255,45); border-radius: {r}px; }} '
            f'QPushButton {{ background: rgba(255,255,255,18); border: none; '
            f'border-radius: 6px; }} '
            f'QPushButton:hover {{ background: rgba(255,255,255,55); }} '
            f'QPushButton:pressed {{ background: rgba(93, 173, 125, 150); }} '
            f'QPushButton:disabled {{ background: rgba(255,255,255,8); }}')

    def set_capability(self, capability):
        self._capability = capability or camera_ptz.NONE
        self.set_enabled(bool(self._capability))

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        if not enabled:
            self._end()
            self.hide()
            return
        self.show()
        for button in self._buttons.values():
            button.setEnabled(True)

    def _begin(self, direction):
        if not self._enabled:
            return
        self._direction = direction
        self.step.emit(direction)
        self._timer.start(camera_ptz.step_interval(self._capability))

    def _repeat(self):
        if self._enabled and self._direction:
            self.step.emit(self._direction)

    def _end(self):
        self._timer.stop()
        if self._direction:
            self._direction = 0
            self.released.emit()


class AspectVideoLayout(QLayout):
    """Keep the canvas itself proportional, not only the image painted inside."""
    def __init__(self):
        super().__init__()
        self._item = None
        self._ratio = 16 / 9
        self.setContentsMargins(49, 12, 49, 0)

    def addItem(self, item):
        self._item = item
        self.invalidate()

    def count(self):
        return int(self._item is not None)

    def itemAt(self, index):
        return self._item if index == 0 else None

    def takeAt(self, index):
        if index != 0:
            return None
        item, self._item = self._item, None
        self.invalidate()
        return item

    def sizeHint(self):
        return QSize(738, 372)

    def minimumSize(self):
        return QSize(338, 147) if self._item is not None else QSize(0, 0)

    def set_ratio(self, width, height):
        if width > 0 and height > 0:
            ratio = width / height
            if abs(ratio - self._ratio) > .001:
                self._ratio = ratio
                self.invalidate()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        if self._item is None:
            return
        bounds = self.contentsRect()
        width = min(bounds.width(), round(bounds.height() * self._ratio))
        height = round(width / self._ratio)
        self._item.setGeometry(QRect(bounds.x() + (bounds.width()-width)//2,
                                     bounds.y(), max(0, width), max(0, height)))


class CameraCanvas(QWidget):
    retry = Signal()
    dragged = Signal(int)
    mute_clicked = Signal()
    quality_clicked = Signal()
    fullscreen_clicked = Signal()
    talk_clicked = Signal()
    ptz_step = Signal(int)
    ptz_released = Signal()

    def __init__(self, parent=None, *, compact_ptz=False):
        super().__init__(parent)
        self.setMinimumSize(240, 135)
        self._image = QImage()
        # 默认完整显示（KeepAspectRatio，不裁画面）；铺满模式由调用方显式开启。
        # 摄像头墙等总览场景一律用默认值——居中裁剪会把监控画面边缘切掉。
        self._fill = False
        self._press = None
        self._drag_anchor = None
        self._drag_enabled = False
        self.drag_busy = False
        self._gesture_sent = False
        self._last_drag_step = 0.0
        self._ptz_capability = camera_ptz.NONE
        self._controls_enabled = False
        self._talk_supported = True
        self._talking = False
        self._notice = QLabel(self)
        self._notice.setAlignment(Qt.AlignCenter)
        self._notice.setWordWrap(True)
        self._notice.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._notice.setStyleSheet('background: transparent; color: #cbd1da; font-size: 10pt; border: none;')
        self._symbol = QLabel(self)
        self._symbol.setAlignment(Qt.AlignCenter)
        self._symbol.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._symbol.setStyleSheet('background: transparent; border: none;')
        self._symbol.setPixmap(qta.icon('mdi.camera-off', color='#8d949f').pixmap(44, 44))
        self._controls = QWidget(self)
        self._controls.setStyleSheet('background: transparent; border: none;')
        row = QHBoxLayout(self._controls)
        row.setContentsMargins(14, 0, 14, 0)
        row.setSpacing(10)
        self.mute = self._button('mdi.volume-off', '开启声音')
        self.talk = self._button('mdi.microphone-outline', '与摄像头通话（点击开始说话）')
        self.quality = QPushButton('480P', self._controls)
        self.quality.setFixedSize(54, 36)
        self.quality.setCursor(Qt.PointingHandCursor)
        self.quality.setToolTip('切换画质')
        self.quality.setStyleSheet('QPushButton { background: transparent; color: white; border: 1px solid rgba(255,255,255,190); border-radius: 6px; font-size: 10pt; font-weight: 600; } QPushButton:hover { background: rgba(255,255,255,25); } QPushButton:disabled { color: #646b74; border-color: #646b74; }')
        self.fullscreen = self._button('mdi.fullscreen', '全屏观看')
        row.addWidget(self.mute)
        row.addWidget(self.talk)
        row.addStretch()
        row.addWidget(self.quality)
        row.addStretch()
        row.addWidget(self.fullscreen)
        self.mute.clicked.connect(self.mute_clicked)
        self.talk.clicked.connect(self.talk_clicked)
        self.quality.clicked.connect(self.quality_clicked)
        self.fullscreen.clicked.connect(self.fullscreen_clicked)
        self._ptz_pad = PtzPad(self, compact=compact_ptz)
        self._ptz_pad.step.connect(self.ptz_step)
        self._ptz_pad.released.connect(self.ptz_released)
        self.message('正在读取摄像头状态…')
        self.set_controls_enabled(False)

    def _button(self, icon, tooltip):
        button = QPushButton(self._controls)
        button.setIcon(qta.icon(icon, color='white', color_disabled='#646b74'))
        button.setIconSize(QSize(28, 28))
        button.setFixedSize(42, 40)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setStyleSheet('QPushButton { background: transparent; border: none; border-radius: 7px; } QPushButton:hover { background: rgba(255,255,255,25); }')
        return button

    def message(self, text, *, clear=True, sleeping=False):
        if clear:
            self._image = QImage()
        self._notice.setText(text)
        self._notice.setVisible(bool(text))
        self._symbol.setVisible(sleeping)
        self.update()

    def set_frame(self, image):
        self._image = image
        self._notice.hide()
        self._symbol.hide()
        self.update()

    def set_fill(self, fill):
        """True = 铺满格子（居中裁剪）；False = 完整显示（留黑边）。"""
        fill = bool(fill)
        if fill != self._fill:
            self._fill = fill
            self.update()

    def set_muted(self, muted):
        self.mute.setIcon(qta.icon('mdi.volume-off' if muted else 'mdi.volume-high', color='white', color_disabled='#646b74'))
        self.mute.setToolTip('开启声音' if muted else '静音')

    def set_fullscreen(self, fullscreen):
        self.fullscreen.setIcon(qta.icon('mdi.fullscreen-exit' if fullscreen else 'mdi.fullscreen', color='white', color_disabled='#646b74'))
        self.fullscreen.setToolTip('退出全屏（Esc）' if fullscreen else '全屏观看')

    def set_ptz_capability(self, capability):
        """按机型开启方向盘；'', 'step', 'continuous' 见 app.core.camera_ptz。"""
        self._ptz_capability = capability or camera_ptz.NONE
        self._ptz_pad.set_capability(self._ptz_capability)
        self._ptz_pad.set_enabled(self._controls_enabled and bool(self._ptz_capability))

    def ptz_capability(self):
        return self._ptz_capability

    def set_talk_supported(self, supported):
        self._talk_supported = bool(supported)
        self.talk.setEnabled(self._controls_enabled and self._talk_supported)
        if not self._talk_supported:
            self.talk.setToolTip('该摄像头的对讲功能暂不可用')

    def set_talking(self, talking):
        self._talking = bool(talking)
        self.talk.setIcon(qta.icon('mdi.microphone' if talking else 'mdi.microphone-outline',
                                   color='#7ce0a8' if talking else 'white'))
        self.talk.setToolTip('结束通话' if talking else '与摄像头通话（点击开始说话）')
        border = 'rgba(124,224,168,230)' if talking else 'rgba(255,255,255,190)'
        background = 'rgba(64,168,110,90)' if talking else 'transparent'
        self.talk.setStyleSheet(
            f'QPushButton {{ background: {background}; '
            f'border: 1px solid {border}; border-radius: 7px; }} '
            f'QPushButton:hover {{ background: rgba(255,255,255,25); }}')

    def set_controls_enabled(self, enabled):
        self._controls_enabled = bool(enabled)
        self.mute.setEnabled(enabled)
        self.quality.setEnabled(enabled)
        self.talk.setEnabled(bool(enabled) and self._talk_supported)
        self.fullscreen.setEnabled(True)
        self._ptz_pad.set_enabled(bool(enabled) and bool(self._ptz_capability))

    def resizeEvent(self, event):
        self._controls.setGeometry(14, max(0, self.height() - 62), max(0, self.width() - 28), 46)
        self._notice.setGeometry(24, max(0, self.height() // 2 - 15), max(0, self.width() - 48), 58)
        self._symbol.setGeometry(0, max(0, self.height() // 2 - 80), self.width(), 56)
        self._ptz_pad.move(max(0, self.width() - self._ptz_pad.width() - 12),
                           max(0, (self.height() - self._ptz_pad.height()) // 2 - 10))
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#080a0d'))
        if not self._image.isNull():
            if self._fill:
                # KeepAspectRatioByExpanding：小的那一边也能盖住，多出来的部分裁掉
                size = self._image.size().scaled(self.size(), Qt.KeepAspectRatioByExpanding)
                target = QRectF((self.width() - size.width()) / 2,
                                (self.height() - size.height()) / 2,
                                size.width(), size.height())
                painter.setClipRect(self.rect())
                painter.drawImage(target, self._image)
                painter.setClipping(False)
            else:
                size = self._image.size().scaled(self.size(), Qt.KeepAspectRatio)
                target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())
                painter.drawImage(target, self._image)
        gradient = QLinearGradient(0, self.height() - 100, 0, self.height())
        gradient.setColorAt(0, QColor(0, 0, 0, 0))
        gradient.setColorAt(1, QColor(0, 0, 0, 155))
        painter.fillRect(0, max(0, self.height() - 100), self.width(), 100, gradient)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.drag_busy:
                event.accept()
                return
            self._press = event.position().toPoint()
            self._drag_anchor = self._press
            self._gesture_sent = False
            self._last_drag_step = 0.0
            if self.drag_enabled:
                self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    @property
    def drag_enabled(self):
        return self._drag_enabled

    @drag_enabled.setter
    def drag_enabled(self, enabled):
        if self._drag_enabled == bool(enabled):
            return
        self._drag_enabled = bool(enabled)
        self._press = self._drag_anchor = None
        self.setCursor(Qt.OpenHandCursor if enabled else Qt.ArrowCursor)

    def _send_drag_step(self, point):
        if not self.drag_enabled or self.drag_busy or self._gesture_sent or self._drag_anchor is None:
            return
        delta = point - self._drag_anchor
        now = time.monotonic()
        if max(abs(delta.x()), abs(delta.y())) < 12 or now - self._last_drag_step < .4:
            return
        direction = (2 if delta.x() > 0 else 1) if abs(delta.x()) >= abs(delta.y()) else (4 if delta.y() > 0 else 3)
        self._drag_anchor = point
        self._last_drag_step = now
        self._gesture_sent = True
        self.dragged.emit(direction)

    def set_drag_busy(self, busy):
        self.drag_busy = bool(busy)
        if busy:
            # Discard the held gesture; unlocking never replays queued movement.
            self._press = self._drag_anchor = None
        self.setCursor(Qt.BusyCursor if busy else
                       (Qt.OpenHandCursor if self.drag_enabled else Qt.ArrowCursor))

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.LeftButton:
            self._send_drag_step(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None:
            point = event.position().toPoint()
            delta = point - self._press
            self._send_drag_step(point)
            self._press = None
            self._drag_anchor = None
            self.setCursor(Qt.BusyCursor if self.drag_busy else
                           (Qt.OpenHandCursor if self.drag_enabled else Qt.ArrowCursor))
            if not self._gesture_sent and self._image.isNull() and max(abs(delta.x()), abs(delta.y())) < 12:
                self.retry.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

