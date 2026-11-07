# SPDX-License-Identifier: GPL-3.0-or-later
"""Small everyday light controls, without the generic device editor."""
import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import (QColorDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)
from app.core.device_controls import light_controls
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors


class LightSlider(QSlider):
    committed = Signal()

    def __init__(self):
        super().__init__(Qt.Horizontal)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self.committed.emit()

    def wheelEvent(self, event):
        event.ignore()

    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
                           Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown):
            self.committed.emit()


class LightControlDialog(QWidget):
    """Shared controls: an ordinary child widget in tray, a dialog window on desktop."""
    power_changed = Signal(bool)
    finished = Signal(int)

    def __init__(self, service, jobs, device, parent=None, *, embedded=False):
        super().__init__(parent)
        self._embedded = embedded
        self._backdrop = None
        self.setWindowFlags(Qt.Widget if embedded else Qt.Dialog | Qt.FramelessWindowHint)
        self.setWindowTitle(device.name)
        self.setAttribute(Qt.WA_TranslucentBackground, not embedded)
        if embedded:
            self.setObjectName('lightControlPage')
            self.setAttribute(Qt.WA_StyledBackground)
            self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setWindowModality(Qt.NonModal if embedded else Qt.WindowModal if parent else Qt.NonModal)
        self._service, self._jobs, self.device = service, jobs, device
        self._closed = False
        self._busy = self._read_pending = self._loaded = False
        self._props, self._values, self._sliders, self._labels = {}, {}, {}, {}
        self._power_prop = None
        self._dialog_screen = None
        self._last_power = None
        self._interaction_revision = 0
        self._color_button = None
        self._panel = QFrame(self)
        self._panel.setObjectName('lightPanel')
        self._outer_layout = outer = QVBoxLayout(self)
        outer.setContentsMargins(*(20, 18, 20, 18) if embedded else (24, 24, 24, 24))
        if embedded:
            outer.addStretch(1)
        outer.addWidget(self._panel, alignment=Qt.AlignHCenter if embedded else Qt.Alignment())
        if embedded:
            outer.addStretch(1)
            self._panel.setFixedWidth(336)
        else:
            shadow = QGraphicsDropShadowEffect(self._panel)
            shadow.setBlurRadius(40)
            shadow.setOffset(0, 3)
            shadow.setColor(QColor(0, 0, 0, 60))
            self._panel.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self._panel)
        layout.setContentsMargins(*(18, 18, 18, 20) if embedded else (26, 22, 26, 24))
        layout.setSpacing(16 if embedded else 22)
        self._header = QWidget(self._panel)
        self._header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._head = head = QHBoxLayout(self._header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        self._title = QLabel(device.name)
        self._title.setFont(QFont('Microsoft YaHei UI', 10 if embedded else 12, QFont.DemiBold))
        self._title.setWordWrap(True)
        title_column = QVBoxLayout()
        title_column.setAlignment(Qt.AlignVCenter)
        title_column.setSpacing(3)
        title_column.addWidget(self._title)
        self._power_text = QLabel('状态待确认')
        self._power_text.setStyleSheet(f'color: {SiColors.TEXT_SECONDARY}; font-size: 9pt; background: transparent;')
        self._power_text.hide()
        title_column.addWidget(self._power_text)
        head.addLayout(title_column, 1)
        if not embedded:
            diagnostic = QPushButton('诊断报告')
            diagnostic.clicked.connect(self._show_diagnostics)
            head.addWidget(diagnostic)
        self._close_button = close = QPushButton('✕')
        close.setFixedSize(24 if embedded else 28, 24 if embedded else 28)
        close.setAccessibleName('返回设备列表' if embedded else '关闭灯光控制')
        close.clicked.connect(self.reject)
        head.addWidget(close)
        layout.addWidget(self._header)
        self._body = QWidget()
        self._body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(18 if embedded else 24)
        self._body.hide()
        layout.addWidget(self._body)
        self._status = QLabel('正在读取灯光状态…')
        self._status.setWordWrap(True)
        self._status.setFixedHeight(max(18, self._status.fontMetrics().height() + 4))
        status_policy = self._status.sizePolicy()
        status_policy.setRetainSizeWhenHidden(True)
        self._status.setSizePolicy(status_policy)
        layout.addWidget(self._status)
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self.refresh)
        if not embedded:
            self.setFixedWidth(480)  # 432 content + glow margins; no DPI/font scaling.
        self.retheme()

    def _show_diagnostics(self):
        from app.ui.diagnostic_dialog import show_device_diagnostics
        show_device_diagnostics(self, self._service, self._jobs, [self.device])

    def _alive(self):
        return shiboken6.isValid(self) and not self._closed

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        self._jobs.submit(lambda: self._service.device_detail(self.device.did),
                          on_success=self._build, on_error=self._failed)

    def _build(self, detail):
        if not self._alive():
            return
        self._props = light_controls(detail.props)
        if not self._props:
            self.reject()  # A plain on/off light never needs a second page.
            return
        self._power_prop = next((p for p in detail.props if p.type == 'bool'
            and p.writable and (p.name == 'on' or p.name.startswith(('on-', 'on_')))), None)
        if self._power_prop:
            self._power = PowerButton(28 if self._embedded else 36, 20 if self._embedded else 22)
            self._power.setEnabled(False)
            self._power.clicked.connect(self._toggle)
            self._head.insertWidget(0, self._power, alignment=Qt.AlignVCenter)
            self._power_text.show()
        for kind in ('brightness', 'temperature'):
            if kind not in self._props:
                continue
            prop = self._props[kind]
            column = QVBoxLayout()
            column.setSpacing(8 if self._embedded else 12)
            label = QLabel('亮度' if kind == 'brightness' else '色温')
            value = QLabel('—')
            row = QHBoxLayout()
            row.addWidget(label, 1)
            row.addWidget(value)
            column.addLayout(row)
            slider = LightSlider()
            slider.setRange(0, 100 if kind == 'brightness' else 1000)
            slider.setEnabled(False)
            slider.setAccessibleName('灯光亮度' if kind == 'brightness' else '灯光色温')
            slider.sliderPressed.connect(self._begin_interaction)
            slider.valueChanged.connect(lambda _, k=kind: self._preview(k))
            slider.committed.connect(lambda k=kind: self._write(
                self._props[k].name, self._slider_value(k)))
            column.addWidget(slider)
            self._sliders[kind], self._labels[kind] = slider, value
            self._body_layout.addLayout(column)
        if 'color' in self._props:
            self._color_button = QPushButton('选择灯光颜色')
            self._color_button.setEnabled(False)
            self._color_button.setMinimumHeight(36)
            self._color_button.clicked.connect(self._choose_color)
            self._body_layout.addWidget(self._color_button)
        self._body.show()
        for value_label in self._labels.values():
            value_label.ensurePolished()
            value_label.setFixedHeight(max(18, value_label.fontMetrics().height() + 4))
        if not self._embedded:
            self._panel.ensurePolished()
            self._panel.layout().activate()
            self.layout().activate()
            self.setFixedHeight(self.sizeHint().height() + 8)
            self._place_centered()
        self.refresh()
        self._poll.start()

    def _slider_value(self, kind):
        prop, slider = self._props[kind], self._sliders[kind]
        low, high = prop.range[:2]
        value = (high * slider.value() / 100 if kind == 'brightness' else
                 low + (high - low) * slider.value() / slider.maximum())
        step = prop.range[2] if len(prop.range) > 2 and prop.range[2] else 1
        value = max(low, min(high, low + round((value - low) / step) * step))
        return float(value) if prop.type == 'float' else int(round(value))

    def _preview(self, kind):
        value = self._slider_value(kind)
        if kind == 'brightness':
            value = round(100 * value / self._props[kind].range[1])
        self._labels[kind].setText(f'{value:g}' + ('%' if kind == 'brightness' else ' K'))

    def refresh(self):
        if not self._alive() or self._busy or self._read_pending:
            return
        if any(slider.isSliderDown() for slider in self._sliders.values()):
            return
        names = [p.name for p in self._props.values() if p.readable]
        if self._power_prop and self._power_prop.readable:
            names.append(self._power_prop.name)
        self._read_pending = True
        revision = self._interaction_revision
        def success(values):
            if self._alive():
                self._read_pending = False
                if not self._busy and revision == self._interaction_revision and not any(
                        slider.isSliderDown() for slider in self._sliders.values()):
                    self._values.update(values)
                    self._render()
        def failure(error):
            if self._alive():
                self._read_pending = False
                self._failed(error)
        self._jobs.submit(lambda: self._service.read_props(self.device.did, names),
                          on_success=success, on_error=failure)

    def _render(self):
        enabled = self._adjustments_allowed()
        if self._power_prop:
            state = self._values.get(self._power_prop.name)
            self._power.set_state(state)
            self._power.setEnabled(state is not None and not self._busy)
            self._power_text.setText('状态待确认' if state is None else '已开启' if state else '已关闭')
            if isinstance(state, bool) and state != self._last_power:
                self._last_power = state
                self.power_changed.emit(state)
        for kind, slider in self._sliders.items():
            prop = self._props[kind]
            value = self._values.get(prop.name)
            slider.setEnabled(enabled and value is not None and not self._busy)
            if value is None:
                continue
            low, high = prop.range[:2]
            fraction = value / high if kind == 'brightness' else (value - low) / (high - low)
            slider.blockSignals(True)
            slider.setValue(round(fraction * slider.maximum()))
            slider.blockSignals(False)
            self._preview(kind)
        if self._color_button:
            self._color_button.setEnabled(enabled and not self._busy)
        self._status.setText('')
        self._status.hide()

    def _toggle(self):
        current = self._values.get(self._power_prop.name)
        if isinstance(current, bool):
            self._write(self._power_prop.name, not current)

    def _adjustments_allowed(self):
        return (self._power_prop is not None
                and self._values.get(self._power_prop.name) is True
                and not self._busy)

    def _choose_color(self):
        if not self._adjustments_allowed():
            return
        prop = self._props['color']
        current = self._values.get(prop.name, 0xffffff)
        color = QColorDialog.getColor(QColor.fromRgb(current or 0), self, '选择灯光颜色')
        if color.isValid() and self._alive():
            self._write(prop.name, color.rgb() & 0xffffff)

    def _write(self, name, value):
        if self._busy or not self._alive():
            return
        if (self._power_prop is None or name != self._power_prop.name) and not self._adjustments_allowed():
            return
        self._busy = True
        self._begin_interaction()
        if hasattr(self, '_power'):
            self._power.setEnabled(False)
        self._body.setEnabled(False)
        self._status.setText('正在设置…')
        self._status.show()
        def success(_):
            if self._alive():
                self._busy = False
                self._body.setEnabled(True)
                self._values[name] = value
                self._render()
                # Some lamps turn on themselves after a brightness/temperature
                # write. Read their actual power state instead of assuming it
                # stayed unchanged; never send an implicit power command.
                self.refresh()
        def failure(error):
            if self._alive():
                self._busy = False
                self._body.setEnabled(True)
                self._render()
                self._failed(error)
        self._jobs.submit(lambda: self._service.write_prop(self.device.did, name, value),
                          on_success=success, on_error=failure)

    def _begin_interaction(self):
        self._interaction_revision += 1

    def _failed(self, error):
        if self._alive():
            self._status.setText('暂时无法读取或设置灯光，请稍后重试。')
            self._status.show()

    def show_centered(self):
        self._dialog_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.adjustSize()
        self._place_centered()
        self.show()
        self.raise_()
        self.activateWindow()

    def _place_centered(self):
        if self._embedded:
            return
        parent = self.parentWidget()
        parent_window = parent.window() if parent is not None and parent.window().isVisible() else None
        screen = parent_window.screen() if parent_window is not None else self._dialog_screen
        if screen not in QGuiApplication.screens():
            screen = QGuiApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            center = parent_window.frameGeometry().center() if parent_window is not None else area.center()
            point = center - self.rect().center()
            self.move(QPoint(max(area.left(), min(point.x(), area.right() + 1 - self.width())),
                             max(area.top(), min(point.y(), area.bottom() + 1 - self.height()))))

    def done(self, result):
        if self._closed:
            return
        self._closed = True
        self._poll.stop()
        self.hide()
        self.finished.emit(result)

    def reject(self):
        self.done(0)

    def closeEvent(self, event):
        self.reject()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        if self._embedded:
            self._panel.setFixedWidth(min(336, max(180, self.width() - 40)))
        super().resizeEvent(event)

    def set_backdrop(self, pixmap):
        self._backdrop = pixmap
        self.update()

    def paintEvent(self, event):
        if self._embedded:
            # Fully repaint an opaque backing: the old live page is hidden.
            # Transparent siblings plus native hover repaints can otherwise
            # punch holes through the scrim on Windows.
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), QColor(SiColors.CARD))
            if self._backdrop is not None and not self._backdrop.isNull():
                painter.drawPixmap(self.rect(), self._backdrop)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(20, 22, 28, 125))
            painter.drawRect(self.rect())
            painter.end()
        else:
            super().paintEvent(event)

    def retheme(self):
        if self._embedded:
            self.setStyleSheet(f'QWidget#lightControlPage {{ background: {SiColors.CARD}; }}')
        small_label = 'font-size: 9pt;' if self._embedded else ''
        handle = 14 if self._embedded else 16
        self._panel.setStyleSheet(
            f'QFrame#lightPanel {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 14px; }}'
            f'QLabel {{ color: {SiColors.TEXT_PRIMARY}; background: transparent; {small_label} }}'
            f'QPushButton {{ color: {SiColors.TEXT_PRIMARY}; background: {SiColors.SURFACE}; border: none; border-radius: 8px; padding: 5px; }}'
            f'QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}'
            f'QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED}; }}'
            f'QSlider::groove:horizontal {{ height: 5px; background: {SiColors.SURFACE}; border-radius: 2px; }}'
            f'QSlider::sub-page:horizontal {{ background: {SiColors.THEME}; border-radius: 2px; }}'
            f'QSlider::handle:horizontal {{ width: {handle}px; margin: -6px 0; border: none; border-radius: {handle//2}px; background: {SiColors.THEME}; }}'
            f'QSlider::sub-page:horizontal:disabled {{ background: {SiColors.LINE}; }}'
            f'QSlider::handle:horizontal:disabled {{ background: {SiColors.TEXT_DISABLED}; }}')
        if self._embedded:
            self._title.setStyleSheet(f'color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 10pt; font-weight: 600;')
        shadow = self._panel.graphicsEffect()
        if isinstance(shadow, QGraphicsDropShadowEffect):
            shadow.setColor(QColor(0, 0, 0, 105 if QColor(SiColors.CARD).lightness() < 128 else 60))
        if hasattr(self, '_power'):
            self._power._apply()
