# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""米家风格的设备卡片。

卡片按设备角色提供常用操作：圆形电源钮直接开关，面板本体只展示；
调光调色灯点击打开小控制窗，其他设备使用各自的详情路径。

设备是否支持开关在列表接口里拿不到：电源钮初始隐藏，后台批量探测
确认有可写开关属性后才显示并回填真实状态。
"""

from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QDrag
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from app.siui.components.container import SiRowCard

from app.core.models import DeviceInfo
from app.core.device_controls import device_title, device_subtitle, has_device_details, supports_card_power, is_camera, is_light, tray_eligible
from app.ui.camera_play_button import CameraPlayButton
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors, is_dark
import numpy
import qtawesome as qta

_POWER_BTN_SIZE = 36
# 卡片高度 92，图标占 46 才不至于在 212px 宽的卡片里缩成一个小点。
# 云端产品图只有 168×168（实物包围盒 80~120px），显示面积越大越能看清细节。
_ICON_SIZE = 46
# 图标底块内留 3px，产品图不贴圆角边缘
_ICON_INNER = _ICON_SIZE - 6
_ICON_RADIUS = 13
# 深色主题下黑色机身产品用的浅底（见 _icon_tile）
_DARK_PRODUCT_TILE = "#e6ebf3"

# model 图标渲染结果缓存：(路径, mtime, 目标物理像素, DPR) -> QPixmap
_ICON_CACHE: dict[tuple[str, float, int, float], QPixmap] = {}
_ICON_CACHE_LIMIT = 96


def _trim_transparent(image: QImage) -> QImage:
    """裁掉图标四周的透明留白。

    米家 CDN 的产品图固定 168×168，实物只占中间一块（外面还带投影和
    大片透明区）。直接缩到显示尺寸会让图标显得又小又糊；按 alpha 通道
    求包围盒裁一次，同样的容器里图标能大一圈、细节也留得住。
    """
    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return image
    data = bytes(img.constBits())
    bpl = img.bytesPerLine()
    top, bottom, left, right = h, -1, w, -1
    for y in range(h):
        alphas = data[y * bpl: y * bpl + w * 4][3::4]
        first = next((i for i, a in enumerate(alphas) if a > 8), -1)
        if first < 0:
            continue
        last = len(alphas) - 1 - next(
            (i for i, a in enumerate(reversed(alphas)) if a > 8), 0)
        if top == h:
            top = y
        bottom = y
        left = min(left, first)
        right = max(right, last)
    if right < left or bottom < top:
        return image
    pad = max(1, int(min(w, h) * 0.02))
    left, top = max(0, left - pad), max(0, top - pad)
    right, bottom = min(w - 1, right + pad), min(h - 1, bottom + pad)
    width, height = right - left + 1, bottom - top + 1
    # 几乎全透明或裁得过狠时按原图处理，避免把图标裁坏
    if width < w * 0.2 or height < h * 0.2:
        return image
    return image.copy(left, top, width, height)


def _sharpen(image: QImage, amount: float = 0.34) -> QImage:
    """对缩到显示尺寸的产品图做一次轻微锐化。

    云端产品图缩到 80px 左右后，机身缝线、面板按键这类 1px 细节会被
    双线性抹成灰边，看着就是「糊」。用 3×3 拉普拉斯提一点边，透明度
    作为遮罩，透明区域的 RGB 不参与，避免边缘出现黑边光晕。
    """
    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    if w < 8 or h < 8:
        return image
    arr = numpy.frombuffer(bytes(img.constBits()), dtype=numpy.uint8)
    arr = arr.reshape(h, img.bytesPerLine())[:, : w * 4].reshape(h, w, 4).astype(numpy.int16)
    rgb = arr[:, :, :3]
    lap = numpy.zeros_like(rgb)
    lap[1:-1, 1:-1] = (4 * rgb[1:-1, 1:-1] - rgb[:-2, 1:-1] - rgb[2:, 1:-1]
                       - rgb[1:-1, :-2] - rgb[1:-1, 2:])
    mask = (arr[:, :, 3:4] / 255.0)
    out = numpy.clip(rgb + lap * amount * mask, 0, 255).astype(numpy.uint8)
    result = numpy.ascontiguousarray(
        numpy.dstack([out, arr[:, :, 3].astype(numpy.uint8)]))
    return QImage(result.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def _icon_luminance(image: QImage) -> float:
    """产品图实物的平均亮度（透明像素不计），用于挑选图标底色。"""
    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return 255.0
    arr = numpy.frombuffer(bytes(img.constBits()), dtype=numpy.uint8)
    arr = arr.reshape(h, img.bytesPerLine())[:, : w * 4].reshape(h, w, 4)
    alpha = arr[:, :, 3] > 40
    if alpha.sum() < 20:
        return 255.0
    rgb = arr[:, :, :3][alpha].astype(numpy.float32)
    return float((rgb[:, 0] * 0.299 + rgb[:, 1] * 0.587 + rgb[:, 2] * 0.114).mean())


def _icon_tile(image: QImage) -> str:
    """按产品图明暗选底块颜色。

    路由器、小爱音箱这类黑色机身亮度只有 30~65，压在深灰底（#3c3f49，
    亮度约 62）上几乎看不出轮廓。深色主题下给它们换浅底，亮机身仍旧
    用深底，两种极端都能拉开对比。浅色主题卡片本来就是白的，统一用
    主题给的浅底即可。
    """
    if not is_dark():
        return SiColors.ICON_TILE
    return _DARK_PRODUCT_TILE if _icon_luminance(image) < 110 else SiColors.ICON_TILE


def _scaled_icon(path: Path, image: QImage, side: int, ratio: float) -> QPixmap:
    """把云端产品图渲染成 side 个物理像素的 pixmap。

    米家 CDN 只有 168×168 这一档（实测 @2x / !large / big 目录都不存在），
    裁掉透明边后实物也就 80~120px，缩到显示尺寸时单次双线性会把细线
    抹成灰边。这里逐级减半、每步最多缩一半，等效盒式预滤波，边缘和
    线条比一步缩到位干净；结果按（路径, mtime, 尺寸, DPR）缓存，切房间
    重建卡片时不再重复解码和裁剪。
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(path), mtime, side, round(ratio, 3))
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    scaled = _trim_transparent(image)
    while scaled.width() > side * 2 or scaled.height() > side * 2:
        scaled = scaled.scaled(
            max(side, scaled.width() // 2), max(side, scaled.height() // 2),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if scaled.width() != side or scaled.height() != side:
        scaled = scaled.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
    scaled = _sharpen(scaled)
    pixmap = QPixmap.fromImage(scaled)
    pixmap.setDevicePixelRatio(ratio)
    if len(_ICON_CACHE) >= _ICON_CACHE_LIMIT:
        _ICON_CACHE.clear()
    _ICON_CACHE[key] = pixmap
    return pixmap

# 主页内容使用更舒展的尺寸，不影响全局 DPI 或托盘卡片。
# 网格以此最小宽度计算列数，再均分可用宽度，避免右侧留下整块空白。
_CARD_FIXED_WIDTH = 212
_CARD_FIXED_HEIGHT = 92

# 离线卡片的灰置配色：背景更暗、文字更灰，hover 不提亮


class DeviceCard(SiRowCard):
    power_clicked = Signal(str)  # did
    open_requested = Signal(str)  # did
    tray_menu_requested = Signal(str, QPoint)
    reorder_requested = Signal(str, str)  # (dragged did, target did)

    _DRAG_MIME = "application/x-mijiadesk-card-did"
    _DRAG_THRESHOLD = 12

    def __init__(self, device: DeviceInfo, parent=None):
        super().__init__(parent, self.LeftToRight)
        self.device = device
        self._busy = False
        self._hovered = False
        self._online = device.online
        self._metrics_text = None
        self._panel_states = {}
        self._press_pos = None
        self._drag_active = False
        self.setAcceptDrops(True)

        self.style_data.background_color = QColor(
            SiColors.OFFLINE_CARD if not self._online else SiColors.CARD)
        self.style_data.border_radius = 14.0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # 初始基准尺寸；主窗口按实际网格宽度分配最终列宽。
        self.setFixedSize(_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT)
        # 任何在线设备都可点开：摄像头/灯具开专属窗口，其余走通用详情面板
        self.setCursor(Qt.PointingHandCursor if self._online else Qt.ArrowCursor)
        self.muteStretchWidget()

        self._power_btn = PowerButton(_POWER_BTN_SIZE, icon_size=22)
        self._power_btn.clicked.connect(lambda: self.power_clicked.emit(device.did))
        # 列表接口不带开关信息，先隐藏电源钮；后台探测确认设备
        # 有可写开关属性后再显示并回填真实状态
        self._power_btn.hide()
        self._power_btn.set_online(self._online)

        name_color = SiColors.INFO_TEXT if self._online else SiColors.OFFLINE_TEXT
        self._name_label = QLabel(device_title(device))
        self._name_label.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Medium))
        self._name_label.setAccessibleName(device.name)
        self._name_label.setMinimumWidth(0)
        self._name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # 允许换行：长名称不再把所在列撑宽，卡片宽度保持整齐
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet(
            f"color: {name_color}; background: transparent; font-size: 10.2pt; font-weight: 500;")

        self._sub_label = QLabel(self._sub_text())
        self._sub_label.setFont(QFont("Microsoft YaHei UI", 8))
        self._sub_label.setWordWrap(True)
        self._sub_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sub_color = SiColors.TEXT_SECONDARY if self._online else SiColors.OFFLINE_SUB
        self._sub_label.setStyleSheet(
            f"color: {sub_color}; background: transparent; font-size: 9pt;")
        # 离线卡片电源钮不可操作
        if not self._online:
            self._power_btn.setEnabled(False)

        # 设备图标：后台拉取就绪后由 set_icon 填入，未就绪前先画线条图标
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        model_icons = {
            "wifispeaker": "mdi.speaker",
            "light": "mdi.lightbulb-outline",
            "plug": "mdi.power-socket-eu",
            "sensor": "mdi.thermometer",
            "humidifier": "mdi.water-outline",
            "airpurifier": "mdi.air-filter",
            "curtain": "mdi.blinds",
            "camera": "mdi.cctv",
        }
        icon = next((value for key, value in model_icons.items() if key in device.model),
                    "mdi.devices")
        self._fallback_icon = qta.icon(icon, color=SiColors.TEXT_SECONDARY)
        self._has_real_icon = False
        self._icon_path = None
        self._draw_placeholder()
        # 产品图多是「深色机身 + 透明背景」，在深色卡片上几乎看不见；
        # 垫一块比卡片略亮的圆角底，白/黑机身都能看清轮廓。
        self._icon_label.setStyleSheet(
            f"background: {SiColors.ICON_TILE}; border-radius: {_ICON_RADIUS}px; border: none;")

        # 左侧文字列整体垂直居中，与右侧电源钮同一水平轴
        text_col = QWidget()
        text_col.setAttribute(Qt.WA_TranslucentBackground)
        text_lay = QVBoxLayout(text_col)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(5)
        text_lay.addStretch(1)
        text_lay.addWidget(self._name_label)
        text_lay.addWidget(self._sub_label)
        text_lay.addStretch(1)

        lay = self.layout()
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(self._icon_label, alignment=Qt.AlignVCenter)
        lay.addSpacing(7)
        lay.addWidget(text_col, stretch=1)
        lay.addWidget(self._power_btn, alignment=Qt.AlignVCenter)
        self._play_btn = None
        if is_camera(device):
            self._play_btn = CameraPlayButton(_POWER_BTN_SIZE, online=self._online)
            self._play_btn.clicked.connect(lambda: self.open_requested.emit(device.did))
            lay.addWidget(self._play_btn, alignment=Qt.AlignVCenter)
            from app.ui.tray.camera_corner_tag import CameraCornerTag
            self._camera_tag = CameraCornerTag(self, corner_radius=14, width=46, height=14, font_size=6)
            self._camera_tag.raise_()
        elif is_light(device) and has_device_details(device):
            from app.ui.light_corner_tag import LightCornerTag
            self._light_tag = LightCornerTag(self)
            self._light_tag.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802
        """A fine outline keeps the cards legible on the quiet page background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.style_data.background_color)
        border = SiColors.CARD_BORDER_HOVER if self._hovered and self._online else SiColors.LINE
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 14, 14)
        painter.end()

    # ---------- 状态渲染 ----------

    def set_icon(self, path: Path | None) -> None:
        """填入设备图标；路径无效或图片损坏时保留线条图标。"""
        if path is None or not path.is_file():
            return
        image = QImage(str(path))
        if image.isNull():
            return
        # 按屏幕 DPR 渲染：40 逻辑像素在 200% 缩放下要 80 个物理像素，
        # 只塞 40px 原图会被系统再放大一次，边缘就发虚了。
        ratio = float(self.devicePixelRatioF() or 1.0)
        side = max(1, int(round(_ICON_INNER * ratio)))
        pixmap = _scaled_icon(path, image, side, ratio)
        self._icon_path = path
        self._icon_label.setStyleSheet(
            f"background: {_icon_tile(image)}; border-radius: {_ICON_RADIUS}px; border: none;")
        self._icon_label.setPixmap(pixmap)
        self._has_real_icon = True
        self._icon_label.show()

    def _draw_placeholder(self) -> None:
        """产品图还没到位时的线条图标。

        qtawesome 的 pixmap(w, h) 收的是逻辑尺寸、返回的位图已按屏幕
        DPR 放大并自带 dpr；再手动乘一次 ratio 会让图标变成两倍大，
        在 46px 的槽里被裁掉一圈（表现为线条图标糊边、顶到圆角外）。
        """
        side = max(1, int(round(_ICON_INNER * 0.86)))
        self._icon_label.setPixmap(self._fallback_icon.pixmap(side, side))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        # 构造时还没上屏，devicePixelRatio 可能是 1；上屏后按真实缩放补画一次
        if not self._has_real_icon:
            self._draw_placeholder()

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().changeEvent(event)
        # 窗口拖到不同缩放比例的屏幕上时 DPR 会变，重画一次避免图标被系统拉伸
        if event.type() == QEvent.Type.DevicePixelRatioChange and self._icon_path:
            self.set_icon(self._icon_path)

    def set_power_state(self, state: bool) -> None:
        if not supports_card_power(self.device):
            self._power_btn.hide()
            return
        # 能设置状态即证明设备具备开关能力，电源钮随之可见
        self._power_btn.set_state(state)
        self._power_btn.show()

    def _sub_text(self, metrics: str | None = None) -> str:
        """副标题文案：离线优先显示离线标记，有读数时附带。"""
        return device_subtitle(self.device, metrics, self._panel_states)

    def set_metrics(self, text: str | None) -> None:
        """副标题附带实时读数（温湿度等），离线卡片不叠加读数。"""
        # 离线设备不展示温湿度，读数可能是旧缓存
        if not self._online:
            return
        self._metrics_text = text
        self._sub_label.setText(self._sub_text(text))

    def set_panel_states(self, states):
        if self.device.switch_channels:
            self._panel_states = dict(states)
            self._sub_label.setText(self._sub_text(self._metrics_text))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._power_btn.set_busy(busy)

    # ---------- hover 观感 ----------

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = True
        self._apply_card_color()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = False
        self._apply_card_color()
        super().leaveEvent(event)

    def _apply_card_color(self) -> None:
        if not self._online:
            # 离线卡片整体灰置，hover 不提亮（无交互暗示）
            self.style_data.background_color = QColor(SiColors.OFFLINE_CARD)
        else:
            self.style_data.background_color = QColor(
                SiColors.CARD_HOVER if self._hovered and self._online else SiColors.CARD
            )
        self.update()

    # ---------- 交互 ----------

    def contextMenuEvent(self, event):
        # 右键菜单对所有设备开放：里面始终有「设备功能 / 详情」入口
        self.tray_menu_requested.emit(self.device.did, event.globalPos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 电源钮自己消费点击事件，能走到这里的都是卡片空白区
        if event.button() == Qt.LeftButton and not self._drag_active and self._online:
            self.open_requested.emit(self.device.did)
        self._drag_active = False
        self._press_pos = None
        super().mouseReleaseEvent(event)

    # ---------- 卡片拖动重排 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._drag_active = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # 按住卡片空白区拖过阈值即发起重排拖拽；拖拽进行中 Qt 会接管
        # 后续事件，松开不再触发 mouseReleaseEvent 的打开详情逻辑
        if (self._press_pos is not None and event.buttons() & Qt.LeftButton
                and not self._drag_active):
            delta = event.position().toPoint() - self._press_pos
            if delta.manhattanLength() >= self._DRAG_THRESHOLD:
                self._drag_active = self._start_reorder_drag()
                self._press_pos = None
                if self._drag_active:
                    return
        super().mouseMoveEvent(event)

    def _start_reorder_drag(self) -> bool:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(self._DRAG_MIME, self.device.did.encode("utf-8"))
        drag.setMimeData(mime)
        # 半透明缩略图跟随光标：QPixmap 没有 setOpacity，必须经
        # QPainter 绘制到透明底板上实现；直接调用会抛 AttributeError，
        # 拖拽在起始处就崩溃（此前功能完全失效正是这个原因）
        pixmap = self.grab().scaledToWidth(int(self.width() * .9))
        ghost = QPixmap(pixmap.size())
        ghost.fill(Qt.transparent)
        painter = QPainter(ghost)
        painter.setOpacity(.72)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        drag.setPixmap(ghost)
        drag.setHotSpot(self._press_pos)
        result = drag.exec(Qt.DropAction.MoveAction)
        return bool(result)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.source() is not self and \
                event.mimeData().hasFormat(self._DRAG_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        source_did = bytes(event.mimeData().data(self._DRAG_MIME)).decode("utf-8", "ignore")
        if source_did and source_did != self.device.did:
            self.reorder_requested.emit(source_did, self.device.did)
        event.acceptProposedAction()


