# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""回放命令、片段列表对话框与预览面板接线。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.core import camera_playback as pb
from app.core.camera_preview import CameraSession
from app.ui.camera_playback_dialog import CameraPlaybackDialog
from app.ui.camera_preview import CameraPreviewPanel
from tests.camera_preview_test import Jobs


class PlaybackCommandTest(unittest.TestCase):

    def test_session_commands_are_well_formed(self):
        session = CameraSession()
        with patch.object(session, '_send', return_value=True) as send:
            self.assertTrue(session.request_index(10))
            send.assert_called_with('INDEX 10')
            session.play(100, 160)
            send.assert_called_with('PLAY 100 160')
            session.play(100, 160, [0, 10])
            send.assert_called_with('PLAY 100 160 0 10')
            session.play_direct(100, 160, [10])
            send.assert_called_with('PLAYDIRECT 100 160 10')
            session.back_to_live()
            send.assert_called_with('LIVE')
            session.request_file(1_700_000_000, 10)
            send.assert_called_with('FILE 1700000000 10')

    def test_bad_arguments_never_reach_the_relay(self):
        session = CameraSession()
        with patch.object(session, '_send', return_value=True) as send:
            for call in (lambda: session.play('x', 1),
                         lambda: session.request_index('; DROP'),
                         lambda: session.play(1, 2, ['oops']),
                         lambda: session.request_file('x', 1)):
                self.assertFalse(call())
            send.assert_not_called()


class PlaybackDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = CameraPlaybackDialog(dual=True)
        self.widgets = [self.dialog]

    def tearDown(self):
        for w in self.widgets:
            w.close()
            w.deleteLater()
        self.app.processEvents()

    def test_clips_are_grouped_by_day_and_selectable(self):
        base = pb.day_timestamp('2026-11-26') or 0
        clips = [(0, pb.Clip(base + 3600, 60)),
                 (10, pb.Clip(base + 3660, 60, event=True)),
                 (0, pb.Clip(base - 86400 + 3600, 60))]
        self.dialog.set_clips(clips)
        self.assertEqual(self.dialog._days.count(), 2)
        self.assertEqual(self.dialog._list.count(), 2)
        self.assertIn('第二镜头', self.dialog._list.item(1).text())
        self.assertIn('有侦测事件', self.dialog._list.item(1).text())
        self.dialog._list.setCurrentRow(1)
        channel, clip = self.dialog.current_clip()
        self.assertEqual((channel, clip.start), (10, base + 3660))

    def test_play_and_retry_signals(self):
        base = pb.day_timestamp('2026-11-26') or 0
        clip = pb.Clip(base + 60, 60)
        self.dialog.set_clips([(0, clip)])
        self.dialog._list.setCurrentRow(0)
        play, retry = Mock(), Mock()
        self.dialog.play_requested.connect(play)
        self.dialog.direct_requested.connect(retry)
        self.dialog._play()
        play.assert_called_once_with((0, clip))
        self.dialog.show_direct_retry()
        self.assertFalse(self.dialog._direct_btn.isHidden())
        self.dialog._retry_direct()
        retry.assert_called_once_with((0, clip))

    def test_empty_index_gives_actionable_hint(self):
        self.dialog.set_clips([])
        self.assertIn('SD 卡', self.dialog._status.text())
        self.assertEqual(self.dialog._list.count(), 0)


class PlaybackPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.jobs = Jobs()
        self.device = SimpleNamespace(online=True, did='camera-test', name='测试摄像头',
                                      room_name='客厅', model='chuangmi.camera.029a02')
        self.service = SimpleNamespace(prepare_camera_source=Mock(return_value='signed-source'),
                                       power_state=Mock(return_value=False),
                                       set_power_state=Mock())
        self.backend = SimpleNamespace(cancelled=False, start=Mock(return_value='http://127.0.0.1/x.ts'),
                                       command=Mock(return_value=True), stop=Mock(),
                                       play=Mock(return_value=True),
                                       play_direct=Mock(return_value=True),
                                       request_index=Mock(return_value=True),
                                       back_to_live=Mock(return_value=True))
        self.factory = patch('app.ui.camera_preview.CameraSession', return_value=self.backend)
        self.factory.start()
        self.panel = CameraPreviewPanel(self.service, self.jobs, self.device)
        self.panel._player = Mock()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.factory.stop()

    def test_header_has_playback_button(self):
        self.assertEqual(self.panel._playback_btn.text(), '回放')
        self.assertIsNotNone(self.panel._playback_btn.toolTip())

    def test_index_request_walks_storage_channels_with_timeout_fallback(self):
        self.panel._session = self.backend
        dlg = CameraPlaybackDialog(dual=False)
        self.panel._playback_dlg = dlg
        self.panel._request_index()
        self.backend.request_index.assert_called_with(0)
        self.assertTrue(self.panel._playback_index_timer.isActive())
        self.panel._on_playback(self.panel._generation, 'index', [pb.Clip(100, 60)])
        self.assertFalse(self.panel._playback_index_timer.isActive())
        self.assertEqual(dlg._list.count(), 1)
        dlg.close()

    def test_status_and_notice_update_dialog(self):
        dlg = CameraPlaybackDialog()
        self.panel._playback_dlg = dlg
        self.panel._on_playback(self.panel._generation, 'status',
                                {'status': 'filefound', 'duration': 60})
        self.assertTrue(self.panel._playback_active)
        self.assertIn('正在回放', dlg._status.text())
        self.panel._on_playback(self.panel._generation, 'notice', 'PLAYBACK_FAILED')
        self.assertFalse(dlg._direct_btn.isHidden())
        self.panel._on_playback(self.panel._generation, 'notice', 'LIVE')
        self.assertFalse(self.panel._playback_active)
        dlg.close()

    def test_play_clip_uses_exact_start_second(self):
        self.panel._session = self.backend
        clip = pb.Clip(1_700_000_005, 60)
        self.panel._play_clip((0, clip))
        self.backend.play.assert_called_once_with(1_700_000_005, 1_700_000_065, [])
        self.panel._play_clip_direct((10, clip))
        self.backend.play_direct.assert_called_once_with(
            1_700_000_005, 1_700_000_065, [10])

    def test_stop_closes_dialog_and_clears_state(self):
        dlg = CameraPlaybackDialog()
        self.panel._playback_dlg = dlg
        self.panel._session = self.backend
        self.panel._playback_active = True
        self.panel.stop()
        self.assertIsNone(self.panel._playback_dlg)
        self.assertFalse(self.panel._playback_active)
        self.assertFalse(self.panel._playback_index_timer.isActive())


if __name__ == '__main__':
    unittest.main()
