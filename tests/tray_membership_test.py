"""Main/tray context menus and editor use one membership and ordering store."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QWidget
from app.core import tray_store
from app.core.models import DeviceInfo
from app.ui.device_card import DeviceCard
from app.ui.main_window import MainWindow
from app.ui.tray.manager_dialog import TrayManagerDialog
from app.ui.tray.quick_window import TrayQuickWindow
from tests.device_controls_test import Jobs
from tools.test_light_controls import VirtualLightService


class TrayMembershipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, kwargs in [('data_file', {'side_effect': lambda n: Path(self.temp.name) / n}),
                             ('data_dir', {'return_value': Path(self.temp.name)})]:
            p = patch('app.core._json_store.' + name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.widgets = []

    def tearDown(self):
        for w in self.widgets:
            w.hide()
            w.deleteLater()
        self.app.processEvents()

    def test_membership_changes_preserve_scene_snapshots_and_mixed_order(self):
        scene = {'scene_id': 'same', 'name': 'Scene', 'home_id': 'home', 'home_name': 'Home'}
        tray_store.save(['same', 'other'])
        tray_store.save_scenes([scene])
        order = [{'type': 'scene', 'id': 'same'}, {'type': 'device', 'id': 'same'},
                 {'type': 'device', 'id': 'other'}]
        tray_store.save_order(order)
        tray_store.set_device_selected('same', True)
        self.assertEqual(tray_store.load_order(), order)
        tray_store.remove_item('device', 'same')
        self.assertEqual(tray_store.load_order(), [order[0], order[2]])
        self.assertEqual(tray_store.load_scenes(), [scene])
        tray_store.set_device_selected('same', True)
        self.assertEqual(tray_store.load_order()[-1], {'type': 'device', 'id': 'same'})
        tray_store.remove_item('scene', 'same')
        self.assertIn('same', tray_store.load())
        self.assertEqual(tray_store.load_scenes(), [])

    def test_right_click_add_then_tray_remove_updates_editor_and_visible_rows(self):
        service, jobs = VirtualLightService(), Jobs()
        light = service.device
        router = DeviceInfo('router', '路由器', 'xiaomi.router.test', '', '', True)
        window = QWidget()
        self.widgets.append(window)
        window._all_devices = [light, router]
        for name in ('_style_popup_menu', '_build_device_tray_menu', '_set_device_tray_membership', '_remove_tray_item'):
            setattr(window, name, getattr(MainWindow, name).__get__(window))
        quick = TrayQuickWindow(service, jobs)
        self.widgets.append(quick)
        quick.show()
        window._update_tray_devices = Mock(side_effect=lambda: quick.set_devices(window._all_devices, {}))
        quick.remove_tray_item_requested.connect(window._remove_tray_item)
        self.assertIsNone(window._build_device_tray_menu(router.did))
        with patch('app.ui.main_window.Toast.info'):
            menu = window._build_device_tray_menu(light.did)
            self.widgets.append(menu)
            self.assertEqual(menu.actions()[0].text(), '添加到托盘窗口')
            menu.actions()[0].trigger()
        self.assertTrue(tray_store.contains(light.did))
        self.assertEqual(quick._grid.itemAt(0).widget()._did, light.did)
        editor = TrayManagerDialog(window._all_devices, scenes=[])
        self.widgets.append(editor)
        self.assertEqual(editor._checks[light.did].text(), '✓')
        menu = window._build_device_tray_menu(light.did)
        self.widgets.append(menu)
        self.assertEqual(menu.actions()[0].text(), '从托盘窗口移除')
        remove = quick._build_remove_tray_menu('device', light.did)
        self.widgets.append(remove)
        remove.actions()[0].trigger()
        self.assertFalse(tray_store.contains(light.did))
        menu = window._build_device_tray_menu(light.did)
        self.widgets.append(menu)
        self.assertEqual(menu.actions()[0].text(), '添加到托盘窗口')
        editor = TrayManagerDialog(window._all_devices, scenes=[])
        self.widgets.append(editor)
        self.assertEqual(editor._checks[light.did].text(), '+')
        self.assertNotIn('router', editor._checks)
        window._update_tray_devices.assert_called()

    def test_card_right_click_requests_menu_without_opening_or_switching_device(self):
        card = DeviceCard(VirtualLightService().device)
        self.widgets.append(card)
        menu, opened, powered = Mock(), Mock(), Mock()
        card.tray_menu_requested.connect(menu)
        card.open_requested.connect(opened)
        card.power_clicked.connect(powered)
        event = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(10, 10), QPoint(100, 100))
        QApplication.sendEvent(card, event)
        menu.assert_called_once()
        opened.assert_not_called()
        powered.assert_not_called()


if __name__ == '__main__':
    unittest.main()
