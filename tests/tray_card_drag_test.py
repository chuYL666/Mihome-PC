# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""托盘快捷窗口内直接拖动卡片排序：手势、落盘、与旧点击行为共存。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from app.core import tray_store
from app.core.models import DeviceInfo
from app.ui.tray.quick_window import TrayQuickWindow
from tests.device_controls_test import Jobs


def _device(did: str, model: str = 'xiaomi.switch.test', *, channels=("child",),
            online: bool = True) -> DeviceInfo:
    return DeviceInfo(did, did, model, '家', '卧室', online,
                      switch_channels=list(channels))


def _mouse(kind, widget, global_pos: QPoint, button, buttons):
    ev = QMouseEvent(kind, QPointF(5, 5), QPointF(global_pos),
                     button, buttons, Qt.NoModifier)
    return QApplication.sendEvent(widget, ev)


class TrayCardDragTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, kwargs in (
            ('app.core._json_store.data_file', {'side_effect': lambda n: Path(self.temp.name) / n}),
            ('app.core._json_store.data_dir', {'return_value': Path(self.temp.name)}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.widgets = []

    def tearDown(self):
        for w in self.widgets:
            w.hide()
            w.deleteLater()
        self.app.processEvents()

    def _window(self, devices, *, known_power=None, columns=2, scenes=None,
                order=None):
        dids = [d.did for d in devices]
        tray_store.save(dids)
        tray_store.save_scenes(scenes or [])
        tray_store.save_order(order or [{'type': 'device', 'id': d} for d in dids])
        quick = TrayQuickWindow(Mock(), Jobs())
        self.widgets.append(quick)
        quick._columns = columns
        quick._devices = list(devices)
        quick._known_power = dict(known_power or {})
        quick._rebuild()
        quick.show()
        self.app.processEvents()
        return quick

    def _press(self, widget, pos=None):
        pos = pos or widget.mapToGlobal(QPoint(6, 6))
        _mouse(QEvent.MouseButtonPress, widget, pos,
               Qt.LeftButton, Qt.LeftButton)

    def _move(self, widget, pos):
        _mouse(QEvent.MouseMove, widget, pos, Qt.NoButton, Qt.LeftButton)

    def _release(self, widget, pos):
        _mouse(QEvent.MouseButtonRelease, widget, pos,
               Qt.LeftButton, Qt.NoButton)

    def _drag(self, quick, src_idx, dst_idx):
        grid = quick._grid
        src = grid.itemAt(src_idx).widget()
        dst = grid.itemAt(dst_idx).widget()
        start = src.mapToGlobal(QPoint(6, 6))
        # 用目标格子的中心换算成全局坐标，命中双列网格的线性索引
        center = dst.rect().center()
        target = dst.mapToGlobal(center)
        self._press(src, start)
        self._move(src, target)
        self._release(src, target)
        self.app.processEvents()

    def test_drag_reorders_grid_and_persists_mixed_order(self):
        scene = {'scene_id': 's1', 'name': '回家', 'home_id': 'h', 'home_name': '家'}
        devices = [_device('d1'), _device('d2'), _device('d3')]
        order = [{'type': 'device', 'id': 'd1'}, {'type': 'scene', 'id': 's1'},
                 {'type': 'device', 'id': 'd2'}, {'type': 'device', 'id': 'd3'}]
        quick = self._window(devices, scenes=[scene], order=order)
        self.assertEqual(quick._grid.count(), 4)

        self._drag(quick, 0, 2)
        keys = []
        for i in range(quick._grid.count()):
            w = quick._grid.itemAt(i).widget()
            keys.append(('device', w._did) if getattr(w, '_did', None)
                        else ('scene', w._scene_id))
        expected = [('scene', 's1'), ('device', 'd2'),
                    ('device', 'd1'), ('device', 'd3')]
        self.assertEqual(keys, expected)
        self.assertEqual([(it['type'], it['id']) for it in tray_store.load_order()],
                         expected)
        self.assertEqual(tray_store.load(), ['d2', 'd1', 'd3'])
        self.assertEqual([s['scene_id'] for s in tray_store.load_scenes()], ['s1'])
        self.assertFalse(quick._card_drag.active)

    def test_drag_does_not_drop_members_that_are_not_rendered(self):
        # d5 在托盘名单里但当前设备列表没有（离线/尚未同步），排序不能把它删掉
        devices = [_device('d1'), _device('d2'), _device('d3'), _device('d4')]
        quick = self._window(devices)
        tray_store.save(['d1', 'd2', 'd3', 'd4', 'd5'])
        tray_store.save_order([{'type': 'device', 'id': d}
                               for d in ['d1', 'd2', 'd3', 'd4', 'd5']])
        quick._tray_dids = tray_store.load()
        quick._rebuild()
        self.app.processEvents()
        self.assertEqual(quick._grid.count(), 4)

        self._drag(quick, 0, 2)
        self.assertEqual(tray_store.load(), ['d2', 'd3', 'd1', 'd4', 'd5'])
        self.assertEqual([it['id'] for it in tray_store.load_order()],
                         ['d2', 'd3', 'd1', 'd4', 'd5'])

    def test_single_column_drag(self):
        devices = [_device('d1'), _device('d2'), _device('d3'), _device('d4')]
        quick = self._window(devices, columns=1)
        self._drag(quick, 0, 2)
        self.assertEqual([it['id'] for it in tray_store.load_order()],
                         ['d2', 'd3', 'd1', 'd4'])

    def test_rebuild_is_deferred_until_gesture_finishes(self):
        devices = [_device('d1'), _device('d2'), _device('d3'), _device('d4')]
        quick = self._window(devices)
        src = quick._grid.itemAt(0).widget()
        dst = quick._grid.itemAt(2).widget()
        start = src.mapToGlobal(QPoint(6, 6))
        target = dst.mapToGlobal(dst.rect().center())
        self._press(src, start)
        self._move(src, target)
        self.assertTrue(quick._card_drag.active)
        grid_before = quick._grid
        row_before = quick._grid.itemAt(0).widget()
        quick._rebuild()
        self.assertTrue(quick._card_rebuild_pending)
        self.assertIs(quick._grid, grid_before)
        self.assertIs(quick._grid.itemAt(0).widget(), row_before)
        self._release(src, target)
        self.app.processEvents()
        self.assertFalse(quick._card_rebuild_pending)
        self.assertEqual([it['id'] for it in tray_store.load_order()],
                         ['d2', 'd3', 'd1', 'd4'])

    def test_plain_click_still_opens_device_and_buttons_keep_their_click(self):
        camera = _device('cam', 'chuangmi.camera.029a02', channels=())
        washer = DeviceInfo('w', '洗衣机', 'xiaomi.washer.test', '家', '阳台', True)
        quick = self._window([camera, washer], known_power={'w': True})
        opened = Mock()
        quick.open_device_requested.connect(opened)
        cam_row = next(quick._grid.itemAt(i).widget() for i in range(quick._grid.count())
                       if getattr(quick._grid.itemAt(i).widget(), '_did', None) == 'cam')

        # 普通点击：不移动，松开后仍打开设备详情
        self._press(cam_row)
        self._release(cam_row, cam_row.mapToGlobal(QPoint(6, 6)))
        opened.assert_called_once_with('cam')

        # 电源按钮区域按下并拖走：不启动排序，也不误开详情
        washer_row = next(quick._grid.itemAt(i).widget() for i in range(quick._grid.count())
                          if getattr(quick._grid.itemAt(i).widget(), '_did', None) == 'w')
        btn = washer_row._power_btn
        self.assertIsNotNone(btn)
        before = [it['id'] for it in tray_store.load_order()]
        p = btn.mapToGlobal(QPoint(6, 6))
        self._press(btn, p)
        self._move(btn, p + QPoint(0, 120))
        self.assertFalse(quick._card_drag.active)
        self._release(btn, p + QPoint(0, 120))
        self.assertEqual([it['id'] for it in tray_store.load_order()], before)
        self.assertEqual(opened.call_count, 1)


if __name__ == '__main__':
    unittest.main()
