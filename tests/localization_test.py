# SPDX-License-Identifier: GPL-3.0-or-later
"""设备中文名本地化：英文默认名换中文，用户自定义名不动。"""
import unittest

from app.core.localization import has_cjk, localize_name, spec_category

_CAMERA = 'urn:miot-spec-v2:device:camera:0000A01C:isa-hlmax:2'
_ROUTER = 'urn:miot-spec-v2:device:router:0000A036:xiaomi-ra50:1'


class LocalizationTest(unittest.TestCase):
    def test_known_model_uses_product_name(self):
        self.assertEqual(
            localize_name('Mijia DD Combination Washer Dryer 10kg',
                          'mibx5.washer.v7', ''),
            '米家直驱洗烘一体机 10kg')
        self.assertEqual(
            localize_name('Xiaomi Bluetooth Speaker Mini',
                          'xiaomi.wifispeaker.m01a', ''),
            '小米蓝牙音箱 mini')

    def test_unknown_model_falls_back_to_category(self):
        self.assertEqual(
            localize_name('Mystery Camera 2000', 'foo.camera.x1', _CAMERA),
            '智能摄像机')

    def test_user_short_name_is_kept(self):
        # 用户把路由器改名成 "chu"：型号在表里也不能覆盖用户命名
        self.assertIsNone(localize_name('chu', 'xiaomi.router.ra50', _ROUTER))

    def test_chinese_name_is_untouched(self):
        self.assertIsNone(localize_name('NAS 电量插座', 'cuco.plug.v3', ''))
        self.assertTrue(has_cjk('NAS 电量插座'))

    def test_spec_category(self):
        self.assertEqual(spec_category(_CAMERA), 'camera')
        self.assertEqual(spec_category(''), '')
        self.assertEqual(spec_category('not-a-spec'), '')


if __name__ == '__main__':
    unittest.main()
