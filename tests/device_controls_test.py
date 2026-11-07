# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only parent panels, direct light switches and the small light popup."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QPushButton
from app.core import tray_store
from app.core.device_controls import (device_title, device_subtitle, has_device_details,
    light_controls, prepare_device_cards)
from app.core.models import DeviceInfo, PropInfo
from app.core.service import ServiceError
from app.ui.device_card import DeviceCard
from app.ui.device_dialog import DeviceDetailDialog
from app.ui.light_controls import LightControlDialog
from app.ui.main_window import MainWindow
from app.ui.tray.quick_window import TrayQuickWindow
from tools.test_light_controls import VirtualLightService, DEMO_DID


class Jobs:
    def __init__(self):
        self.pending = []

    def submit(self, fn, on_success=None, on_error=None, *, priority=10):
        self.pending.append((fn, on_success, on_error))

    def finish(self):
        fn, success, failure = self.pending.pop(0)
        try:
            result = fn()
        except Exception as error:
            if failure:
                failure(error)
        else:
            if success:
                success(result)


class DeviceControlsTest(unittest.TestCase):
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
        self.service = VirtualLightService()
        self.jobs = Jobs()

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, LightControlDialog):
                widget.reject()
            else:
                widget.hide()
            widget.deleteLater()
        self.app.processEvents()

    def panel_devices(self):
        parent = DeviceInfo('123', '吧台', 'xiaomi.switch.w3', 'Home', '吧台', True)
        children = [DeviceInfo(f'123.s{s}', f'灯{s}', parent.model, 'Home', '吧台', True)
                    for s in (2, 3, 4)]
        return prepare_device_cards([parent, *children])

    def popup(self):
        dialog = LightControlDialog(self.service, self.jobs, self.service.device)
        self.widgets.append(dialog)
        dialog.show()
        dialog.load()
        self.jobs.finish()  # supported controls
        self.jobs.finish()  # initial values
        return dialog

    def test_light_dialog_remains_centered_after_async_controls_load(self):
        parent = QWidget()
        parent.setGeometry(self.app.primaryScreen().availableGeometry().adjusted(10, 10, -10, -10))
        parent.show()
        self.widgets.append(parent)
        dialog = LightControlDialog(self.service, self.jobs, self.service.device, parent)
        self.widgets.append(dialog)
        dialog.show_centered()
        dialog.load()
        self.jobs.finish()  # Dynamic sliders change the dialog height.
        self.app.processEvents()
        self.assertEqual(dialog.windowModality(), Qt.WindowModal)
        area = self.app.primaryScreen().availableGeometry()
        point = parent.frameGeometry().center() - dialog.rect().center()
        expected = QPoint(max(area.left(), min(point.x(), area.right() + 1 - dialog.width())),
                          max(area.top(), min(point.y(), area.bottom() + 1 - dialog.height())))
        self.assertLessEqual((dialog.pos() - expected).manhattanLength(), 3)
        self.assertLess(dialog._power.x(), dialog._title.x())
        self.jobs.finish()
        self.assertEqual(set(dialog._props), {'brightness', 'temperature', 'color'})

    def test_tray_light_page_returns_without_moving_window_and_stops_on_signout(self):
        quick = TrayQuickWindow(self.service, self.jobs)
        self.widgets.append(quick)
        quick.show()
        self.app.processEvents()
        original_geometry = quick.geometry()
        quick.show_light_controls(self.service.device)
        pane = quick._light_control
        self.assertIs(quick._page_stack.currentWidget(), pane)
        self.assertFalse(pane.isWindow())
        quick._hide_after_pointer_leave()
        self.assertTrue(quick.isVisible())
        self.jobs.finish()
        self.jobs.finish()
        self.app.processEvents()
        self.assertEqual(quick.geometry(), original_geometry)
        self.assertLess(pane._power.x(), pane._title.x())
        pane._sliders['brightness'].setValue(75)
        pane._sliders['brightness'].committed.emit()
        self.jobs.finish()
        self.assertEqual(self.service.values['brightness'], 75)
        QTest.mouseClick(pane._close_button, Qt.LeftButton)
        self.assertIsNone(quick._light_control)
        self.assertTrue(quick._overview_page.isEnabled())
        self.assertIsNone(quick._panel.graphicsEffect())
        self.assertIs(quick._page_stack.currentWidget(), quick._overview_page)
        self.assertEqual(quick.geometry(), original_geometry)
        quick.show_light_controls(self.service.device)
        pending = quick._light_control
        quick.set_access_state('signed_out')
        self.assertIsNone(quick._light_control)
        self.assertTrue(pending._closed)

    def test_adjustable_lights_have_led_tag_on_main_and_tray_cards(self):
        from app.ui.light_corner_tag import LightCornerTag
        from dataclasses import replace
        for features in ([], ['brightness'], ['temperature'], ['color']):
            light = replace(self.service.device, light_features=features)
            card = DeviceCard(light)
            self.widgets.append(card)
            quick = TrayQuickWindow(self.service, self.jobs)
            self.widgets.append(quick)
            row = quick._make_row(light)
            self.widgets.append(row)
            self.assertEqual(card.findChild(LightCornerTag) is not None, bool(features))
            self.assertEqual(row.findChild(LightCornerTag) is not None, bool(features))

    def test_panel_card_has_no_power_or_details_even_with_old_cached_state(self):
        parent, *_ = self.panel_devices()
        card = DeviceCard(parent)
        self.widgets.append(card)
        card.set_power_state(True)
        card.set_metrics('24°C')
        card.set_panel_states({'123.s2': True, '123.s3': False, '123.s4': False})
        self.assertEqual(device_title(parent), '吧台面板')
        self.assertIn('设备温度 24°C', card._sub_label.text())
        self.assertIn('部分开启 · 1 路', card._sub_label.text())
        self.assertTrue(card._power_btn.isHidden())
        opened = Mock()
        card.open_requested.connect(opened)
        card.show()
        QTest.mouseClick(card, Qt.LeftButton, pos=QPoint(10, 10))
        opened.assert_not_called()

    def test_panel_unknown_temperature_is_omitted_and_unknown_channel_is_not_off(self):
        parent, *_ = self.panel_devices()
        text = device_subtitle(parent, None, {'123.s2': True, '123.s3': False})
        self.assertNotIn('温度', text)
        self.assertIn('状态待确认', text)

    def test_split_switch_and_plain_light_cannot_open_generic_editor(self):
        devices = self.panel_devices()
        devices.append(DeviceInfo('lamp', '普通灯', 'test.light.basic', 'H', 'R', True))
        for device in devices:
            self.assertFalse(has_device_details(device))
            with self.assertRaises(ValueError):
                DeviceDetailDialog(self.service, self.jobs, device)
            window = SimpleNamespace(_all_devices=[device])
            MainWindow._on_open_device(window, device.did)  # no dialog or cloud call

    def test_parent_can_be_added_to_tray_as_display_only(self):
        devices = self.panel_devices()
        tray_store.save(['123'])
        quick = TrayQuickWindow(self.service, self.jobs)
        self.widgets.append(quick)
        quick.set_devices(devices, {'123': True, '123.s2': True, '123.s3': False, '123.s4': False})
        quick.set_metrics({'123': '24°C'})
        quick._rebuild()
        row = quick._grid.itemAt(0).widget()
        self.assertIsNone(row._power_btn)
        self.assertEqual(row.cursor().shape(), Qt.ArrowCursor)
        self.assertIn('设备温度 24°C', quick._sub_labels['123'].text())
        quick._apply_power({'123.s3': True})
        self.assertIn('部分开启 · 2 路', quick._sub_labels['123'].text())

    def test_popup_only_has_everyday_controls_and_no_function_editor(self):
        dialog = self.popup()
        self.assertEqual(set(dialog._props), {'brightness', 'temperature', 'color'})
        self.assertEqual(dialog._sliders['brightness'].value(), 65)
        self.assertIn('4000', dialog._labels['temperature'].text())
        self.assertTrue(dialog._power.state())
        self.assertFalse(hasattr(dialog, '_drawer'))

    def test_sliders_preview_then_write_on_release_and_rgb_stays_in_memory(self):
        dialog = self.popup()
        slider = dialog._sliders['brightness']
        slider.setValue(80)
        self.assertEqual(self.service.values['brightness'], 65)
        slider.committed.emit()
        self.jobs.finish()
        self.assertEqual(self.service.values['brightness'], 80)
        self.jobs.finish()  # Read back the device's actual post-write state.
        with patch('app.ui.light_controls.QColorDialog.getColor', return_value=QColor('#3366ff')):
            dialog._choose_color()
        self.jobs.finish()
        self.assertEqual(self.service.values['color'], 0x3366ff)
        self.jobs.finish()
        dialog._power.click()
        self.jobs.finish()
        self.assertFalse(self.service.values['on'])
        self.jobs.finish()
        self.assertFalse(slider.isEnabled())
        slider.setValue(55)
        slider.committed.emit()
        self.assertEqual(len(self.jobs.pending), 0)
        self.assertEqual(self.service.values['brightness'], 80)
        self.assertFalse(self.service.values['on'])  # No implicit power-on command.

    def test_adjustments_require_confirmed_power_on_and_color_picker_is_guarded(self):
        self.service.values['on'] = False
        dialog = self.popup()
        for state in (False, None):
            dialog._values['on'] = state
            dialog._render()
            self.assertFalse(dialog._color_button.isEnabled())
            for slider in dialog._sliders.values():
                self.assertFalse(slider.isEnabled())
            with patch('app.ui.light_controls.QColorDialog.getColor') as picker:
                dialog._choose_color()
                picker.assert_not_called()
            for name, value in (('brightness', 45), ('color-temperature', 3000), ('color', 0x3366ff)):
                dialog._write(name, value)
            self.assertEqual(len(self.jobs.pending), 0)
        dialog._values['on'] = False
        dialog._render()
        dialog._power.click()
        self.jobs.finish()
        self.jobs.finish()
        self.assertTrue(dialog._color_button.isEnabled())
        for slider in dialog._sliders.values():
            self.assertTrue(slider.isEnabled())
        slider = dialog._sliders['brightness']
        slider.setValue(45)
        slider.committed.emit()
        self.jobs.finish()
        self.jobs.finish()
        self.assertEqual(self.service.values['brightness'], 45)
        self.assertEqual(dialog._power_text.text(), '已开启')

    def test_tray_controls_hide_old_page_and_require_power_before_slider_clicks(self):
        self.service.values['on'] = False
        quick = TrayQuickWindow(self.service, self.jobs)
        self.widgets.append(quick)
        quick.show()
        quick.show_light_controls(self.service.device)
        self.jobs.finish()
        self.jobs.finish()
        self.app.processEvents()
        pane = quick._light_control
        self.assertTrue(quick._overview_page.isHidden())
        self.assertFalse(quick._overview_page.isEnabled())
        self.assertFalse(quick._overview_page.updatesEnabled())
        self.assertIsNone(quick._overview_page.parentWidget())
        self.assertFalse(pane._backdrop.isNull())
        self.assertTrue(pane.testAttribute(Qt.WA_OpaquePaintEvent))
        self.assertEqual(pane.geometry(), quick._panel.contentsRect())
        self.assertIs(quick._panel.childAt(QPoint(100, 20)), pane)
        before = pane.grab().toImage()
        # A normal background device update rebuilds the actual card grid.
        tray_store.set_device_selected(self.service.device.did, True)
        quick.set_devices([self.service.device], {self.service.device.did: False})
        self.app.processEvents()
        self.assertTrue(quick._overview_page.isHidden())
        self.assertFalse(quick._root.isVisible())
        self.assertTrue(all(not w.isVisible() for w in quick._overview_page.findChildren(QWidget)))
        self.assertEqual(pane.grab().toImage(), before)
        quick._cols_btn.setDown(True)
        quick._cols_btn.update()
        quick._audio_bar._btn_play.update()
        self.app.processEvents()
        self.assertEqual(pane.grab().toImage(), before)
        for widget in (quick._cols_btn, quick._audio_bar._btn_play):
            QTest.mouseMove(pane, widget.mapTo(pane, widget.rect().center()))
            self.app.processEvents()
            self.assertFalse(widget.underMouse())
            self.assertEqual(pane.grab().toImage(), before)
        self.assertFalse(pane.isWindow())
        self.assertNotIn('诊断报告', [b.text() for b in pane.findChildren(QPushButton)])
        self.assertEqual([b.text() for b in pane.findChildren(QPushButton) if b.text() == '返回'], [])
        slider = pane._sliders['brightness']
        self.assertFalse(slider.isEnabled())
        QTest.mouseClick(slider, Qt.LeftButton, pos=QPoint(slider.width() - 2, slider.height() // 2))
        self.assertEqual(len(self.jobs.pending), 0)
        self.assertEqual(self.service.values['brightness'], 65)
        pane._power.click()
        self.jobs.finish()
        self.jobs.finish()
        self.assertTrue(slider.isEnabled())
        target = slider.mapTo(quick._panel, slider.rect().center())
        self.assertIs(quick._panel.childAt(target), slider)
        QTest.mouseClick(slider, Qt.LeftButton, pos=QPoint(slider.width() - 2, slider.height() // 2))
        self.jobs.finish()
        self.assertNotEqual(self.service.values['brightness'], 65)
        original = quick.pos()
        start = pane._title.mapToGlobal(pane._title.rect().center())
        screen = SimpleNamespace(name=lambda: 'test-screen', availableGeometry=lambda: QRect(0, 0, 2000, 1200))
        with patch.object(QGuiApplication, 'screenAt', return_value=screen), \
             patch.object(QGuiApplication, 'screens', return_value=[screen]):
            for kind, point, button, buttons in [
                (QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton),
                (QEvent.MouseMove, start + QPoint(50, 40), Qt.NoButton, Qt.LeftButton),
                (QEvent.MouseButtonRelease, start + QPoint(50, 40), Qt.LeftButton, Qt.NoButton)]:
                event = QMouseEvent(kind, QPointF(pane._title.mapFromGlobal(point)), QPointF(point), button, buttons, Qt.NoModifier)
                QApplication.sendEvent(pane._title, event)
        self.assertEqual(quick.pos(), original + QPoint(50, 40))

    def test_main_light_dialog_does_not_resize_or_move_after_status_and_poll_updates(self):
        dialog = LightControlDialog(self.service, self.jobs, self.service.device)
        self.widgets.append(dialog)
        dialog.show_centered()
        dialog.load()
        self.jobs.finish()  # Controls are ready, first value read is still queued.
        self.app.processEvents()
        initial = dialog.geometry()
        self.jobs.finish()
        self.app.processEvents()
        self.assertEqual(dialog.geometry(), initial)
        for value in (94, 100, 55):
            self.service.values['brightness'] = value
            dialog.refresh()
            self.jobs.finish()
            self.app.processEvents()
            self.assertEqual(dialog.geometry(), initial)

    def test_overlay_isolated_with_real_theme_audio_and_rebuilt_cards(self):
        from app.ui.si_theme import build_qss, current_theme, set_theme
        previous_theme, previous_qss = current_theme(), self.app.styleSheet()
        speaker = DeviceInfo('local-speaker', '测试音箱', 'xiaomi.wifispeaker.test',
                             '本地测试', '测试房间', True)
        devices = [self.service.device, speaker]
        tray_store.set_device_selected(self.service.device.did, True)
        try:
            for theme in ('light', 'dark'):
                set_theme(theme)
                self.app.setStyleSheet(build_qss())
                quick = TrayQuickWindow(self.service, self.jobs)
                self.widgets.append(quick)
                quick.set_devices(devices, {self.service.device.did: False})
                quick.show()
                self.app.processEvents()
                while self.jobs.pending:
                    self.jobs.finish()
                old_play = quick._audio_bar._btn_play
                old_point = old_play.mapTo(quick._overview_page, old_play.rect().center())
                self.assertTrue(old_play.isVisible())
                quick.show_light_controls(self.service.device)
                while self.jobs.pending:
                    self.jobs.finish()
                self.app.processEvents()
                pane = quick._light_control
                before = pane.grab().toImage()
                for _ in range(3):
                    quick.set_devices(devices, {self.service.device.did: False})
                    QTest.mouseMove(pane, old_point)
                    old_play.update()
                    self.app.processEvents()
                    self.assertFalse(old_play.isVisible())
                    self.assertFalse(old_play.underMouse())
                    self.assertEqual(pane.grab().toImage(), before)
                pane.reject()
                self.app.processEvents()
                self.assertTrue(old_play.isVisible())
                self.assertTrue(quick._overview_page.updatesEnabled())
                self.assertIs(quick._overview_page.parentWidget(), quick._panel)
                quick.hide()
        finally:
            set_theme(previous_theme)
            self.app.setStyleSheet(previous_qss)

    def test_repeated_light_close_clears_scrim_from_entire_tray_surface(self):
        from app.ui.si_theme import SiColors, build_qss, current_theme, set_theme
        previous_theme, previous_qss = current_theme(), self.app.styleSheet()
        try:
            for theme in ('light', 'dark'):
                set_theme(theme)
                self.app.setStyleSheet(build_qss())
                quick = TrayQuickWindow(self.service, self.jobs)
                self.widgets.append(quick)
                quick.show()
                self.app.processEvents()
                for _ in range(4):
                    quick.show_light_controls(self.service.device)
                    while self.jobs.pending:
                        self.jobs.finish()
                    self.app.processEvents()
                    pane = quick._light_control
                    QTest.mouseClick(pane._close_button, Qt.LeftButton)
                    self.app.processEvents()
                    self.assertIsNone(quick._light_control)
                    self.assertTrue(pane.isHidden())
                    self.assertIsNone(quick._panel.graphicsEffect())
                    # Sample gaps outside the child widgets: these were left
                    # gray even though the individual cards repainted white.
                    image = quick.grab().toImage()
                    ratio = image.devicePixelRatio()
                    panel = quick._panel.geometry()
                    for point in (QPoint(panel.center().x(), panel.top() + 2),
                                  QPoint(panel.left() + 2, panel.center().y()),
                                  QPoint(panel.right() - 2, panel.center().y())):
                        self.assertEqual(image.pixelColor(round(point.x() * ratio),
                                                          round(point.y() * ratio)),
                                         QColor(SiColors.CARD))
                quick.hide()
        finally:
            set_theme(previous_theme)
            self.app.setStyleSheet(previous_qss)

    def test_stale_read_does_not_reset_slider_during_drag(self):
        dialog = self.popup()
        dialog.refresh()
        slider = dialog._sliders['brightness']
        slider.setSliderDown(True)
        slider.setValue(90)
        self.jobs.finish()
        self.assertEqual(slider.value(), 90)
        slider.setSliderDown(False)

    def test_clicking_slider_track_also_commits_once(self):
        dialog = self.popup()
        self.app.processEvents()
        slider = dialog._sliders['brightness']
        QTest.mouseClick(slider, Qt.LeftButton, pos=QPoint(slider.width() - 2, slider.height() // 2))
        self.assertEqual(len(self.jobs.pending), 1)
        self.jobs.finish()
        self.assertNotEqual(self.service.values['brightness'], 65)

    def test_unsupported_and_readonly_properties_are_not_exposed(self):
        props = [PropInfo('mode', 'Mode', 'uint', True, True),
                 PropInfo('brightness', 'Brightness', 'uint', True, False, (1, 100, 1)),
                 PropInfo('color', 'Not RGB', 'uint', True, True, (0, 10, 1))]
        self.assertEqual(light_controls(props), {})

    def test_virtual_service_does_not_construct_account_client(self):
        self.assertFalse(hasattr(self.service, '_api'))
        with self.assertRaises(ServiceError):
            self.service.set_power_state('real-device', True)


if __name__ == '__main__':
    unittest.main()
