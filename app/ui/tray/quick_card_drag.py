# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""托盘快捷窗口内直接拖拽卡片排序。

管理对话框里已有拖拽排序，但用户更常停留在托盘浮窗。这里用轻量的
「按下—移动—松开」手势：按住卡片空白处拖动，网格实时让位，松开后把
设备/场景混排顺序写回 tray_store。电源开关、摄像头播放、场景执行按钮
保持原点击行为，不启动拖动。

不用 QDrag：半透明 Tool 窗口同时背着「整个浮窗可拖动」和「鼠标离开自动
隐藏」两套鼠标逻辑，原生拖放容易与它们抢事件，也做不出实时让位效果。
"""

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

from app.core import tray_store
from app.ui.si_theme import SiColors


class QuickCardDragController(QObject):
    """绑定到快捷窗口的每张卡片（及其子控件），把左键手势翻译成排序。"""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        # widget -> (所属卡片, (type, id))；含卡片自身与全部子控件
        self._targets: dict[QWidget, tuple[QWidget, tuple[str, str]]] = {}
        # 交互控件及其子控件：在这些区域按下时放行，不启动拖动
        self._interactive: set[QWidget] = set()
        self._rows: list[QWidget] = []
        self._keys: list[tuple[str, str]] = []
        self._origin = QPoint(0, 0)
        self._cell_w = 0
        self._cell_h = 0
        self._cur = -1
        self._src = -1
        self._press_global: QPoint | None = None
        self._press_row: QWidget | None = None
        self._active = False
        self._orig_style = ''

    @property
    def active(self) -> bool:
        return self._active

    # ---------- 绑定 / 解绑 ----------

    def bind(self, row, key: tuple[str, str]) -> None:
        """在 _rebuild 中对每张已构造完的卡片调用一次。"""
        for widget in [row, *row.findChildren(QWidget)]:
            self._targets[widget] = (row, key)
            widget.installEventFilter(self)
        for button in row.findChildren(QAbstractButton):
            self._interactive.add(button)
            for child in button.findChildren(QWidget):
                self._interactive.add(child)

    def reset(self) -> None:
        """_destroy_grid 开头调用：先解绑，再让控件安全销毁。"""
        for widget in list(self._targets):
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        self._targets.clear()
        self._interactive.clear()
        self.cancel()

    def cancel(self) -> None:
        """丢弃进行中的手势（窗口隐藏、重建时调用），不写盘。"""
        if self._active and self._press_row is not None:
            try:
                self._press_row.setStyleSheet(self._orig_style)
            except RuntimeError:
                pass
        self._rows = []
        self._keys = []
        self._cur = self._src = -1
        self._press_global = None
        self._press_row = None
        self._active = False
        self._orig_style = ''

    # ---------- 鼠标手势 ----------

    def eventFilter(self, obj, event):  # noqa: N802
        info = self._targets.get(obj)
        if info is None:
            return False
        kind = event.type()
        if kind == QEvent.MouseButtonPress:
            if event.button() != Qt.LeftButton or obj in self._interactive:
                return False
            self._press_global = event.globalPosition().toPoint()
            self._press_row = info[0]
            # 吃掉 press：默认 QWidget 会 ignore 并把事件冒泡给父级，
            # 而托盘浮窗在父级上装了「拖动整个窗口」的过滤器
            return True
        if kind == QEvent.MouseMove:
            if self._press_global is None:
                return False
            pos = event.globalPosition().toPoint()
            if not self._active:
                if (pos - self._press_global).manhattanLength() < QApplication.startDragDistance():
                    return True
                self._begin()
            if self._active:
                self._move_to(pos)
            return True
        if kind == QEvent.MouseButtonRelease:
            if event.button() != Qt.LeftButton or self._press_global is None:
                return False
            was_active = self._active
            pos = event.globalPosition().toPoint()
            row = self._press_row
            self._finish()
            if not was_active and row is not None:
                self._maybe_open(row, pos)
            return True
        return False

    def _begin(self) -> None:
        grid = getattr(self.win, '_grid', None)
        if grid is None or self._press_row is None:
            return
        rows: list[QWidget] = []
        keys: list[tuple[str, str]] = []
        for i in range(grid.count()):
            widget = grid.itemAt(i).widget()
            if widget is None:
                continue
            info = self._targets.get(widget)
            if info is None:
                continue
            rows.append(widget)
            keys.append(info[1])
        if len(rows) < 2:
            return
        self._rows, self._keys = rows, keys
        self._src = self._cur = rows.index(self._press_row)
        first = rows[0].geometry()
        self._origin = QPoint(first.x(), first.y())
        self._cell_w = first.width() + grid.horizontalSpacing()
        self._cell_h = first.height() + grid.verticalSpacing()
        self._active = True
        self.win._leave_timer.stop()
        self._orig_style = self._press_row.styleSheet()
        self._press_row.setStyleSheet(
            "QFrame#trayRow { background: %s; border: 1px solid %s; border-radius: 9px; }"
            % (SiColors.TRAY_CARD_HOVER, SiColors.THEME))

    def _move_to(self, global_pos: QPoint) -> None:
        if self._cell_w <= 0 or self._cell_h <= 0:
            return
        n = len(self._rows)
        cols = max(1, getattr(self.win, '_columns', 2))
        pos = self.win._host.mapFromGlobal(global_pos)
        r = (pos.y() - self._origin.y()) // self._cell_h
        c = (pos.x() - self._origin.x()) // self._cell_w
        r = max(0, min((n - 1) // cols, r))
        c = max(0, min(cols - 1, c))
        target = min(r * cols + c, n - 1)
        if target != self._cur:
            self._reorder(target)

    def _reorder(self, target: int) -> None:
        rows, keys = list(self._rows), list(self._keys)
        widget, key = rows.pop(self._cur), keys.pop(self._cur)
        rows.insert(target, widget)
        keys.insert(target, key)
        self._rows, self._keys = rows, keys
        self._cur = target
        grid = self.win._grid
        cols = max(1, self.win._columns)
        for item in rows:
            grid.removeWidget(item)
        for i, item in enumerate(rows):
            grid.addWidget(item, i // cols, i % cols)
            item.show()
        grid.activate()
        self.win._host.update()

    # ---------- 落盘 ----------

    def _finish(self) -> None:
        changed = self._active and self._cur != self._src
        if changed:
            self._commit()
        self.cancel()
        # 后台 5s 轮询可能在拖动期间请求过重建；异步执行，避免在本次
        # release 事件还没处理完时销毁正在派发事件的控件
        if getattr(self.win, '_card_rebuild_pending', False):
            self.win._card_rebuild_pending = False
            QTimer.singleShot(0, self.win._rebuild)
        if self.win.underMouse():
            self.win._leave_timer.stop()

    def _commit(self) -> None:
        """只改顺序，绝不删成员：当前未显示的卡片（离线/未同步）保持原样。"""
        keys = list(self._keys)
        seen = set(keys)
        order = [{'type': t, 'id': i} for t, i in keys]
        for item in tray_store.load_order():
            key = (item['type'], item['id'])
            if key not in seen:
                order.append({'type': item['type'], 'id': item['id']})
                seen.add(key)
        tray_store.save_order(order)

        old_dids = tray_store.load()
        dids = [i for t, i in keys if t == 'device']
        used = set(dids)
        tray_store.save(dids + [d for d in old_dids if d not in used])

        scenes = tray_store.load_scenes()
        by_id = {s['scene_id']: s for s in scenes}
        scene_ids = {i for t, i in keys if t == 'scene'}
        ordered_scenes = [by_id[i] for i in (i for t, i in keys if t == 'scene')
                          if i in by_id]
        ordered_scenes += [s for s in scenes if s['scene_id'] not in scene_ids]
        tray_store.save_scenes(ordered_scenes)

        self.win._tray_order = order
        self.win._tray_dids = tray_store.load()
        self.win._tray_scenes = tray_store.load_scenes()

    def _maybe_open(self, row, global_pos: QPoint) -> None:
        """没拖动 = 普通点击：沿用原先「点卡片打开设备详情」的行为。"""
        did = getattr(row, '_open_did', None)
        if not did or not row.rect().contains(row.mapFromGlobal(global_pos)):
            return
        self.win.open_device_requested.emit(did)
