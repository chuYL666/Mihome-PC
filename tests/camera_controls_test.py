# SPDX-License-Identifier: GPL-3.0-or-later
"""云台 / 对讲控制链路的离屏回归测试。"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtWidgets import QApplication

from app.core import camera_ptz
from app.core.camera_preview import CameraSession
from app.core.camera_talk import (CODEC_OPUS, CODEC_PCMA, CODEC_PCM16,
                                  TalkEncoder, pcm16_to_alaw, speaker_codec,
                                  to_8k)
from app.core.models import DeviceInfo
from app.ui.camera_canvas import CameraCanvas

app = QApplication.instance() or QApplication(sys.argv)


class PtzCapabilityTest(unittest.TestCase):
    def test_all_user_cameras_have_ptz(self):
        for model in ('chuangmi.camera.039a01', 'chuangmi.camera.ip029a',
                      'isa.camera.hlmax', 'chuangmi.camera.029a02'):
            self.assertEqual(camera_ptz.capability(model), camera_ptz.STEP, model)

    def test_fixed_lens_and_second_channel_disabled(self):
        self.assertEqual(camera_ptz.capability('loock.cateye.v02'), '')
        self.assertEqual(camera_ptz.capability('isa.camera.hlmax', channel=1), '')
        self.assertEqual(camera_ptz.capability('xiaomi.wifispeaker.lx06'), '')

    def test_continuous_model_and_payload(self):
        cap = camera_ptz.capability('isa.camera.df3')
        self.assertEqual(cap, camera_ptz.CONTINUOUS)
        self.assertEqual(camera_ptz.motor_payload(camera_ptz.LEFT, cap, 5),
                         '{"direction":"left","speed":5}')
        self.assertEqual(camera_ptz.motor_payload(camera_ptz.UP, cap, stop=True),
                         '{"direction":"stop","speed":0}')
        self.assertEqual(camera_ptz.motor_payload(camera_ptz.RIGHT, camera_ptz.STEP),
                         '{"operation":2}')


class AudioEncodingTest(unittest.TestCase):
    def test_speaker_codec_by_model(self):
        self.assertEqual(speaker_codec('chuangmi.camera.039a01'), CODEC_PCMA)
        self.assertEqual(speaker_codec('isa.camera.df3'), CODEC_PCM16)
        self.assertEqual(speaker_codec('chuangmi.camera.72ac1'), CODEC_OPUS)
        self.assertIsNone(speaker_codec('loock.cateye.v02'))

    def test_alaw_known_vectors(self):
        # G.711 A-law: 0 -> 0xD5, -1 -> 0x55, 小幅度正值落在 0xD0~0xFF
        out = pcm16_to_alaw(np.array([0, -1], dtype='<i2').tobytes())
        self.assertEqual(out[0], 0xD5)
        self.assertEqual(out[1], 0x55)
        self.assertEqual(len(pcm16_to_alaw(np.zeros(320, dtype='<i2').tobytes())), 320)

    def test_resample_and_framing(self):
        t = np.arange(0, 1.0, 1 / 48000)
        pcm = (np.sin(2 * np.pi * 1000 * t) * 12000).astype('<i2').tobytes()
        self.assertEqual(len(to_8k(pcm)), 16000)  # 8000 samples * 2 bytes
        frames = TalkEncoder(CODEC_PCMA).feed(pcm)
        self.assertEqual(len(frames), 25)         # 1 s = 25 * 40 ms
        self.assertTrue(all(len(f) == 320 for f in frames))
        frames16 = TalkEncoder(CODEC_PCM16).feed(pcm)
        self.assertEqual(len(frames16), 25)
        self.assertTrue(all(len(f) == 640 for f in frames16))

    def test_partial_frame_is_buffered(self):
        enc = TalkEncoder(CODEC_PCMA)
        self.assertEqual(enc.feed(b'\x00' * 100), [])
        frames = enc.feed(b'\x00' * (3840 - 100))
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 320)


class SessionCommandTest(unittest.TestCase):
    def test_new_commands_are_formatted(self):
        session = CameraSession()
        sent = []
        session._host = type('Host', (), {'send': lambda self, sid, cmd: sent.append((sid, cmd)) or True})()
        session._sid = 'abc'
        self.assertTrue(session.talk(True))
        self.assertTrue(session.set_speaker_codec(1027))
        self.assertTrue(session.motor('left', 5))
        self.assertTrue(session.send_audio(b'\x00' * 320))
        self.assertEqual(sent[0][1], 'TALK 1')
        self.assertEqual(sent[1][1], 'CODEC 1027')
        self.assertEqual(sent[2][1], 'MOTOR left 5')
        self.assertTrue(sent[3][1].startswith('AUD '))
        self.assertFalse(session.motor('diagonal', 5))
        self.assertFalse(session.set_speaker_codec(9999))
        self.assertFalse(session.send_audio(b''))
        self.assertFalse(session.send_audio(b'x' * 5000))


class CanvasControlTest(unittest.TestCase):
    def test_ptz_pad_and_talk_button(self):
        c = CameraCanvas(compact_ptz=True)
        c.set_ptz_capability(camera_ptz.STEP)
        c.set_controls_enabled(True)
        c.show()
        app.processEvents()
        self.assertTrue(c._ptz_pad.isVisible())
        self.assertTrue(c.talk.isEnabled())
        c.set_talk_supported(False)
        self.assertFalse(c.talk.isEnabled())
        c.set_controls_enabled(False)
        self.assertFalse(c._ptz_pad.isVisible())
        self.assertEqual(c.ptz_capability(), camera_ptz.STEP)
        c.set_ptz_capability('')
        c.set_controls_enabled(True)
        self.assertFalse(c._ptz_pad.isVisible())

    def test_ptz_pad_emits_step_and_release(self):
        c = CameraCanvas(compact_ptz=True)
        c.set_ptz_capability(camera_ptz.STEP)
        c.set_controls_enabled(True)
        steps, releases = [], []
        c.ptz_step.connect(steps.append)
        c.ptz_released.connect(lambda: releases.append(1))
        c._ptz_pad._buttons[camera_ptz.LEFT].pressed.emit()
        c._ptz_pad._buttons[camera_ptz.LEFT].released.emit()
        self.assertEqual(steps, [camera_ptz.LEFT])
        self.assertEqual(releases, [1])


class PanelWiringTest(unittest.TestCase):
    def test_panel_uses_model_capability_not_hardcoded(self):
        from app.ui.camera_preview import CameraPreviewPanel
        for model in ('chuangmi.camera.039a01', 'chuangmi.camera.ip029a',
                      'isa.camera.hlmax'):
            dev = DeviceInfo(did=model, name=model, model=model,
                             home_name='家', room_name='客厅', online=True)
            panel = CameraPreviewPanel(type('S', (), {})(), type('J', (), {})(), dev)
            self.assertEqual(panel._ptz_cap, camera_ptz.STEP, model)
            self.assertTrue(panel._canvas._ptz_pad._capability)
            panel.stop()
            panel.deleteLater()

    def test_step_sends_ptz_command(self):
        from app.ui.camera_preview import CameraPreviewPanel
        dev = DeviceInfo(did='1', name='摄像头', model='chuangmi.camera.039a01',
                         home_name='家', room_name='客厅', online=True)
        panel = CameraPreviewPanel(type('S', (), {})(), type('J', (), {})(), dev)
        calls = []
        panel._session = type('S', (), {
            'command': lambda self, n, v: calls.append((n, v)) or True,
            'motor': lambda self, d, s: calls.append(('MOTOR', d, s)) or True,
            'stop': lambda self: None,
        })()
        panel._playing = True
        panel._ptz_step(camera_ptz.UP)
        self.assertIn(('PTZ', camera_ptz.UP), calls)
        panel._ptz_release()
        panel.stop()
        panel.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)
