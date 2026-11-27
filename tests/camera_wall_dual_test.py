# SPDX-License-Identifier: GPL-3.0-or-later
"""摄像头墙：双摄自动两格 + 错峰鉴权 + 画面不裁剪。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app.ui.camera_wall import CameraWallDialog, _CameraTile

app = QApplication.instance() or QApplication([])


class Dev:
    def __init__(self, did, model, name):
        self.did, self.model, self.name = did, model, name
        self.online = True
        self.room_name = '院子'


class Jobs:
    def submit(self, fn, *args, **kwargs):
        return fn()


class FakeSession:
    """假的会话对象：close() 时 stop() 要能被调用。"""
    def stop(self):
        pass


class Svc:
    def prepare_camera_source(self, device):
        return {'did': device.did}

    def read_prop_raw(self, did, siid, piid):
        return True      # 两个镜头电源都开着，不应触发属性写入

    def set_prop_raw(self, *args):
        raise AssertionError('不该写镜头电源属性')


def devices():
    return [Dev('cam1', 'chuangmi.camera.039a01', '冰箱'),
            Dev('cam2', 'chuangmi.camera.039a01', '二楼'),
            Dev('cam3', 'chuangmi.camera.ip029a', '出租屋'),
            Dev('cam4', 'chuangmi.camera.039a01', '堂屋'),
            Dev('dual', 'isa.camera.hlmax', '鸡圈菜地双摄')]


class WallDualTest(unittest.TestCase):
    def setUp(self):
        self.wall = CameraWallDialog(Svc(), Jobs(), devices())

    def tearDown(self):
        self.wall.close()

    def test_dual_camera_occupies_two_tiles(self):
        tiles = list(self.wall._tiles.values())
        self.assertEqual(len(tiles), 6, '5 台设备、双摄占两格应共 6 格')
        pair = self.wall._tile_by_did['dual']
        self.assertEqual(len(pair), 2)
        self.assertEqual([t._lens for t in pair], [0, 1])
        self.assertEqual([t._wait_peer for t in pair], [False, True])
        self.assertTrue(all(t._lens_locked for t in pair))
        self.assertIn('云台', pair[0]._name.text())
        self.assertIn('枪机', pair[1]._name.text())

    def test_tiles_never_crop_the_frame(self):
        for tile in self.wall._tiles.values():
            self.assertFalse(tile._canvas._fill, '总览格子必须完整显示，不能裁剪')

    def test_second_lens_waits_for_first_frame(self):
        wall = self.wall
        cloud, box = wall._tile_by_did['dual']
        started = []

        def fake_start(tile):
            started.append(tile)
            tile._session = FakeSession()
            tile._start_at = time.monotonic()
            tile._got_frame = False

        with patch.object(_CameraTile, 'start', autospec=True,
                          side_effect=lambda self_tile: fake_start(self_tile)):
            wall._pending = [cloud, box] + [
                t for t in wall._tiles.values() if t not in (cloud, box)]
            wall._pump()
            self.assertIn(cloud, started)
            self.assertNotIn(box, started, '云台没出帧时不能并发开枪机鉴权')
            self.assertIn(box, wall._pending, '枪机只是延后，不能被丢掉')

            # 其它两路连上后腾出并发名额，云台也出了首帧
            for tile in started:
                if tile is not cloud:
                    tile._url = 'http://127.0.0.1/stream'
            cloud._got_frame = True
            wall._pump()
            self.assertIn(box, started, '云台出帧后枪机应立即接上')

    def test_second_lens_is_not_starved_forever(self):
        wall = self.wall
        cloud, box = wall._tile_by_did['dual']
        cloud._session = FakeSession()
        cloud._got_frame = False
        cloud._start_at = time.monotonic() - 20.0
        self.assertFalse(wall._peer_still_connecting(box),
                         '云台 12 秒没出帧就应放行枪机')


if __name__ == '__main__':
    unittest.main()
