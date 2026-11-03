#!/usr/bin/env python3
"""Local AIRMX Pro registration, telemetry, and signed manual control service (stdlib).

This is a deliberately scoped MQTT 3.1/3.1.1 endpoint, not a general broker.
MQTT credentials are unknown: trust is limited to fixed source IPs and topics.
A CONNECT password is considered as a signing-key candidate only in memory,
and is persisted only if a fan status signature verifies with it. Fan controls
are limited to a signed manual airflow command through protected APIs; real sensor
telemetry can be relayed only to an already subscribed fan.
Upstream accepts the supplied MAC unchanged; it establishes no byte reversal.
"""
import argparse
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import tempfile
import threading
import time
import urllib.parse

FAN_IP = '192.0.2.20'
SENSOR_IP = '192.0.2.21'
HA_IP = '192.0.2.30'
FAN_MAC = '020000000020'
DEVICE_ID = 10001
COMMAND_TOPIC = f'airmx/01/1/1/0/1/{DEVICE_ID}'
FAN_STATUS_TOPIC = f'airmx/01/0/1/1/1/{DEVICE_ID}'
FAN_STATUS_TOPICS = {f'airmx/01/{a}/{b}/1/1/{DEVICE_ID}' for a in (0, 1) for b in (0, 1)}
SENSOR_TOPIC = f'airmx/01/1/0/1/1/{DEVICE_ID}'
SENSOR_SUBSCRIPTIONS = {SENSOR_TOPIC, f'airmx/01/+/1/+/+/{DEVICE_ID}'}
FAN_SUBSCRIPTIONS = {f'airmx/01/1/+/+/+/{DEVICE_ID}',
                     f'airmx/01/+/+/1/+/{DEVICE_ID}', COMMAND_TOPIC, SENSOR_TOPIC}
MAX_PACKET = 65536
MAX_CONTROL_STATUS_AGE = 360  # Fan reports signed state about every five minutes.


def mqtt_topic_matches(topic_filter, topic):
    """Match MQTT single-level and multi-level wildcards for a known topic."""
    filters, levels = topic_filter.split('/'), topic.split('/')
    for index, part in enumerate(filters):
        if part == '#':
            return index == len(filters) - 1
        if index >= len(levels) or (part != '+' and part != levels[index]):
            return False
    return len(filters) == len(levels)


def client_subscribed_to(client, topic):
    return bool(client and any(mqtt_topic_matches(topic_filter, topic)
                               for topic_filter in client.subscriptions))


def log(event, **fields):
    # Callers only supply fixed event names, fixed topics, and numeric metadata.
    print(json.dumps({'ts': int(time.time()), 'event': event, **fields}), flush=True)


def normalize_mac(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F:-]+', value):
        raise ValueError('invalid MAC')
    result = value.replace(':', '').replace('-', '').lower()
    if not re.fullmatch(r'[0-9a-f]{12}', result):
        raise ValueError('invalid MAC')
    return result


def valid_key(value):
    return isinstance(value, str) and 1 <= len(value) <= 256 and all(32 <= ord(c) <= 126 for c in value)


class KeyStore:
    """Single known device; disk errors fail closed and never erase the old key."""
    def __init__(self, data_dir):
        self.directory = Path(os.path.abspath(data_dir))
        self.path = self.directory / 'device.json'
        self.lock = threading.RLock()
        self._record = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            pass
        self._check(self.directory, directory=True)
        if self.path.exists() or self.path.is_symlink():
            self._load()

    @staticmethod
    def _check(path, directory=False):
        info = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        mode = 0o700 if directory else 0o600
        if not expected(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError('key store must be owned by this user with protected permissions')

    def _load(self):
        self._check(self.path)
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('invalid key store')
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get('mac') != FAN_MAC or record.get('deviceId') != DEVICE_ID or not valid_key(record.get('key')):
            raise ValueError('invalid key store')
        self._record = record

    @property
    def registered(self):
        with self.lock:
            return self._record is not None

    def key(self):
        with self.lock:
            return self._record['key'] if self._record else None

    def register(self, mac, key):
        if normalize_mac(mac) != FAN_MAC or not valid_key(key):
            raise ValueError('invalid registration')
        record = {'mac': FAN_MAC, 'deviceId': DEVICE_ID, 'key': key}
        with self.lock:
            self._check(self.directory, directory=True)
            if self.path.exists() or self.path.is_symlink():
                self._check(self.path)
            fd, temporary = tempfile.mkstemp(prefix='.device-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(record, stream, separators=(',', ':'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                self._record = record
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return DEVICE_ID


def load_api_token(path):
    """Read a dedicated LAN API token from protected storage without logging it."""
    token_path = Path(path)
    info = token_path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('API token file must be owned by this user with mode 0600')
    token = token_path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
        raise ValueError('invalid API token')
    return token


def compact(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sign_text(body, key):
    return hashlib.md5((body + ',' + key).encode('utf-8')).hexdigest()


def status_request(key, frequency=2, duration=10, timestamp=None):
    if type(frequency) is not int or type(duration) is not int or not (1 <= frequency <= 60 and 1 <= duration <= 60):
        raise ValueError('frequency and duration must be integers in 1..60 seconds')
    message = {'cmdId': 40, 'name': 'instantPush', 'time': int(time.time()) if timestamp is None else timestamp,
               'from': 4, 'data': {'frequencyTime': frequency, 'durationTime': duration}}
    message['sig'] = sign_text(compact(message)[1:-1], key)
    return compact(message).encode('utf-8')


def fan_control_request(key, current, mode, cadr, power=None, timestamp=None):
    if type(mode) is not int or mode not in (0, 1, 2, 3):
        raise ValueError('fan mode must be an integer in 0..3')
    if type(cadr) is not int or not 0 <= cadr <= 100:
        raise ValueError('fan speed must be an integer in 0..100')
    if not isinstance(current, dict):
        raise ValueError('verified fan status required')
    if any(type(current.get(name)) is not int for name in ('power', 'heatStatus', 'denoise')):
        raise ValueError('incomplete fan status')
    if any(current[name] not in (0, 1) for name in ('power', 'heatStatus', 'denoise')):
        raise ValueError('invalid fan status')
    selected_power = current['power'] if power is None else power
    if type(selected_power) is not int or selected_power not in (0, 1):
        raise ValueError('invalid fan power')
    data = {'power': selected_power, 'heatStatus': current['heatStatus'], 'mode': mode,
            'cadr': cadr, 'denoise': current['denoise']}
    message = {'cmdId': 100, 'name': 'control', 'time': int(time.time()) if timestamp is None else timestamp,
               'from': 4, 'data': data}
    message['sig'] = sign_text(compact(message)[1:-1], key)
    return compact(message).encode('utf-8')


def fan_speed_request(key, current, percent, timestamp=None):
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError('fan speed must be an integer in 0..100')
    return fan_control_request(key, current, mode=0, cadr=percent, power=1, timestamp=timestamp)


def parse_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('duplicate JSON field')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def verify_status(raw, key):
    # Preserve original bytes/whitespace/order, as upstream Signer.signText does.
    text = raw.decode('utf-8')
    value = parse_json(text)
    if not isinstance(value, dict) or value.get('cmdId') != 210 or value.get('name') != 'eagleStatus' or value.get('from') != 2 or not isinstance(value.get('data'), dict):
        raise ValueError('not eagle status')
    marker = text.rfind(',"sig"')
    if not text.startswith('{') or not text.endswith('}') or marker < 0 or list(value)[-1] != 'sig':
        raise ValueError('unsupported signature framing')
    signature = value.get('sig')
    if not isinstance(signature, str) or not hmac.compare_digest(sign_text(text[1:marker], key), signature):
        raise ValueError('invalid signature')
    return value


NUMERIC_FIELDS = set('power mode status denoise heatStatus cadr prm diffPressure1 diffPressure2 t0 g4Percent carbonPercent hepaPercent battery co2 co2_time h oh opm100 opm25 ot pm100 pm25 pm250 pm50 pm_time t time tvoc tvoc_duration tvoc_time temperature humidity value sensorType'.split())
TEXT_FIELDS = {'version', 'battery_state', 'co2_state', 'humi_state', 'pm_state', 'temp_state', 'temp_unit', 'tvoc_state', 'tvoc_unit', 'version_type', 'type', 'name', 'unit'}
SAFE_LABELS = set('charging discharge sampling c C % ppm ppb release battery co2 h pm100 pm25 pm250 pm50 t temperature humidity pm2.5 pm10'.split())
MEASUREMENT_FIELDS = {'timestamp', 'battery', 'temperature', 'humidity', 'pm25', 'co2', 'pm10'}


def sanitize_telemetry(value, depth=0):
    """Only recognized measurement fields, safe labels, and version numbers leave memory."""
    if depth > 5 or not isinstance(value, dict):
        return {}
    safe = {}
    for name, item in value.items():
        if name in NUMERIC_FIELDS and type(item) in (int, float) and abs(item) <= 1e12:
            safe[name] = item
        elif name in TEXT_FIELDS and isinstance(item, str) and (item in SAFE_LABELS or (name == 'version' and re.fullmatch(r'\d+(?:\.\d+){1,3}', item))):
            safe[name] = item
        elif name in MEASUREMENT_FIELDS and isinstance(item, dict):
            safe[name] = sanitize_telemetry(item, depth + 1)
        elif name == 'sensorData' and isinstance(item, list):
            safe[name] = [sanitize_telemetry(entry, depth + 1) for entry in item[:64] if isinstance(entry, dict)]
    return safe


class RecoveryState:
    def __init__(self, store, fan_ip=FAN_IP, sensor_ip=SENSOR_IP, request_on_subscribe=False, frequency=2, duration=10, relay_sensor=False):
        if fan_ip == sensor_ip:
            raise ValueError('fan and sensor addresses must differ')
        ipaddress.IPv4Address(fan_ip)
        ipaddress.IPv4Address(sensor_ip)
        self.store, self.fan_ip, self.sensor_ip = store, fan_ip, sensor_ip
        self.request_on_subscribe = request_on_subscribe
        self.frequency, self.duration = frequency, duration
        self.relay_sensor = relay_sensor
        self.lock = threading.RLock()
        self.status_condition = threading.Condition(self.lock)
        self.clients = {}
        self.latest_status = None
        self.latest_sensor = None
        self.last_request = None
        self.auto_requested = False
        self.last_control = None

    def http_get(self, source, target):
        if source not in (self.fan_ip, self.sensor_ip):
            return 403, {'error': 'source denied'}
        if len(target) > 8192:
            return 414, {'error': 'request too long'}
        try:
            url = urllib.parse.urlsplit(target)
        except ValueError:
            return 400, {'error': 'invalid request target'}
        if url.path == '/gettime':
            return 200, {'time': int(time.time())}
        if url.path != '/eagle':
            return 404, {'error': 'not found'}
        if source != self.fan_ip:
            return 403, {'error': 'source denied'}
        try:
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True, max_num_fields=20)
            if any(len(v) != 1 for v in query.values()):
                raise ValueError('duplicate parameter')
            operation = query.get('path', [''])[0]
            if operation == 'eagle/GET/genId':
                params = parse_json(query.get('params', ['{}'])[0])
                if not isinstance(params, dict):
                    raise ValueError('invalid parameters')
                self.store.register(params.get('mac'), params.get('key'))
                log('registration_completed', device_id=DEVICE_ID)
                self.maybe_request_status()
                return 200, {'status': 200, 'data': {'eagleId': DEVICE_ID}}
            if operation == 'eagle/GET/online':
                # Reject a supplied mismatching ID, but tolerate absent/empty upstream fields.
                supplied_id = query.get('eagleId', [''])[0]
                if supplied_id not in ('', str(DEVICE_ID)):
                    return 403, {'error': 'device denied'}
                # Match the upstream mock: this endpoint is a pairing declaration, not
                # an authorization gate. Returning 409 here can abort provisioning before
                # firmware makes its subsequent genId request.
                log('online_simulated_pairing_declaration', device_id=DEVICE_ID, snow=1, eagle=1,
                    registered=self.store.registered)
                return 200, {'status': 200, 'data': {'snow': 1, 'eagle': 1}}
            return 404, {'error': 'not found'}
        except (ValueError, TypeError, UnicodeError):
            return 400, {'error': 'invalid registration request'}
        except OSError:
            log('registration_storage_failed')
            return 503, {'error': 'protected storage unavailable'}

    def snapshot(self):
        with self.lock:
            return {'deviceId': DEVICE_ID, 'registered': self.store.registered,
                    'fan_connected': self.fan_ip in self.clients, 'sensor_connected': self.sensor_ip in self.clients,
                    'status': self.latest_status, 'sensor': self.latest_sensor,
                    'last_status_request': self.last_request, 'sensor_relay_enabled': self.relay_sensor,
                    'mqtt_authentication': 'source IP and topic allowlist; credentials unverified'}

    def request_status(self, frequency=None, duration=None):
        with self.lock:
            key = self.store.key()
            client = self.clients.get(self.fan_ip)
            if not key:
                raise ValueError('local registration required')
            if not client_subscribed_to(client, COMMAND_TOPIC):
                raise ValueError('verified fan subscription required')
            payload = status_request(key, self.frequency if frequency is None else frequency,
                                     self.duration if duration is None else duration)
            client.send(0x30, utf(COMMAND_TOPIC) + payload)
            self.last_request = int(time.time())
        log('status_request_sent', device_id=DEVICE_ID, cmd_id=40, payload_length=len(payload))
        return {'sent': True, 'cmdId': 40, 'deviceId': DEVICE_ID}

    def _fresh_status_for_control(self, refresh_timeout=6):
        with self.lock:
            if not self.store.registered:
                raise ValueError('local registration required')
            before = self.latest_status
            age = int(time.time()) - before.get('received_at', 0) if before else None
            fresh = bool(before and before.get('signature_verified') and
                         age is not None and 0 <= age <= MAX_CONTROL_STATUS_AGE)
        if fresh:
            status = before
        else:
            self.request_status(frequency=1, duration=5)
            with self.status_condition:
                updated = self.status_condition.wait_for(
                    lambda: self.latest_status is not before and
                    bool(self.latest_status and self.latest_status.get('signature_verified')),
                    timeout=refresh_timeout)
                if not updated:
                    raise ValueError('fresh signed fan status not received')
                status = self.latest_status
        return status

    def _send_control(self, payload, command, **fields):
        with self.lock:
            now = time.monotonic()
            if self.last_control is not None and now - self.last_control < 5:
                raise ValueError('fan control rate limit')
            client = self.clients.get(self.fan_ip)
            # The device subscribes to an allowlisted wildcard covering the
            # command topic rather than necessarily subscribing to that exact
            # topic. Keep the same proof requirement as request_status().
            if not client_subscribed_to(client, COMMAND_TOPIC):
                raise ValueError('verified fan command subscription required')
            client.send(0x30, utf(COMMAND_TOPIC) + payload)
            self.last_control = now
        log('fan_control_sent', device_id=DEVICE_ID, command=command, **fields)
        return {'sent': True, 'command': command, **fields, 'deviceId': DEVICE_ID}

    def set_speed(self, percent, refresh_timeout=6):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError('fan speed must be an integer in 0..100')
        status = self._fresh_status_for_control(refresh_timeout)
        payload = fan_speed_request(self.store.key(), status.get('data'), percent)
        return self._send_control(payload, 'set_speed', percent=percent)

    def set_mode(self, mode, refresh_timeout=6):
        if type(mode) is not int or mode not in (0, 1, 2, 3):
            raise ValueError('fan mode must be an integer in 0..3')
        status = self._fresh_status_for_control(refresh_timeout)
        current = status.get('data')
        if not isinstance(current, dict) or type(current.get('cadr')) is not int:
            raise ValueError('complete signed fan status required')
        payload = fan_control_request(self.store.key(), current, mode=mode, cadr=current['cadr'])
        return self._send_control(payload, 'set_mode', mode=mode)

    def maybe_request_status(self):
        # Registration can occur before OR after the observed MQTT subscription.
        with self.lock:
            if not self.request_on_subscribe or self.auto_requested:
                return
            client = self.clients.get(self.fan_ip)
            if not self.store.registered or not client_subscribed_to(client, COMMAND_TOPIC):
                return
            try:
                self.request_status()
            except (OSError, ValueError):
                log('automatic_status_request_unavailable')
            else:
                self.auto_requested = True

    def ingest(self, source, topic, raw, candidate_key=None):
        if source == self.fan_ip and topic in FAN_STATUS_TOPICS:
            key = self.store.key()
            if key:
                value = verify_status(raw, key)
            else:
                # Recover a key only when a fan-originated status frame proves
                # that the in-memory MQTT password candidate is correct.
                if not candidate_key:
                    raise ValueError('registration required')
                value = verify_status(raw, candidate_key)
                self.store.register(FAN_MAC, candidate_key)
                log('registration_completed_from_verified_mqtt_password', device_id=DEVICE_ID)
            status = {'received_at': int(time.time()), 'signature_verified': True, 'data': sanitize_telemetry(value['data'])}
            with self.status_condition:
                self.latest_status = status
                self.status_condition.notify_all()
            log('fan_status_verified', cmd_id=210, payload_length=len(raw))
        elif source == self.sensor_ip and topic == SENSOR_TOPIC:
            value = parse_json(raw)
            if not isinstance(value, dict) or str(value.get('type')) != '12' or not isinstance(value.get('sensorData'), list):
                raise ValueError('unsupported sensor message')
            with self.lock:
                self.latest_sensor = {'received_at': int(time.time()), 'signature_verified': False, 'data': sanitize_telemetry(value)}
            log('sensor_telemetry_received', payload_length=len(raw))
            self.relay_sensor_update(topic, raw)
        else:
            raise ValueError('publish denied')

    def relay_sensor_update(self, topic, raw):
        if not self.relay_sensor:
            return False
        with self.lock:
            client = self.clients.get(self.fan_ip)
            subscribed = client_subscribed_to(client, topic)
        if not subscribed:
            return False
        try:
            client.send(0x30, utf(topic) + raw)  # Forward only the real sensor's original QoS 0 payload.
        except OSError:
            log('sensor_telemetry_relay_failed', payload_length=len(raw))
            return False
        log('sensor_telemetry_relayed', payload_length=len(raw))
        return True


def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError
        result.extend(part)
    return bytes(result)


def encode_remaining(length):
    if not 0 <= length <= MAX_PACKET:
        raise ValueError('packet too large')
    result = bytearray()
    while True:
        digit = length % 128
        length //= 128
        result.append(digit | (128 if length else 0))
        if not length:
            return bytes(result)


def recv_packet(sock):
    header = recv_exact(sock, 1)[0]
    length = 0
    for index in range(4):
        digit = recv_exact(sock, 1)[0]
        length += (digit & 127) << (index * 7)
        if length > MAX_PACKET:
            raise ValueError('packet too large')
        if not digit & 128:
            if index and digit == 0:
                raise ValueError('noncanonical remaining length')
            return header, recv_exact(sock, length)
    raise ValueError('invalid remaining length')


def utf(value):
    raw = value.encode('utf-8')
    return len(raw).to_bytes(2, 'big') + raw


def read_field(body, offset, binary=False):
    if offset + 2 > len(body):
        raise ValueError('truncated field')
    length = int.from_bytes(body[offset:offset + 2], 'big')
    end = offset + 2 + length
    if end > len(body):
        raise ValueError('truncated field')
    value = body[offset + 2:end]
    if not binary:
        value = value.decode('utf-8')
        if '\x00' in value:
            raise ValueError('invalid UTF8 field')
    return value, end


def packet_id(body):
    if len(body) != 2 or body == b'\0\0':
        raise ValueError('invalid packet ID')
    return body


class MQTTHandler(socketserver.BaseRequestHandler):
    def send(self, header, body=b''):
        with self.send_lock:
            self.request.sendall(bytes([header]) + encode_remaining(len(body)) + body)

    def handle(self):
        self.send_lock = threading.Lock()
        self.subscriptions = set()
        self.pending = {}
        self.completed = set()
        self.candidate_signing_key = None
        source = self.client_address[0]
        state = self.server.state
        connected = False
        self.request.settimeout(10)
        if source not in (state.fan_ip, state.sensor_ip):
            log('mqtt_source_denied')
            return
        try:
            header, body = recv_packet(self.request)
            if header != 0x10:
                raise ValueError('CONNECT required')
            protocol, offset = read_field(body, 0)
            if offset + 4 > len(body):
                raise ValueError('truncated CONNECT')
            level, flags = body[offset:offset + 2]
            keepalive = int.from_bytes(body[offset + 2:offset + 4], 'big')
            if (protocol, level) not in (('MQTT', 4), ('MQIsdp', 3)) or flags & 1 or flags & 0x40 and not flags & 0x80:
                raise ValueError('unsupported CONNECT')
            will = bool(flags & 4)
            if ((flags >> 3) & 3) == 3 or not will and flags & 0x38:
                raise ValueError('invalid will flags')
            _, offset = read_field(body, offset + 4)  # client ID stays private
            if will:
                _, offset = read_field(body, offset)
                _, offset = read_field(body, offset, binary=True)  # Never relay wills.
            if flags & 0x80:
                _, offset = read_field(body, offset)
            if flags & 0x40:
                password, offset = read_field(body, offset, binary=True)
                try:
                    candidate = password.decode('ascii')
                except UnicodeDecodeError:
                    candidate = None
                if valid_key(candidate):
                    self.candidate_signing_key = candidate
            if offset != len(body):
                raise ValueError('invalid CONNECT length')
            self.request.settimeout(keepalive * 1.5 if keepalive else None)
            with state.lock:
                previous = state.clients.get(source)
                state.clients[source] = self
                if previous:
                    try:
                        previous.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            connected = True
            self.send(0x20, b'\0\0')
            log('mqtt_connected', role='fan' if source == state.fan_ip else 'sensor', keepalive=keepalive,
                username_present=bool(flags & 0x80), password_present=bool(flags & 0x40))
            while True:
                header, body = recv_packet(self.request)
                kind = header >> 4
                if kind == 8 and header == 0x82:
                    if len(body) < 5:
                        raise ValueError('empty SUBSCRIBE')
                    identifier = packet_id(body[:2])
                    offset, grants = 2, []
                    while offset < len(body):
                        topic, offset = read_field(body, offset)
                        if offset >= len(body) or body[offset] > 2:
                            raise ValueError('invalid subscription')
                        allowed = topic in (FAN_SUBSCRIPTIONS if source == state.fan_ip else SENSOR_SUBSCRIPTIONS)
                        grants.append(0 if allowed else 128)  # outbound messages are always QoS0
                        if allowed:
                            self.subscriptions.add(topic)
                        offset += 1
                    self.send(0x90, identifier + bytes(grants))
                    log('mqtt_subscriptions_checked', accepted=sum(q == 0 for q in grants), rejected=sum(q == 128 for q in grants))
                    if source == state.fan_ip:
                        state.maybe_request_status()
                elif kind == 3:
                    qos = (header >> 1) & 3
                    if qos == 3 or (qos == 0 and header & 8):
                        raise ValueError('invalid PUBLISH flags')
                    topic, offset = read_field(body, 0)
                    identifier = packet_id(body[offset:offset + 2]) if qos else None
                    offset += 2 if qos else 0
                    raw = body[offset:]
                    if topic not in (FAN_STATUS_TOPICS if source == state.fan_ip else {SENSOR_TOPIC}):
                        raise ValueError('publish topic denied')
                    if qos == 2:
                        if identifier not in self.pending:
                            if len(self.pending) >= 64:
                                raise ValueError('too many pending publishes')
                            self.completed.discard(identifier)
                            self.pending[identifier] = (topic, raw)
                        elif self.pending[identifier] != (topic, raw):
                            raise ValueError('conflicting QoS2 duplicate')
                        self.send(0x50, identifier)
                    else:
                        self.ingest(state, source, topic, raw,
                                    self.candidate_signing_key if source == state.fan_ip else None)
                        if qos == 1:
                            self.send(0x40, identifier)
                elif header == 0x62:
                    identifier = packet_id(body)
                    entry = self.pending.pop(identifier, None)
                    if entry:
                        self.ingest(state, source, *entry,
                                    self.candidate_signing_key if source == state.fan_ip else None)
                        self.completed.add(identifier)
                        if len(self.completed) > 64:
                            self.completed.clear()
                    self.send(0x70, identifier)
                elif header == 0xc0 and not body:
                    self.send(0xd0)
                elif header == 0xa2:
                    identifier = packet_id(body[:2])
                    offset = 2
                    if len(body) <= offset:
                        raise ValueError('empty UNSUBSCRIBE')
                    while offset < len(body):
                        topic, offset = read_field(body, offset)
                        self.subscriptions.discard(topic)
                    self.send(0xb0, identifier)
                elif header == 0xe0 and not body:
                    return
                else:
                    raise ValueError('unsupported packet')
        except (EOFError, OSError):
            pass
        except (ValueError, UnicodeError):
            log('mqtt_protocol_rejected')
        finally:
            if connected:
                with state.lock:
                    if state.clients.get(source) is self:
                        del state.clients[source]
                log('mqtt_closed', role='fan' if source == state.fan_ip else 'sensor')

    @staticmethod
    def ingest(state, source, topic, raw, candidate_key=None):
        try:
            state.ingest(source, topic, raw, candidate_key=candidate_key)
        except (ValueError, UnicodeError):
            log('mqtt_payload_rejected', payload_length=len(raw))


class MQTTServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, MQTTHandler)

    def server_close(self):
        with self.state.lock:
            for client in list(self.state.clients.values()):
                try:
                    client.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        super().server_close()


class HTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Base handler would leak genId query secrets.

    def respond(self, code, value):
        body = compact(value).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {'error': 'request rejected'})

    def local_admin_allowed(self):
        expected_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return (self.client_address[0] == '127.0.0.1' and self.headers.get('Host') in expected_hosts
                and self.headers.get('Origin') is None)

    def local_api_allowed(self):
        expected_host = f'{self.server.server_address[0]}:{self.server.server_port}'
        authorization = self.headers.get('Authorization', '')
        return (self.server.api and self.client_address[0] == self.server.api_client_ip
                and self.headers.get('Host') == expected_host and self.headers.get('Origin') is None
                and hmac.compare_digest(authorization, f'Bearer {self.server.api_token}'))

    def do_GET(self):
        if self.server.api:
            if not self.local_api_allowed():
                return self.respond(403, {'error': 'forbidden'})
            if self.path == '/api/status':
                return self.respond(200, self.server.state.snapshot())
            return self.respond(404, {'error': 'not found'})
        if self.server.admin:
            if not self.local_admin_allowed():
                return self.respond(403, {'error': 'loopback required'})
            if self.path == '/health':
                return self.respond(200, {'ready': True, 'key_present': self.server.state.store.registered})
            if self.path == '/status':
                return self.respond(200, self.server.state.snapshot())
            return self.respond(404, {'error': 'not found'})
        code, value = self.server.state.http_get(self.client_address[0], self.path)
        route = 'other'
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == '/gettime':
                route = 'gettime'
            elif parsed.path == '/eagle':
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
                operation = query.get('path', [''])[0]
                if operation == 'eagle/GET/genId':
                    route = 'genId'
                elif operation == 'eagle/GET/online':
                    route = 'online'
                else:
                    route = 'eagle_other'
        except (ValueError, TypeError):
            route = 'invalid'
        source = self.client_address[0]
        role = 'fan' if source == self.server.state.fan_ip else 'sensor' if source == self.server.state.sensor_ip else 'other'
        log('http_device_request', role=role, route=route, status=code)
        self.respond(code, value)

    def do_POST(self):
        is_admin = (self.server.admin and self.local_admin_allowed()
                    and self.path in ('/request-status', '/set-speed', '/set-mode')
                    and self.headers.get('X-Airmx-Local') == '1')
        is_api = (self.server.api and self.local_api_allowed()
                  and self.path in ('/api/set-speed', '/api/set-mode'))
        if not (is_admin or is_api):
            return self.respond(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 1024 or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid length')
            options = parse_json(self.rfile.read(length)) if length else {}
            if not isinstance(options, dict):
                raise ValueError('invalid options')
            if self.path == '/request-status':
                if set(options) - {'frequency', 'duration'}:
                    raise ValueError('invalid options')
                result = self.server.state.request_status(**options)
            elif self.path.endswith('/set-mode'):
                if set(options) != {'mode'}:
                    raise ValueError('invalid options')
                result = self.server.state.set_mode(options['mode'])
            else:
                if set(options) != {'percent'}:
                    raise ValueError('invalid options')
                result = self.server.state.set_speed(options['percent'])
            self.respond(200, result)
        except (ValueError, TypeError):
            self.respond(409, {'error': 'registration, subscription, and bounded timing required'})
        except OSError:
            self.respond(503, {'error': 'fan unavailable'})


class HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state, admin=False, api_token=None, api_client_ip=HA_IP):
        if admin and address[0] != '127.0.0.1':
            raise ValueError('admin must bind IPv4 loopback')
        if api_token is not None:
            ipaddress.IPv4Address(api_client_ip)
            bind_ip = ipaddress.IPv4Address(address[0])
            if bind_ip.is_unspecified or not valid_key(api_token):
                raise ValueError('LAN API requires a specific bind address and protected token')
        self.state, self.admin = state, admin
        self.api, self.api_token, self.api_client_ip = api_token is not None, api_token, api_client_ip
        super().__init__(address, HTTPHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='serve registration and MQTT; signed manual fan control is available only through protected APIs')
    serve.add_argument('--data-dir', required=True, help='persistent protected directory; retain across restarts (0700, device.json 0600)')
    serve.add_argument('--bind', default='192.0.2.1')
    serve.add_argument('--fan-ip', default=FAN_IP, help='expected fan source address; exact allowlist')
    serve.add_argument('--sensor-ip', default=SENSOR_IP, help='expected sensor source address; must differ from fan')
    serve.add_argument('--http-port', type=int, default=25880)
    serve.add_argument('--mqtt-port', type=int, default=25884)
    serve.add_argument('--admin-port', type=int, default=25886, help='IPv4 loopback only; 0 disables admin')
    serve.add_argument('--api-port', type=int, default=0, help='LAN REST API port; disabled unless explicitly configured')
    serve.add_argument('--api-token-file', help='protected bearer-token file (0600), required with --api-port')
    serve.add_argument('--api-client-ip', default=HA_IP, help='only this client address may use the LAN REST API')
    serve.add_argument('--request-status-on-subscribe', action='store_true', help='opt in to one signed cmdId40 after registration and verified subscription per process')
    serve.add_argument('--relay-sensor', action='store_true', help='forward only real sensor telemetry to an already-subscribed fan')
    serve.add_argument('--frequency', type=int, default=2, choices=range(1, 61), metavar='1..60')
    serve.add_argument('--duration', type=int, default=10, choices=range(1, 61), metavar='1..60')
    for name in ('status', 'request-status', 'set-speed', 'set-mode'):
        command = sub.add_parser(name, help='query sanitized state or send an allowlisted signed command through local admin')
        command.add_argument('--admin-port', type=int, default=25886)
        if name == 'request-status':
            command.add_argument('--frequency', type=int, default=2, choices=range(1, 61), metavar='1..60')
            command.add_argument('--duration', type=int, default=10, choices=range(1, 61), metavar='1..60')
        elif name == 'set-speed':
            command.add_argument('--percent', type=int, required=True, choices=range(0, 101))
        elif name == 'set-mode':
            command.add_argument('--mode', type=int, required=True, choices=range(0, 4))
    args = parser.parse_args()
    if args.command != 'serve':
        # set-speed may wait up to six seconds for a fresh signed status.
        connection = http.client.HTTPConnection('127.0.0.1', args.admin_port, timeout=12)
        try:
            if args.command == 'status':
                connection.request('GET', '/status')
            elif args.command == 'request-status':
                connection.request('POST', '/request-status', body=compact({'frequency': args.frequency, 'duration': args.duration}), headers={'Content-Type': 'application/json', 'X-Airmx-Local': '1'})
            elif args.command == 'set-mode':
                connection.request('POST', '/set-mode', body=compact({'mode': args.mode}), headers={'Content-Type': 'application/json', 'X-Airmx-Local': '1'})
            else:
                connection.request('POST', '/set-speed', body=compact({'percent': args.percent}), headers={'Content-Type': 'application/json', 'X-Airmx-Local': '1'})
            response = connection.getresponse()
            print(response.read().decode('utf-8'))
            return 0 if response.status == 200 else 1
        except OSError:
            parser.exit(1, 'Local admin unavailable.\n')
        finally:
            connection.close()
    servers = []
    started = []
    try:
        store = KeyStore(args.data_dir)
        state = RecoveryState(store, fan_ip=args.fan_ip, sensor_ip=args.sensor_ip,
                              request_on_subscribe=args.request_status_on_subscribe,
                              frequency=args.frequency, duration=args.duration,
                              relay_sensor=args.relay_sensor)
        servers.append(HTTPServer((args.bind, args.http_port), state))
        servers.append(MQTTServer((args.bind, args.mqtt_port), state))
        if args.admin_port:
            servers.append(HTTPServer(('127.0.0.1', args.admin_port), state, admin=True))
        if args.api_port:
            if not args.api_token_file:
                raise ValueError('--api-token-file is required when --api-port is enabled')
            servers.append(HTTPServer((args.bind, args.api_port), state,
                                      api_token=load_api_token(args.api_token_file),
                                      api_client_ip=args.api_client_ip))
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            started.append(server)
        log('ready', device_id=DEVICE_ID, http_port=args.http_port, mqtt_port=args.mqtt_port,
            admin_port=args.admin_port, registered=store.registered,
            sensor_relay_enabled=state.relay_sensor,
            status_request_opt_in=args.request_status_on_subscribe)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        log('startup_failed')
        return 1
    finally:
        for server in servers:
            if server in started:
                server.shutdown()
            server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
