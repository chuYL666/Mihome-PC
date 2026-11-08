# SPDX-License-Identifier: GPL-3.0-or-later
"""通用设备详情面板。

卡片点击后为「非摄像头 / 非灯具」的普通设备提供统一入口：
上半部分按设备规格（MIoT spec）自动生成可控属性控件（开关、枚举、
数值），中部是设备声明的动作（重启、校准、格式化等），底部是设备
信息（型号、设备 ID、固件）。规格里没有可写属性的设备（传感器、
路由器等）只展示只读读数与信息，不会出现空白面板。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from app.core.models import DeviceInfo
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import SiColors

_PANEL_WIDTH = 430
_PANEL_MAX_HEIGHT = 620


class DevicePanelDialog(OverlayDialog):
    """单台设备的通用详情面板（属性 / 动作 / 信息）。"""

    def __init__(self, service, jobs, device: DeviceInfo, parent=None):
        super().__init__(parent, overlay=True, modal=Qt.WindowModal)
        self._service = service
        self._jobs = jobs
        self.device = device
        self._detail = None
        self._controls: dict[str, callable] = {}
        self._labels: dict[str, QLabel] = {}
        self._busy = False

        panel = self._panel
        panel.setMinimumWidth(_PANEL_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(device.name)
        title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            f" font-size: 14pt; font-weight: 600;")
        titles.addWidget(title)
        subtitle = QLabel(self._subtitle_text())
        subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9.5pt;")
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        header.addWidget(self._make_close_button(), alignment=Qt.AlignTop)
        outer.addLayout(header)

        self._scroll = QScrollArea(panel)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.setMaximumHeight(_PANEL_MAX_HEIGHT)
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 4, 4)
        self._body_layout.setSpacing(8)
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)

        self._hint = QLabel('正在读取设备规格…')
        self._hint.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent; font-size: 9.5pt;")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._refresh = QPushButton('刷新')
        self._refresh.setCursor(Qt.PointingHandCursor)
        self._refresh.clicked.connect(self.reload)
        footer.addWidget(self._refresh)
        outer.addLayout(footer)
        self._style_button(self._refresh)

    # ---------- 外观 ----------

    def _subtitle_text(self) -> str:
        parts = [p for p in (self.device.room_name, '在线' if self.device.online else '离线') if p]
        return ' · '.join(parts)

    def _style_button(self, button: QPushButton, danger: bool = False) -> None:
        background = SiColors.DANGER if danger else SiColors.SURFACE
        button.setStyleSheet(
            f"QPushButton {{ background: {background}; color: {SiColors.TEXT_PRIMARY};"
            f" border: none; border-radius: 8px; padding: 7px 14px; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}"
            f"QPushButton:disabled {{ color: {SiColors.TEXT_MUTED}; }}")

    def _section(self, text: str) -> None:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            f" font-size: 9pt; font-weight: 600; padding-top: 6px;")
        self._body_layout.addWidget(label)

    def _row(self, text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 10pt;")
        row.addWidget(label, 1)
        self._body_layout.addLayout(row)
        return row

    # ---------- 加载 ----------

    def reload(self) -> None:
        if self._busy:
            return
        if not self.device.online:
            self._hint.setText('设备当前离线，只能查看基础信息。')
            self._build_info()
            return
        self._busy = True
        self._hint.setText('正在读取设备规格…')
        did = self.device.did

        def failure(error):
            self._busy = False
            if shiboken6.isValid(self):
                self._hint.setText(f'读取设备详情失败：{error}')

        self._jobs.submit(lambda: self._service.device_detail(did),
                          on_success=self._on_detail, on_error=failure)

    def _on_detail(self, detail) -> None:
        self._detail = detail
        self._busy = False
        if not shiboken6.isValid(self):
            return
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        self._controls.clear()
        self._labels.clear()

        writable = [p for p in detail.props if p.writable]
        readonly = [p for p in detail.props if p.readable and not p.writable]
        if writable:
            self._section('可控功能')
            for prop in writable:
                self._add_prop(prop, editable=True)
        if readonly:
            self._section('当前状态')
            for prop in readonly[:40]:
                self._add_prop(prop, editable=False)
        if detail.actions:
            self._section('设备动作')
            self._add_actions(detail)
        if not writable and not readonly and not detail.actions:
            self._hint.setText('该设备没有公开的可控属性，仅支持查看信息。')
        else:
            self._hint.setText('')
        self._build_info()
        self._load_values([p.name for p in detail.props if p.readable])

    def _add_prop(self, prop, editable: bool) -> None:
        row = self._row(prop.desc or prop.name)
        widget = self._make_control(prop, editable)
        if widget is not None:
            row.addWidget(widget)
        else:
            value = QLabel('—')
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 10pt;")
            self._labels[prop.name] = value
            row.addWidget(value)

    def _make_control(self, prop, editable: bool):
        if not editable:
            return None
        if prop.type == 'bool':
            button = QPushButton('—')
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(72)
            self._style_button(button)
            button.clicked.connect(
                lambda checked=False, name=prop.name: self._write(name, bool(checked)))
            self._controls[prop.name] = lambda value, b=button: self._set_toggle(b, value)
            return button
        if prop.value_list:
            combo = QComboBox()
            combo.setMinimumWidth(110)
            for entry in prop.value_list:
                if not isinstance(entry, dict):
                    continue
                combo.addItem(str(entry.get('desc_zh_cn') or entry.get('description') or entry.get('value')),
                              entry.get('value'))
            combo.setStyleSheet(
                f"QComboBox {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY};"
                f" border: none; border-radius: 8px; padding: 5px 10px; }}")
            combo.currentIndexChanged.connect(
                lambda _index, name=prop.name, c=combo: self._write(
                    name, c.currentData()))
            self._controls[prop.name] = lambda value, c=combo: self._set_combo(c, value)
            return combo
        rng = prop.range or ()
        if prop.type.startswith(('int', 'uint')) and len(rng) >= 2:
            spin = QSpinBox()
            spin.setRange(int(rng[0]), int(rng[1]))
            if len(rng) >= 3 and int(rng[2]) > 1:
                spin.setSingleStep(int(rng[2]))
            spin.setMinimumWidth(96)
            spin.setStyleSheet(
                f"QSpinBox {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY};"
                f" border: none; border-radius: 8px; padding: 5px 8px; }}")
            spin.editingFinished.connect(
                lambda name=prop.name, s=spin: self._write(name, s.value()))
            self._controls[prop.name] = lambda value, s=spin: self._set_spin(s, value)
            return spin
        if prop.type == 'float' and len(rng) >= 2:
            spin = QDoubleSpinBox()
            spin.setRange(float(rng[0]), float(rng[1]))
            spin.setSingleStep(float(rng[2]) if len(rng) >= 3 else .1)
            spin.setMinimumWidth(96)
            spin.setStyleSheet(
                f"QDoubleSpinBox {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY};"
                f" border: none; border-radius: 8px; padding: 5px 8px; }}")
            spin.editingFinished.connect(
                lambda name=prop.name, s=spin: self._write(name, s.value()))
            self._controls[prop.name] = lambda value, s=spin: self._set_spin(s, value)
            return spin
        return None

    def _add_actions(self, detail) -> None:
        grid = QWidget()
        grid.setStyleSheet("background: transparent;")
        grid_layout = QHBoxLayout(grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(8)
        for action in detail.actions[:8]:
            button = QPushButton(action.desc or action.name)
            button.setCursor(Qt.PointingHandCursor)
            self._style_button(button)
            button.clicked.connect(
                lambda checked=False, name=action.name, b=button: self._run_action(name, b))
            grid_layout.addWidget(button)
        grid_layout.addStretch(1)
        self._body_layout.addWidget(grid)

    def _build_info(self) -> None:
        self._section('设备信息')
        for text, value in [('型号', self.device.model),
                            ('设备 ID', self.device.did),
                            ('家庭', self.device.home_name or '—'),
                            ('固件', self.device.firmware or '—')]:
            row = self._row(text)
            label = QLabel(value or '—')
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9.5pt;")
            row.addWidget(label)

    # ---------- 值同步 ----------

    def _load_values(self, names: list[str]) -> None:
        if not names:
            return
        did = self.device.did

        def success(values):
            if not shiboken6.isValid(self):
                return
            for name, value in values.items():
                if value is None:
                    continue
                setter = self._controls.get(name)
                if setter is not None:
                    try:
                        setter(value)
                    except Exception:
                        pass
                label = self._labels.get(name)
                if label is not None:
                    label.setText(self._format(value))

        self._jobs.submit(lambda: self._service.read_props(did, names),
                          on_success=success,
                          on_error=lambda _error: None)

    @staticmethod
    def _format(value) -> str:
        if isinstance(value, bool):
            return '开' if value else '关'
        if isinstance(value, float):
            return f'{value:g}'
        text = str(value)
        return text if len(text) <= 28 else text[:25] + '…'

    @staticmethod
    def _set_toggle(button: QPushButton, value) -> None:
        state = bool(value)
        button.blockSignals(True)
        button.setChecked(state)
        button.setText('开' if state else '关')
        button.blockSignals(False)

    @staticmethod
    def _set_combo(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    @staticmethod
    def _set_spin(spin, value) -> None:
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    # ---------- 写入 ----------

    def _write(self, name: str, value) -> None:
        if not self.device.online:
            return
        self._hint.setText(f'正在设置 {name}…')
        did = self.device.did

        def success(_result):
            if shiboken6.isValid(self):
                self._hint.setText('已生效')

        def failure(error):
            if shiboken6.isValid(self):
                self._hint.setText(f'设置失败：{error}')

        self._jobs.submit(lambda: self._service.write_prop(did, name, value),
                          on_success=success, on_error=failure)

    def _run_action(self, name: str, button: QPushButton) -> None:
        button.setEnabled(False)
        did = self.device.did

        def restore():
            if shiboken6.isValid(button):
                button.setEnabled(True)

        def success(_result):
            restore()
            if shiboken6.isValid(self):
                self._hint.setText('动作已下发')

        def failure(error):
            restore()
            if shiboken6.isValid(self):
                self._hint.setText(f'动作失败：{error}')

        self._jobs.submit(lambda: self._service.run_action(did, name),
                          on_success=success, on_error=failure)

    # ---------- 定位 ----------

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._place_overlay()
        self._center_panel()
        if self._detail is None:
            self.reload()

    def _center_panel(self) -> None:
        panel = self._panel
        panel.adjustSize()
        width = min(_PANEL_WIDTH, self.width() - 40)
        height = min(panel.sizeHint().height(), self.height() - 60)
        panel.setGeometry((self.width() - width) // 2, (self.height() - height) // 2,
                          width, height)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        self._center_panel()
