# SPDX-License-Identifier: GPL-3.0-or-later
# MijiaDesk: 米家设备的 Windows 桌面控制端
"""托盘语音条的麦克风按钮：点击唤起系统语音输入，文字进来后结束等待。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.models import DeviceInfo
from app.ui.tray.quick_window import TrayQuickWindow


class _StubService:
    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


class _StubJobs:
    def submit(self, *args, **kwargs):
        return None


class TrayVoiceInputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, kwargs in [('data_file', {'side_effect': lambda n: Path(self.temp.name) / n}),
                             ('data_dir', {'return_value': Path(self.temp.name)})]:
            patcher = patch('app.core._json_store.' + name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()

    def _window(self):
        window = TrayQuickWindow(_StubService(), _StubJobs())
        self.windows.append(window)
        speaker = DeviceInfo(did='spk', name='小爱音箱Pro', model='xiaomi.wifispeaker.lx06',
                             home_name='家', room_name='客厅', online=True)
        window.set_devices([speaker], {})
        window.set_access_state('ready')
        window.show()
        self.assertTrue(window._voice_frame.isVisible())
        return window

    def test_mic_button_sits_left_of_keyboard_button(self):
        window = self._window()
        bar = window._voice_frame
        layout = bar.layout()
        mic_index = layout.indexOf(window._mic_btn)
        self.assertGreaterEqual(mic_index, 0)
        # 麦克风在输入框之后、键盘（最后一个按钮）之前
        self.assertLess(mic_index, layout.count() - 1)

    def test_click_mic_enters_listening_state(self):
        window = self._window()
        with patch('app.ui.tray.quick_window.voice_typing.trigger', return_value=True) as trigger:
            QTest.mouseClick(window._mic_btn, Qt.LeftButton)
        trigger.assert_called_once()
        self.assertTrue(window._listening)
        self.assertIn('正在听', window._voice_edit.placeholderText())

    def test_typed_text_ends_listening(self):
        window = self._window()
        with patch('app.ui.tray.quick_window.voice_typing.trigger', return_value=True):
            QTest.mouseClick(window._mic_btn, Qt.LeftButton)
        window._voice_edit.setText('打开卧室灯')
        self.assertFalse(window._listening)
        self.assertEqual(window._voice_edit.placeholderText(), '')

    def test_trigger_failure_explains_in_placeholder(self):
        window = self._window()
        with patch('app.ui.tray.quick_window.voice_typing.trigger', return_value=False):
            QTest.mouseClick(window._mic_btn, Qt.LeftButton)
        self.assertFalse(window._listening)
        self.assertIn('没能唤起', window._voice_edit.placeholderText())

    def test_timeout_points_at_microphone_and_language_pack(self):
        window = self._window()
        with patch('app.ui.tray.quick_window.voice_typing.trigger', return_value=True):
            QTest.mouseClick(window._mic_btn, Qt.LeftButton)
        window._voice_timeout()
        self.assertFalse(window._listening)
        self.assertIn('麦克风', window._voice_edit.placeholderText())

    def test_deactivate_does_not_hide_while_listening(self):
        window = self._window()
        with patch('app.ui.tray.quick_window.voice_typing.trigger', return_value=True):
            QTest.mouseClick(window._mic_btn, Qt.LeftButton)
        window.hide = lambda: (_ for _ in ()).throw(AssertionError('不应在语音输入时收起窗口'))
        window._maybe_hide_on_deactivate()


if __name__ == '__main__':
    unittest.main()
