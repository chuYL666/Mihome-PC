# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, read-only device samples. Never serialize a device/API object."""
import json
import math
import platform
import re
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse

from app import __version__


def fetch_public_spec(spec_type):
    if not identifier(spec_type) or not spec_type.startswith('urn:miot-spec-v2:device:'):
        return None, 'no_public_spec_type'
    try:
        with requests.get('https://miot-spec.org/miot-spec-v2/instance',
                          params={'type': spec_type}, timeout=(3, 6), stream=True) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(16384):
                content.extend(chunk)
                if len(content) > 2_000_000:
                    return None, 'public_spec_too_large'
            raw = json.loads(content)
            if not isinstance(raw, dict) or not isinstance(raw.get('services'), list):
                return None, 'invalid_public_spec'
            return raw, 'ok'
    except requests.Timeout:
        return None, 'public_spec_timeout'
    except Exception:
        return None, 'public_spec_unavailable'


def identifier(value, limit=240):
    return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(r'[A-Za-z0-9_.:-]+', value) else None


def number(value):
    return value if type(value) in (int, float) and abs(value) <= 1e16 and math.isfinite(value) else None


def numbers(values):
    return [n for v in values[:64] if (n := number(v)) is not None] if isinstance(values, list) else []


def label(value):
    # Only used for descriptions fetched from the public MIoT specification.
    return ''.join(c for c in value[:160] if c.isprintable()) if isinstance(value, str) else ''


def items(value, limit):
    return value[:limit] if isinstance(value, list) else []


def public_capabilities(raw):
    """Keep public service/property/action/event definitions, never current values."""
    services = []
    for service in items(raw.get('services'), 80):
        if not isinstance(service, dict):
            continue
        entry = {'siid': number(service.get('iid')), 'type': identifier(service.get('type')),
                 'description': label(service.get('description'))}
        for kind, id_key in [('properties', 'piid'), ('actions', 'aiid'), ('events', 'eiid')]:
            definitions = []
            for item in items(service.get(kind), 160):
                if not isinstance(item, dict):
                    continue
                clean = {id_key: number(item.get('iid')), 'type': identifier(item.get('type')),
                         'description': label(item.get('description'))}
                if kind == 'properties':
                    clean.update(format=identifier(item.get('format')), unit=identifier(item.get('unit')),
                                 access=[x for x in items(item.get('access'), 8) if x in ('read', 'write', 'notify')],
                                 range=numbers(item.get('value-range')),
                                 enum=[{'value': number(v.get('value')), 'description': label(v.get('description'))}
                                       for v in items(item.get('value-list'), 64) if isinstance(v, dict)])
                elif kind == 'actions':
                    clean.update(inputs=numbers(item.get('in')), outputs=numbers(item.get('out')))
                else:
                    clean['arguments'] = numbers(item.get('arguments'))
                definitions.append(clean)
            entry[kind] = definitions
        services.append(entry)
    return services


def cached_capabilities(raw):
    """The SDK flattens specs and drops events/notify/input/output information."""
    result = {}
    for kind, iid in [('properties', 'piid'), ('actions', 'aiid')]:
        result[kind] = [{
            'name': identifier(p.get('name')), 'siid': number(p.get('method', {}).get('siid')),
            iid: number(p.get('method', {}).get(iid)), 'format': identifier(p.get('type')),
            'access': [c for c in ('r', 'w') if c in p.get('rw', '')],
            'range': numbers(p.get('range')),
        } for p in (raw or {}).get(kind, [])[:400] if isinstance(p, dict)]
    result['events'] = 'unavailable_in_sdk_cache'
    return result


STAGES = {'idle', 'power', 'authorization', 'relay', 'session_auth', 'media', 'decode', 'playing', 'stream'}
CODES = {'NONE', 'NOT_ATTEMPTED', 'UNKNOWN', 'VENDOR_UNSUPPORTED', 'AUTH_FIELDS', 'AUTH_KEY', 'CLOUD_REJECTED',
         'HTTP_ERROR', 'REQUEST_FAILED', 'CONFIG', 'RELAY', 'AUTH', 'MEDIA', 'STREAM',
         'HELPER_EXIT', 'TIMEOUT', 'DECODE', 'CANCELLED', 'OFFLINE', 'POWER_UNKNOWN',
         'POWER_READ_FAILED', 'AUDIO_UNSUPPORTED', 'CONTROL_FAILED'}


def safe_observation(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {k: raw[k] for k in ('attempted', 'power_readable', 'video_received') if type(raw.get(k)) is bool}
    for key in ('vendor_id', 'http_status', 'cloud_code', 'width', 'height', 'player_error'):
        if (n := number(raw.get(key))) is not None:
            result[key] = n
    result['stage'] = raw.get('stage') if isinstance(raw.get('stage'), str) and raw['stage'] in STAGES else 'idle'
    result['error_code'] = raw.get('error_code') if isinstance(raw.get('error_code'), str) and raw['error_code'] in CODES else 'UNKNOWN' if raw.get('attempted') else 'NOT_ATTEMPTED'
    if raw.get('protocol') in ('CS2', 'unrecognized'):
        result['protocol'] = raw['protocol']
    if isinstance(raw.get('authorization_fields_present'), dict):
        result['authorization_fields_present'] = {k: raw['authorization_fields_present'][k]
            for k in ('p2p_id', 'init_string', 'public_key', 'sign')
            if type(raw['authorization_fields_present'].get(k)) is bool}
    if type(raw.get('elapsed_ms')) is int:
        result['elapsed_ms'] = max(0, min(raw['elapsed_ms'], 86400000))
    result['timeline'] = [{
        'stage': e.get('stage') if isinstance(e.get('stage'), str) and e['stage'] in STAGES else 'idle',
        'elapsed_ms': max(0, min(e.get('elapsed_ms', 0), 86400000)) if type(e.get('elapsed_ms')) is int else 0,
        'code': e.get('code') if isinstance(e.get('code'), str) and e['code'] in CODES else None,
    } for e in items(raw.get('timeline'), 1000)[-24:] if isinstance(e, dict)]
    return result


def region_from_url(url):
    if not isinstance(url, str):
        return 'unknown'
    host = urlparse(url).hostname
    return 'cn' if host == 'api.io.mi.com' else next(
        (r for r in ('de', 'us', 'sg', 'ru', 'in', 'i2', 'tw') if host == f'{r}.api.io.mi.com'), 'unknown')


def build_report(device, *, spec=None, cached_spec=None, spec_status='not_requested',
                 region='unknown', observation=None):
    from .device_controls import device_kinds
    data = {
        'schema': 'mijiadeck-device-diagnostic/1',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'application': {'version': __version__, 'os': platform.system(), 'os_release': platform.release(),
                        'os_build': platform.version(), 'architecture': platform.machine()},
        'device': {'model': identifier(device.model), 'spec_type': identifier(getattr(device, 'spec_type', '')),
                   'firmware': identifier(getattr(device, 'firmware', ''), 80),
                   'online': bool(device.online), 'kinds': sorted(device_kinds(device)), 'cloud_region': region},
        'capabilities': {'source': 'public_miot_spec' if spec is not None else 'sdk_cache' if cached_spec else 'unavailable',
                         'fetch_status': spec_status,
                         'declared_only': True,
                         'definitions': public_capabilities(spec) if spec is not None else cached_capabilities(cached_spec)},
        'observation': safe_observation(observation),
        'limits': ['声明存在不代表 MijiaDeck 已实现或实机验证；事件定义不代表已收到事件。',
                   '未采集当前属性值、历史事件、媒体地址或摄像头画面；未执行任何设备动作。',
                   '未尝试视频连接时，无法仅凭 MIoT 规格确定实际视频传输协议。'],
    }
    # Invalid/untrusted role strings are never copied from model metadata.
    data['device']['kinds'] = [x for x in data['device']['kinds'] if identifier(x, 64)]
    content = json.dumps(data, ensure_ascii=False, indent=2).replace('`', '\\u0060')
    return ('### MijiaDeck 设备诊断报告\n\n'
            '请补充：想实现的功能／操作步骤／实际现象／米家 APP 中是否正常。\n\n'
            '```json\n' + content + '\n```\n')
