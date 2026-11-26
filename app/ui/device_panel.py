# SPDX-License-Identifier: GPL-3.0-or-later
"""通用设备详情面板。

卡片点击后为「非摄像头 / 非灯具」的普通设备提供统一入口：顶部是设备
信息，中部是按 MIoT 规格列出的参数（只读项合成一张紧凑表格），下部是
白名单内的可控属性与设备声明的动作。

两条硬约束（都是踩过坑之后加的）：

1. **参数用一张 HTML 表格渲染，而不是每行一个控件。** 空调有 109 项属性，
   逐行建 QLabel/QSpinBox 会产生上百个子控件，200% 缩放下布局计算极慢，
   历史上表现为「点开卡片后面板空白或直接崩掉」。表格只有一个 QLabel。
2. **只对白名单属性生成可写控件。** 路由器、摄像头这类设备的规格里包含
   PPPoE 密码、WiFi 密码、静态 IP、DNS 等项，误写会把设备网络配置改坏；
   而且路由器属性在米家云端一律返回 -704002000（不允许读取），生成控件
   也拿不到值。这些设备整面板只读，只展示规格与能读到的读数。
"""

import json
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QSizePolicy,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
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

logger = logging.getLogger(__name__)

_PANEL_WIDTH = 460
_PANEL_MAX_HEIGHT = 620
_PANEL_TARGET_HEIGHT = 700
# 可写控件上限：空调这类设备有 100+ 属性、40+ 可写项，逐个建输入控件会让
# 弹面板明显卡顿（200% 缩放下更甚）。超出的项仍然展示，但只作只读读数。
_MAX_CONTROLS = 24

# 属性名里含这些片段的，一律只读：写错会改掉设备的网络/账号配置。
_UNSAFE_WRITE_KEYS = (
    'password', 'passwd', 'encrypt', 'secret', 'token',
    'ssid', 'ip-address', 'subnet', 'gateway', 'dns',
    'mac', 'static', 'ppp',
)

# 整机只读的型号前缀：云端不返回读数（-704002000），且规格里全是网络配置项。
_READONLY_MODEL_PREFIXES = ('xiaomi.router.', 'redmi.router.', 'xiaomi.repeater.')


def _is_readonly_device(device: DeviceInfo) -> bool:
    return device.model.startswith(_READONLY_MODEL_PREFIXES)


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(key in lowered for key in ('password', 'passwd', 'secret', 'token', 'encrypt'))


def _escape(text: str) -> str:
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _short_name(text: str, limit: int = 12) -> str:
    """参数名取中文部分并限长：spec 的 desc 常是「English / 中文」，
    整串放进两列表格会把名称列撑成两三行，值列被挤出去。"""
    text = (text or '').strip()
    if ' / ' in text:
        zh = text.split(' / ', 1)[1].strip()
        if zh and any('\u4e00' <= ch <= '\u9fff' for ch in zh):
            text = zh
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _two_col_table(pairs: list[tuple[str, str]], name_px: int = 200,
                   value_px: int = 190) -> str:
    """两列（名称 / 值）的紧凑表格；值可以是已经转义好的 HTML 片段。

    列宽用像素而不是百分比：Qt 的富文本表格对百分比宽度支持不稳定，实际
    会按内容自适应，长参数名会把值列顶出面板外被裁掉。面板宽度固定 460，
    内容区约 410 px，直接给名称列一个固定宽度最可靠。
    """
    rows = []
    for name, value in pairs:
        rows.append(
            '<tr>'
            '<td width="%d" style="padding:4px 12px 4px 0; color:%s; font-size:9.5pt;">%s</td>'
            '<td width="%d" align="right" style="padding:4px 0; color:%s; font-size:9.5pt;">%s</td>'
            '</tr>' % (name_px, SiColors.TEXT_SECONDARY, _escape(name),
                       value_px, SiColors.TEXT_PRIMARY, value))
    return ('<table width="100%" cellspacing="0" cellpadding="0" style="border:none;">'
            + ''.join(rows) + '</table>')


class DevicePanelDialog(OverlayDialog):
    """单台设备的通用详情面板（信息 / 参数 / 可控项 / 动作）。"""

    def __init__(self, service, jobs, device: DeviceInfo, parent=None):
        super().__init__(parent, overlay=True, modal=Qt.WindowModal)
        self._service = service
        self._jobs = jobs
        self.device = device
        self._detail = None
        self._controls: dict[str, callable] = {}
        self._values: dict[str, object] = {}
        self._readonly_device = _is_readonly_device(device)
        self._panel_height = _PANEL_TARGET_HEIGHT
        self._busy = False
        self._closed = False

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
        if self._closed or self._busy:
            return
        if not self.device.online:
            self._hint.setText('设备当前离线，只能查看基础信息。')
            self._build_info()
            return
        self._busy = True
        self._hint.setText('正在读取设备规格…')
        self._refresh.setEnabled(False)
        did = self.device.did

        def failure(error):
            self._busy = False
            if self._closed or not shiboken6.isValid(self):
                return
            self._refresh.setEnabled(True)
            self._hint.setText(f'读取设备详情失败：{error}')
            self._build_info()

        self._jobs.submit(lambda: self._service.device_detail(did),
                          on_success=self._on_detail, on_error=failure)

    def _on_detail(self, detail) -> None:
        self._detail = detail
        self._busy = False
        if self._closed or not shiboken6.isValid(self):
            return
        self._refresh.setEnabled(True)
        try:
            self._render(detail)
        except Exception as exc:  # 渲染异常不能把整个面板带崩
            logger.exception('设备面板渲染失败: %s', self.device.did)
            self._hint.setText(f'面板渲染失败：{exc}')

    def _reset_body(self) -> None:
        """整块换掉滚动区的内容容器。

        早先的做法是逐项 takeAt + deleteLater 清空旧内容。反复刷新/切换设备
        时，被移出的子布局和控件会在 Qt 内部残留引用，压测里表现为十几轮
        之后进程 access violation（堆已被写坏）。换成「旧容器整个丢弃、新
        容器重建」后不再有残留。
        """
        old = self._scroll.takeWidget()
        if old is not None:
            old.setParent(None)
            old.deleteLater()
        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 4, 4)
        self._body_layout.setSpacing(8)
        self._scroll.setWidget(self._body)
        self._controls.clear()
        self._values.clear()
        self._table = None
        self._table_props = []
        self._info_built = False

    def _render(self, detail) -> None:
        self._reset_body()

        all_writable = [p for p in detail.props if p.writable and self._writable_allowed(p)]
        # 按 spec 原始顺序保留：厂家把电源、模式、温度这些常用项排在前面，
        # 不要按类型重排，否则空调只剩一排开关、模式和温度却被挤到只读表。
        writable = all_writable[:_MAX_CONTROLS]
        chosen = {p.name for p in writable}
        readonly = [p for p in detail.props if p.name not in chosen]

        self._build_info()

        if readonly:
            self._section('设备参数')
            self._build_values_table(readonly)

        if writable:
            self._section('可控功能')
            for prop in writable:
                try:
                    self._add_control(prop)
                except Exception:  # 单项控件失败不影响其余
                    logger.exception('属性控件创建失败: %s/%s', self.device.did, prop.name)

        if detail.actions:
            self._section('设备动作')
            self._add_actions(detail)

        if not detail.props and not detail.actions:
            self._hint.setText('该设备没有公开的功能规格，仅支持查看基础信息。')
        else:
            self._hint.setText('')
        self._load_values([p.name for p in detail.props if p.readable])
        # 内容建完才能拿到真实 sizeHint：showEvent 里算高度时面板还是空的，
        # 不重新定位的话面板会一直停在「只有标题」的高度。
        QTimer.singleShot(0, self._reposition)

    def _writable_allowed(self, prop) -> bool:
        if self._readonly_device:
            return False
        name = prop.name.lower()
        if any(key in name for key in _UNSAFE_WRITE_KEYS):
            return False
        if prop.type == 'bool':
            return True
        if prop.value_list and len([e for e in prop.value_list if isinstance(e, dict)]) > 1:
            return True
        rng = prop.range or ()
        return prop.type.startswith(('int', 'uint', 'float')) and len(rng) >= 2

    # ---------- 只读参数表 ----------

    def _build_values_table(self, props) -> None:
        self._table_props = list(props)
        self._table = QLabel()
        self._table.setTextFormat(Qt.RichText)
        self._table.setWordWrap(True)
        # 宽度必须由父布局决定：默认 sizeHint 按内容撑宽，长参数名会把
        # 整张表顶出面板右侧，值列被裁掉。
        self._table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._table.setStyleSheet("background: transparent;")
        self._table.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._body_layout.addWidget(self._table)
        self._refresh_values_table()

    def _refresh_values_table(self) -> None:
        if not getattr(self, '_table', None) or not shiboken6.isValid(self._table):
            return
        pairs = []
        for prop in getattr(self, '_table_props', []):
            value = self._values.get(prop.name)
            if value is None:
                text = '<span style="color:%s">—</span>' % SiColors.TEXT_MUTED
            else:
                text = _escape(self._format(prop.name, value))
            pairs.append((_short_name(prop.desc or prop.name), text))
        self._table.setText(_two_col_table(pairs, name_px=200))

    def _add_control(self, prop) -> None:
        row = self._row(prop.desc or prop.name)
        widget = self._make_control(prop)
        if widget is None:
            value = QLabel('—')
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 10pt;")
            row.addWidget(value)
            self._controls[prop.name] = lambda v, w=value: self._set_label(w, v)
            return
        row.addWidget(widget)

    def _make_control(self, prop):
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
                combo.addItem(str(entry.get('desc_zh_cn') or entry.get('description')
                                  or entry.get('value')), entry.get('value'))
            if combo.count() == 0:
                return None
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
            # QSpinBox 只能装 32 位 int；有的属性 range 到 4294967295 甚至更大，
            # 直接 setRange 会抛 OverflowError（吊扇 jog-mode 就是），裁到 int32。
            low = max(int(rng[0]), -2147483648)
            high = min(int(rng[1]), 2147483647)
            if high <= low:
                return None
            spin = QSpinBox()
            spin.setRange(low, high)
            if len(rng) >= 3 and 1 < int(rng[2]) <= high - low:
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
        grid_layout = QGridLayout(grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(8)
        for index, action in enumerate(detail.actions[:12]):
            button = QPushButton(action.desc or action.name)
            button.setCursor(Qt.PointingHandCursor)
            self._style_button(button, danger=bool(action.desc and '重启' in action.desc))
            button.clicked.connect(
                lambda checked=False, name=action.name, b=button: self._run_action(name, b))
            grid_layout.addWidget(button, index // 2, index % 2)
        self._body_layout.addWidget(grid)

    def _build_info(self) -> None:
        if getattr(self, '_info_built', False):
            return
        self._info_built = True
        self._section('设备信息')
        label = QLabel()
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        label.setStyleSheet("background: transparent;")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setText(_two_col_table([
            ('型号', _escape(self.device.model or '—')),
            ('设备 ID', '<span style="font-size:8.5pt">%s</span>' % _escape(self.device.did)),
            ('家庭', _escape(self.device.home_name or '—')),
            ('固件', _escape(self.device.firmware or '—')),
        ], name_px=110, value_px=280))
        self._body_layout.addWidget(label)

    # ---------- 值同步 ----------

    def _load_values(self, names: list[str]) -> None:
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            return
        did = self.device.did

        def success(values):
            if self._closed or not shiboken6.isValid(self):
                return
            try:
                self._apply_values(values)
            except Exception:
                logger.exception('设备面板取值渲染失败: %s', did)

        def failure(error):
            if self._closed or not shiboken6.isValid(self):
                return
            self._hint.setText(f'读取设备读数失败：{error}')

        self._jobs.submit(lambda: self._service.read_props(did, names),
                          on_success=success, on_error=failure)

    def _apply_values(self, values: dict) -> None:
        for name, value in (values or {}).items():
            if value is None:
                continue
            self._values[name] = value
            setter = self._controls.get(name)
            if setter is not None:
                try:
                    setter(value)
                except Exception:
                    logger.debug('控件回填失败: %s', name, exc_info=True)
        self._refresh_values_table()
        if self._readonly_device and not any(v is not None for v in (values or {}).values()):
            self._hint.setText(
                '云端没有返回实时读数：该型号通常不允许通过米家云端读取参数，'
                '下面按设备规格列出它支持的项。')

    @staticmethod
    def _format(name: str, value) -> str:
        if _is_secret(name):
            return '••••••'
        if isinstance(value, bool):
            return '开' if value else '关'
        if isinstance(value, float):
            return f'{value:g}'
        if isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
            return text if len(text) <= 24 else text[:21] + '…'
        text = str(value)
        return text if len(text) <= 24 else text[:21] + '…'

    @staticmethod
    def _set_label(label: QLabel, value) -> None:
        if shiboken6.isValid(label):
            label.setText(DevicePanelDialog._format('', value))

    @staticmethod
    def _set_toggle(button: QPushButton, value) -> None:
        if not shiboken6.isValid(button):
            return
        state = bool(value)
        button.blockSignals(True)
        button.setChecked(state)
        button.setText('开' if state else '关')
        button.blockSignals(False)

    @staticmethod
    def _set_combo(combo: QComboBox, value) -> None:
        if not shiboken6.isValid(combo):
            return
        index = combo.findData(value)
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    @staticmethod
    def _set_spin(spin, value) -> None:
        if not shiboken6.isValid(spin):
            return
        try:
            spin.blockSignals(True)
            spin.setValue(value)
        finally:
            try:
                spin.blockSignals(False)
            except Exception:
                pass

    # ---------- 写入 ----------

    def _write(self, name: str, value) -> None:
        if not self.device.online:
            return
        self._hint.setText(f'正在设置 {name}…')
        did = self.device.did

        def success(_result):
            if self._closed or not shiboken6.isValid(self):
                return
            self._hint.setText('已生效')

        def failure(error):
            if self._closed or not shiboken6.isValid(self):
                return
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
            if self._closed or not shiboken6.isValid(self):
                return
            self._hint.setText('动作已下发')

        def failure(error):
            restore()
            if self._closed or not shiboken6.isValid(self):
                return
            self._hint.setText(f'动作失败：{error}')

        self._jobs.submit(lambda: self._service.run_action(did, name),
                          on_success=success, on_error=failure)

    # ---------- 生命周期 ----------

    def closeEvent(self, event):  # noqa: N802
        self._closed = True
        self._controls.clear()
        self._values.clear()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._place_overlay()
        self._center_panel()
        if self._detail is None:
            self.reload()

    def _center_panel(self) -> None:
        # _fill_parent_window 会改对话框几何并触发 resizeEvent，进而在
        # 这里被重入；用一个闸门避免无限递归。
        if getattr(self, '_positioning', False):
            return
        self._positioning = True
        try:
            if self.width() < 600 or self.height() < 400:
                # showEvent 时窗口还可能是默认尺寸，先撑到父窗口或屏幕
                if not self._fill_parent_window():
                    self._center_on_screen(900, 760, 40)
            width = min(_PANEL_WIDTH, max(360, self.width() - 40))
            height = min(self._panel_height, max(360, self.height() - 40))
            self._panel.setGeometry((self.width() - width) // 2,
                                    (self.height() - height) // 2, width, height)
        finally:
            self._positioning = False

    def _reposition(self) -> None:
        """内容建好后再定一次高度：属性少的设备不该占满一屏。"""
        if self._closed or not shiboken6.isValid(self):
            return
        try:
            content = self._body.sizeHint().height()
        except Exception:
            content = 0
        if content > 0:
            self._panel_height = min(_PANEL_TARGET_HEIGHT, max(360, content + 190))
        self._center_panel()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        self._center_panel()
