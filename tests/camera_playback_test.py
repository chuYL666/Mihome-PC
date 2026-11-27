# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""SD 卡回放协议编解码的离线回归。"""
import base64
import json
import os
import struct
import unittest
from datetime import datetime

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app.core import camera_playback as pb


def _index(records):
    """按 wire 格式拼一段索引回复。"""
    out = bytearray(b'\x00\x00\x00\x00')
    for start, packed in records:
        out += struct.pack('<II', start, packed)
    return bytes(out)


class PlaybackProtocolTest(unittest.TestCase):

    def test_rdt_requests_are_binary_not_json(self):
        req = pb.index_request(10)
        self.assertEqual(len(req), 24)
        self.assertEqual(struct.unpack_from('<I', req, 8)[0], 10)
        self.assertEqual(req[:8], b'\x00' * 8)
        self.assertNotIn(b'{', req)
        file_req = pb.file_request(1_700_000_000, 10)
        self.assertEqual(len(file_req), 12)
        self.assertEqual(struct.unpack_from('<I', file_req, 0)[0], 1_700_000_000)
        self.assertEqual(struct.unpack_from('<I', file_req, 8)[0], 10)
        self.assertEqual(file_req[4:8], b'\x00' * 4)

    def test_parse_index_reads_start_duration_and_event_bit(self):
        start = 1_700_000_000
        payload = _index([(start, 60), (start + 60, 60 | pb._EVENT_BIT)])
        clips = pb.parse_index(payload)
        self.assertEqual([(c.start, c.duration, c.event) for c in clips],
                         [(start, 60, False), (start + 60, 60, True)])
        self.assertEqual(clips[1].end, start + 120)

    def test_parse_index_drops_unwritten_slots_and_duplicates(self):
        start = 1_700_000_000
        payload = _index([
            (0, 60),                       # 未写过的槽：1970
            (2_600_000_000, 60),          # 远未来
            (start, 0),                    # 零时长
            (start, 4000),                 # 超过上限
            (start, 60),
            (start, 60),                   # 重复
            (start - 30, 30),
        ])
        clips = pb.parse_index(payload)
        self.assertEqual([c.start for c in clips], [start - 30, start])
        self.assertEqual(clips[0].duration, 30)

    def test_parse_index_handles_empty_or_truncated(self):
        self.assertEqual(pb.parse_index(None), [])
        self.assertEqual(pb.parse_index(b'\x00' * 4), [])
        self.assertEqual(pb.parse_index(b'\x00' * 11), [])

    def test_playback_request_has_every_mandatory_field(self):
        body = json.loads(pb.playback_request(7, 1_700_000_000, 1_700_000_060))
        self.assertEqual(body, {
            'sessionid': 7, 'autoswitchtolive': 1, 'avchannelmerge': 1,
            'starttime': 1_700_000_000, 'endtime': 1_700_000_060,
            'offset': 0, 'speed': 1,
        })
        self.assertNotIn('channel', body)

    def test_dual_lens_uses_five_parallel_arrays(self):
        body = json.loads(pb.playback_request(3, 100, 160, [0, 10]))
        self.assertEqual(body['channel'], [0, 10])
        for key in ('starttime', 'endtime', 'offset', 'speed'):
            self.assertIsInstance(body[key], list)
            self.assertEqual(len(body[key]), 2)
            self.assertTrue(all(isinstance(v, int) for v in body[key]))
        self.assertEqual(body['starttime'], [100, 100])
        self.assertEqual(body['speed'], [1, 1])
        self.assertIsInstance(body['sessionid'], int)

    def test_stop_request_returns_to_live(self):
        body = json.loads(pb.stop_request(9))
        self.assertEqual((body['starttime'], body['endtime']), (0, 0))

    def test_status_parsing(self):
        self.assertTrue(pb.status_found(pb.parse_status(
            b'{"id":1,"status":"filefound","starttime":1700000000,"duration":60}')))
        self.assertFalse(pb.status_found(pb.parse_status(
            b'{"id":1,"status":"filenotfound"}')))
        self.assertIsNone(pb.parse_status(b'not json'))
        self.assertIsNone(pb.parse_status(b''))
        self.assertIsNone(pb.parse_status(b'[1,2]'))

    def test_grouping_and_labels(self):
        base = int(datetime(2026, 11, 26, 14, 30, 5).timestamp())
        clips = [pb.Clip(base, 60), pb.Clip(base + 60, 60),
                 pb.Clip(base - 86400, 60)]
        groups = pb.group_by_day(clips)
        self.assertEqual([len(items) for _, items in groups], [2, 1])
        self.assertIn('14:30:05', pb.format_span(clips[0]))
        self.assertEqual(pb.day_timestamp(groups[0][0]),
                         int(datetime(2026, 11, 26).timestamp()))
        self.assertIsNone(pb.day_timestamp('not-a-date'))
        self.assertEqual(pb.clips_near(clips, base + 30), [clips[0]])

    def test_storage_channels_and_window(self):
        self.assertEqual(pb.storage_channels('chuangmi.camera.029a02'), [0])
        self.assertEqual(pb.storage_channels('isa.camera.hlmax'), [0, 10])
        self.assertEqual(pb.storage_channels('isa.camera.hlmax', channel=1), [10, 0])
        self.assertEqual(pb.playback_window(pb.Clip(100, 60)), (100, 160))


class RelayMessageTest(unittest.TestCase):

    def test_index_message(self):
        payload = _index([(1_700_000_000, 60)])
        message = 'RDT 6 ' + base64.b64encode(payload).decode()
        kind, value = pb.parse_relay_message(message)
        self.assertEqual(kind, 'index')
        self.assertEqual(value, [pb.Clip(1_700_000_000, 60)])

    def test_file_status_and_notice_messages(self):
        kind, value = pb.parse_relay_message(
            'RDT 1 ' + base64.b64encode(b'ftyp').decode())
        self.assertEqual((kind, value), ('file', b'ftyp'))
        body = b'{"id":1,"status":"filefound","starttime":1700000000,"duration":60}'
        self.assertEqual(pb.parse_relay_message(
            'PLAYBACK ' + base64.b64encode(body).decode()),
            ('status', {'id': 1, 'status': 'filefound',
                        'starttime': 1_700_000_000, 'duration': 60}))
        self.assertEqual(pb.parse_relay_message('NOTICE LIVE'), ('notice', 'LIVE'))
        self.assertEqual(pb.parse_relay_message('NOTICE INDEX_FAILED'),
                         ('notice', 'INDEX_FAILED'))

    def test_unrelated_or_corrupt_messages_are_ignored(self):
        for text in ('READY', 'INFO VIDEO_CODEC H264', 'NOTICE MOTOR_OK',
                     'RDT 6 !!!not-base64!!!', 'PLAYBACK !!!',
                     'RDT 99 ' + base64.b64encode(b'x').decode(), None):
            with self.subTest(text=text):
                self.assertIsNone(pb.parse_relay_message(text))


if __name__ == '__main__':
    unittest.main()
