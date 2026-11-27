# SPDX-License-Identifier: GPL-3.0-or-later
"""双画面窗口：第二路不能在云台还没出帧时抢跑鉴权。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest

from PySide6.QtWidgets import QApplication

from app.ui.camera_dual_view import DualCameraDialog

app = QApplication.instance() or QApplication([])


class Dev:
    did = 'did-hlmax'
    name = '鸡圈菜地双摄'
    online = True
    model = 'isa.camera.hlmax'
    room_name = '院子'


class Jobs:
    def submit(self, fn, *args, **kwargs):
        return fn()


class Svc:
    def prepare_camera_source(self, device):
        return {'did': device.did}


class DualViewTimingTest(unittest.TestCase):
    def setUp(self):
        self.dialog = DualCameraDialog(Svc(), Jobs(), Dev(), quality=1)
        self.opened = []
        self.dialog._start_pane = lambda channel: self.opened.append(channel)

    def tearDown(self):
        self.dialog._secondary_timer.stop()
        self.dialog.close()

    def test_secondary_waits_until_primary_has_frames(self):
        d = self.dialog
        d._secondary_waits = 0
        primary = d._pane(0)
        # 云台还在鉴权/等首帧：连续两轮检查都不开第二路
        d._check_secondary()
        d._check_secondary()
        self.assertEqual(self.opened, [], '云台没出帧时不能并发开枪机')
        self.assertEqual(d._secondary_waits, 2)

        primary.frames = 1
        d._check_secondary()
        self.assertEqual(self.opened, [1], '云台一出帧就开枪机')

    def test_secondary_is_not_starved_forever(self):
        d = self.dialog
        d._secondary_waits = 0
        for _ in range(4):
            d._check_secondary()
        self.assertEqual(self.opened, [1], '云台一直不出帧也要兜底放行枪机')

    def test_on_frame_starts_secondary_immediately(self):
        d = self.dialog
        # 恢复真实 _start_pane，改用假会话避免起线程
        import app.ui.camera_dual_view as dv
        from PySide6.QtMultimedia import QVideoFrame
        from PySide6.QtGui import QImage

        class FakeSession:
            cancelled = False
            opened = []

            def start(self, source, quality=1, channel=0, on_progress=None, on_audio=None):
                FakeSession.opened.append(channel)
                return f'http://127.0.0.1/stream-{channel}'

            def stop(self):
                pass

        original = dv.CameraSession
        dv.CameraSession = FakeSession
        try:
            dialog = DualCameraDialog(Svc(), Jobs(), Dev(), quality=1)
            dialog._start_pane = self.dialog._start_pane  # 记录而不起线程
            dialog._secondary_timer.start(6000)
            frame = QVideoFrame(QImage(64, 36, QImage.Format_RGB32))
            dialog._on_frame(dialog._pane(0), frame)
            self.assertIn(1, self.opened, '云台首帧应立即触发枪机')
            dialog._secondary_timer.stop()
            dialog.close()
        finally:
            dv.CameraSession = original


if __name__ == '__main__':
    unittest.main()
