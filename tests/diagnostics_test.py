# SPDX-License-Identifier: GPL-3.0-or-later
"""Reports must preserve useful capabilities while excluding all private data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.core.diagnostics import build_report, fetch_public_spec, safe_observation
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.core.camera_access import build_camera_source
from app.core.camera_preview import CameraPreviewError
from app.ui.diagnostic_dialog import DiagnosticDialog
from tests.camera_preview_test import Jobs


URN = 'urn:miot-spec-v2:device:motion-sensor:00000000:example-presence:1'
DEVICE = DeviceInfo('PRIVATE-DID', 'PRIVATE-NAME', 'example.sensor.presence',
                    'PRIVATE-HOME', 'PRIVATE-ROOM', True, URN, firmware='1.2.3_456')
SPEC = {'services': [{'iid': 2, 'type': 'urn:miot-spec-v2:service:motion-sensor:00000000:example:1',
    'description': 'Presence', 'properties': [{'iid': 1,
        'type': 'urn:miot-spec-v2:property:occupancy-status:00000000:example:1',
        'description': 'Occupancy', 'format': 'uint8', 'access': ['read', 'notify'],
        'value-range': [0, 1, 1], 'value-list': [{'value': 0, 'description': 'Absent'},
                                              {'value': 1, 'description': 'Present'}],
        'value': 'PRIVATE-CURRENT-VALUE', 'token': 'PRIVATE-TOKEN'}],
    'actions': [{'iid': 1, 'type': 'urn:miot-spec-v2:action:play:00000000:example:1',
                  'description': 'Play', 'in': [1], 'out': [2]}],
    'events': [{'iid': 1, 'type': 'urn:miot-spec-v2:event:motion-detected:00000000:example:1',
                'description': 'Motion', 'arguments': [1], 'history': 'PRIVATE-HISTORY'}]}],
    'auth': 'PRIVATE-AUTH'}


def payload(report):
    return json.loads(report.split('```json\n', 1)[1].split('\n```', 1)[0])


class DiagnosticCoreTest(unittest.TestCase):
    def test_sample_preserves_notify_events_and_action_arguments_but_no_private_data(self):
        observation = {'attempted': True, 'stage': 'relay', 'error_code': 'RELAY',
            'vendor_id': 4, 'protocol': 'CS2', 'cloud_code': 0, 'http_status': 200,
            'authorization_fields_present': {'sign': True, 'private_key': 'PRIVATE-KEY'},
            'url': 'PRIVATE-MEDIA-URL', 'exception': 'PRIVATE-EXCEPTION',
            'timeline': [{'stage': 'authorization', 'elapsed_ms': 0},
                         {'stage': 'relay', 'elapsed_ms': 325, 'code': 'RELAY', 'did': 'PRIVATE-DID'}]}
        report = build_report(DEVICE, spec=SPEC, spec_status='ok', observation=observation)
        self.assertNotIn('PRIVATE-', report)
        data = payload(report)
        definition = data['capabilities']['definitions'][0]
        self.assertEqual(definition['properties'][0]['access'], ['read', 'notify'])
        self.assertEqual(definition['events'][0]['arguments'], [1])
        self.assertEqual(definition['actions'][0]['inputs'], [1])
        self.assertTrue(data['capabilities']['declared_only'])
        self.assertEqual(data['device']['firmware'], '1.2.3_456')
        self.assertEqual(data['observation']['error_code'], 'RELAY')

    def test_no_attempt_is_not_reported_as_compatible_or_connected(self):
        data = payload(build_report(DEVICE))
        self.assertEqual(data['observation']['error_code'], 'NOT_ATTEMPTED')
        self.assertNotIn('protocol', data['observation'])
        self.assertEqual(data['capabilities']['source'], 'unavailable')
        self.assertEqual(safe_observation({'error_code': ['PRIVATE'], 'stage': {}, 'timeline': None})['stage'], 'idle')

    def test_metadata_collection_never_reads_properties_or_attempts_video(self):
        service = MijiaService.__new__(MijiaService)
        service._api = Mock(api_base_url='https://de.api.io.mi.com/app')
        service._spec_cache = {}
        with patch('app.core.diagnostics.fetch_public_spec', return_value=(SPEC, 'ok')) as fetch:
            data = payload(service.device_diagnostic_report(DEVICE))
        fetch.assert_called_once_with(URN)
        service._api.get_devices_prop.assert_not_called()
        service._api.set_devices_prop.assert_not_called()
        service._api.session.post.assert_not_called()
        self.assertEqual(data['device']['cloud_region'], 'de')
        service.set_control_access('offline')
        with patch('app.core.diagnostics.fetch_public_spec') as fetch:
            data = payload(service.device_diagnostic_report(DEVICE))
            fetch.assert_not_called()
        self.assertEqual(data['capabilities']['fetch_status'], 'offline_or_not_authorized')

    def test_public_lookup_is_bounded_and_has_no_credentials(self):
        response = Mock()
        response.iter_content.return_value = [json.dumps(SPEC).encode()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('app.core.diagnostics.requests.get', return_value=response) as get:
            spec, status = fetch_public_spec(URN)
            self.assertEqual(status, 'ok')
            self.assertEqual(spec, SPEC)
            self.assertEqual(get.call_args.kwargs, {'params': {'type': URN}, 'timeout': (3, 6), 'stream': True})
        with patch('app.core.diagnostics.requests.get', side_effect=requests.Timeout('PRIVATE-URL')):
            self.assertEqual(fetch_public_spec(URN), (None, 'public_spec_timeout'))
        with patch('app.core.diagnostics.requests.get') as get:
            self.assertEqual(fetch_public_spec('https://PRIVATE-URL')[1], 'no_public_spec_type')
            get.assert_not_called()

    def test_vendor_probe_only_records_protocol_number_and_field_presence(self):
        values = {}
        with self.assertRaises(CameraPreviewError) as caught:
            build_camera_source('PRIVATE-DID', 'example.camera.1', lambda *_: {
                'vendor': {'vendor': 7, 'vendor_params': {'p2p_id': 'PRIVATE-P2P', 'init_string': 'PRIVATE-INIT'}},
                'public_key': 'PRIVATE-KEY', 'sign': 'PRIVATE-SIGN'}, diagnostic=values.update)
        self.assertEqual(caught.exception.code, 'VENDOR_UNSUPPORTED')
        self.assertEqual(values['vendor_id'], 7)
        self.assertNotIn('PRIVATE-', json.dumps(values))

    def test_cache_fallback_does_not_invent_event_support(self):
        data = payload(build_report(DEVICE, cached_spec={'properties': [], 'actions': []}, spec_status='public_spec_timeout'))
        self.assertEqual(data['capabilities']['definitions']['events'], 'unavailable_in_sdk_cache')


class DiagnosticDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clipboard = self.app.clipboard().text()
        self.jobs = Jobs()
        self.service = SimpleNamespace(device_diagnostic_report=Mock(return_value='safe report'))
        second = deepcopy(DEVICE)
        second.model = 'example.speaker.1'
        self.dialog = DiagnosticDialog(self.service, self.jobs, [DEVICE, second])
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.app.clipboard().setText(self.clipboard)

    def test_clipboard_requires_explicit_copy_and_switching_ignores_old_report(self):
        function, success, _ = self.jobs.pending.pop(0)
        result = function()
        self.dialog.selector.setCurrentIndex(1)
        success(result)
        self.assertEqual(self.dialog.preview.toPlainText(), '')
        self.assertFalse(self.dialog.copy.isEnabled())
        self.dialog._generate()
        function, success, _ = self.jobs.pending.pop(0)
        success(function())
        self.assertEqual(self.app.clipboard().text(), self.clipboard)
        self.dialog._copy()
        self.assertEqual(self.app.clipboard().text(), 'safe report')

    def test_closed_dialog_ignores_pending_report(self):
        _, success, failure = self.jobs.pending.pop(0)
        self.dialog.close()
        success('late report')
        failure(RuntimeError('PRIVATE-ERROR'))
        self.assertEqual(self.dialog.preview.toPlainText(), '')


if __name__ == '__main__':
    unittest.main()
