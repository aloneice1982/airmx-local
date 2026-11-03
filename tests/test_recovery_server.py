"""Offline tests: only temporary files, socketpairs, and IPv4 loopback sockets."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/airmx-local-recovery/scripts'))
import recovery_server as r

# Public upstream openairmx src/airmx.test.ts fixture, never a real device secret.
OBSERVED_SENSOR = {'type': '12', 'sensorData': [{'timestamp': {'value': 1700000000},
    'battery': {'value': 100}, 'temperature': {'value': 22.0, 'status': 2, 'unit': 'C'},
    'humidity': {'value': 45.0, 'status': 2, 'unit': '%'}, 'pm25': {'value': 7, 'status': 2},
    'co2': {'value': 551, 'status': 2}, 'pm10': {'value': 7, 'status': 2}}]}

PUBLIC_KEY = 'f0eb21fe346c88e1d1ac73546022cd5d'
PUBLIC_MESSAGE = b'{"cmdId": 210,"name":"eagleStatus","time":1752675701,"from":2,"data":{"version":"10.00.17","power":1,"heatStatus":0,"mode":0,"cadr":47,"prm":1320,"g4Percent": 100,"hepaPercent":100,"carbonId":"031","g4Id":"041","hepaId":"021","carbonPercent":17,"diffPressure1":99999,"diffPressure2":99999,"t0":35,"status":0,"denoise":1},"sig":"b8796682da77e8c929dddf7e6461afec"}'


def genid(mac=r.FAN_MAC, key=PUBLIC_KEY):
    return '/eagle?' + urllib.parse.urlencode({'path': 'eagle/GET/genId', 'params': json.dumps({'mac': mac, 'key': key})})


def signed_status(cadr):
    value = {'cmdId': 210, 'name': 'eagleStatus', 'time': 1752675701, 'from': 2,
             'data': {'cadr': cadr, 'key': 'PRIVATE-MARKER', 'version': '10.00.17'}}
    value['sig'] = r.sign_text(r.compact(value)[1:-1], PUBLIC_KEY)
    return r.compact(value).encode()


def signed_full_status():
    value = {'cmdId': 210, 'name': 'eagleStatus', 'time': 1752675701, 'from': 2,
             'data': {'version': '10.00.17', 'power': 1, 'heatStatus': 1, 'mode': 2,
                      'cadr': 35, 'denoise': 1}}
    value['sig'] = r.sign_text(r.compact(value)[1:-1], PUBLIC_KEY)
    return r.compact(value).encode()


def send(sock, header, body=b''):
    sock.sendall(bytes([header]) + r.encode_remaining(len(body)) + body)


def connect(sock, keepalive=0, password='CONNECT-PRIVATE-MARKER'):
    # Deliberately different password from signing key; it must not be retained.
    body = r.utf('MQTT') + bytes([4, 0xc2]) + keepalive.to_bytes(2, 'big') + r.utf('offline-test') + r.utf('test-user') + r.utf(password)
    send(sock, 0x10, body)
    return r.recv_packet(sock)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / 'protected'
        self.store = r.KeyStore(self.directory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_registration_permissions_persistence_and_stable_id(self):
        self.assertEqual(self.store.register('02:00:00:00:00:20', PUBLIC_KEY), 10001)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        restarted = r.KeyStore(self.directory)
        self.assertEqual(restarted.key(), PUBLIC_KEY)
        self.assertEqual(restarted.register('02-00-00-00-00-20', 'replacement-public-test-key'), 10001)
        self.assertEqual(r.KeyStore(self.directory).key(), 'replacement-public-test-key')

    def test_wrong_mac_and_reversed_bytes_fail_without_creating_key(self):
        for mac in ('200000000002', '020000000021', 'garbage', None):
            with self.assertRaises(ValueError):
                self.store.register(mac, PUBLIC_KEY)
        self.assertFalse(self.store.registered)
        self.assertFalse(self.store.path.exists())

    def test_write_failure_keeps_old_key_and_cleans_temporary(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        with mock.patch.object(r.os, 'replace', side_effect=OSError('injected failure')):
            with self.assertRaises(OSError):
                self.store.register(r.FAN_MAC, 'replacement-public-test-key')
        self.assertEqual(self.store.key(), PUBLIC_KEY)
        self.assertEqual(r.KeyStore(self.directory).key(), PUBLIC_KEY)
        self.assertEqual([p.name for p in self.directory.iterdir()], ['device.json'])

    def test_unsafe_or_corrupted_store_fails_closed(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        self.store.path.chmod(0o644)
        with self.assertRaises(ValueError):
            r.KeyStore(self.directory)
        self.store.path.chmod(0o600)
        self.store.path.write_text('{broken')
        with self.assertRaises(ValueError):
            r.KeyStore(self.directory)

    def test_symlink_store_rejected(self):
        self.store.path.symlink_to(Path(self.tmp.name) / 'missing')
        with self.assertRaises(ValueError):
            r.KeyStore(self.directory)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = r.KeyStore(Path(self.tmp.name) / 'protected')
        self.state = r.RecoveryState(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_http_allowlist_registration_online_and_unknown_routes(self):
        for source in (r.SENSOR_IP, '192.0.2.99'):
            self.assertEqual(self.state.http_get(source, genid())[0], 403)
        online = '/eagle?path=eagle/GET/online&eagleId=10001'
        # Upstream treats online as a simulated pairing declaration even before genId.
        # Rejecting this request can prevent the fan from proceeding to registration.
        code, result = self.state.http_get(r.FAN_IP, online)
        self.assertEqual((code, result['data']), (200, {'snow': 1, 'eagle': 1}))
        self.assertFalse(self.store.registered)
        self.assertEqual(self.state.http_get(r.FAN_IP, genid('200000000002'))[0], 400)
        code, result = self.state.http_get(r.FAN_IP, genid())
        self.assertEqual((code, result['data']['eagleId']), (200, 10001))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.state.http_get(r.FAN_IP, online)[0], 200)
        self.assertIn('online_simulated_pairing_declaration', output.getvalue())
        self.assertEqual(self.state.http_get(r.FAN_IP, '/eagle?path=eagle/GET/online&eagleId=1')[0], 403)
        for path in ('/exchange?device=10001', '/', '/gettime/else', '/eagle?path=unknown'):
            self.assertEqual(self.state.http_get(r.FAN_IP, path)[0], 404)
        self.assertIsInstance(self.state.http_get(r.SENSOR_IP, '/gettime')[1]['time'], int)

    def test_registration_storage_failure_returns_503_not_success(self):
        with mock.patch.object(self.store, 'register', side_effect=OSError('private marker')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code, result = self.state.http_get(r.FAN_IP, genid())
        self.assertEqual(code, 503)
        self.assertNotIn('private marker', output.getvalue())
        self.assertFalse(self.store.registered)

    def test_api_token_requires_protected_file(self):
        token_path = Path(self.tmp.name) / 'api-token'
        token_path.write_text('a' * 48, encoding='ascii')
        token_path.chmod(0o600)
        self.assertEqual(r.load_api_token(token_path), 'a' * 48)
        token_path.chmod(0o644)
        with self.assertRaises(ValueError):
            r.load_api_token(token_path)

    def test_duplicate_parameters_and_keys_rejected(self):
        self.assertEqual(self.state.http_get(r.FAN_IP, genid() + '&path=eagle/GET/genId')[0], 400)
        target = '/eagle?' + urllib.parse.urlencode({'path': 'eagle/GET/genId', 'params': '{"mac":"020000000020","key":"a","key":"b"}'})
        self.assertEqual(self.state.http_get(r.FAN_IP, target)[0], 400)
        self.assertFalse(self.store.registered)

    def test_upstream_public_signature_vector_and_tampering(self):
        self.assertEqual(r.verify_status(PUBLIC_MESSAGE, PUBLIC_KEY)['data']['cadr'], 47)
        with self.assertRaises(ValueError):
            r.verify_status(PUBLIC_MESSAGE.replace(b'"cadr":47', b'"cadr":48'), PUBLIC_KEY)
        # Re-serialization changes signed whitespace, so validation uses raw body.
        with self.assertRaises(ValueError):
            r.verify_status(r.compact(json.loads(PUBLIC_MESSAGE)).encode(), PUBLIC_KEY)

    def test_source_topic_signature_and_telemetry_sanitization(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        self.state.ingest(r.FAN_IP, r.FAN_STATUS_TOPIC, signed_status(42))
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['status']['data'], {'cadr': 42, 'version': '10.00.17'})
        for source, topic in ((r.SENSOR_IP, r.FAN_STATUS_TOPIC), (r.FAN_IP, r.FAN_STATUS_TOPIC.replace('10001', '1'))):
            with self.assertRaises(ValueError):
                self.state.ingest(source, topic, signed_status(22))
        sensor = {'type': '12', 'key': 'PRIVATE-MARKER', 'sensorData': [{'co2': 812, 'pm25': 9, 'key': 'PRIVATE-MARKER', 'name': 'PRIVATE-MARKER'}]}
        self.state.ingest(r.SENSOR_IP, r.SENSOR_TOPIC, json.dumps(sensor).encode())
        serialized = json.dumps(self.state.snapshot())
        self.assertNotIn('PRIVATE-MARKER', serialized)
        self.assertNotIn(PUBLIC_KEY, serialized)
        self.assertIn('812', serialized)
        self.assertFalse(self.state.snapshot()['sensor_relay_enabled'])

    def test_observed_sensor_nested_values_preserved_without_secrets(self):
        sample = json.loads(json.dumps(OBSERVED_SENSOR))
        sample['key'] = 'PRIVATE-MARKER'
        sample['sensorData'][0]['unknown'] = {'value': 123, 'secret': 'PRIVATE-MARKER'}
        sample['sensorData'][0]['temperature']['key'] = 'PRIVATE-MARKER'
        self.state.ingest(r.SENSOR_IP, r.SENSOR_TOPIC, json.dumps(sample).encode())
        result = self.state.snapshot()['sensor']['data']['sensorData'][0]
        self.assertEqual(result, OBSERVED_SENSOR['sensorData'][0])
        self.assertEqual(result['pm25']['value'], 7)
        self.assertEqual(result['co2']['value'], 551)
        self.assertNotIn('PRIVATE-MARKER', json.dumps(self.state.snapshot()))

    def test_protocol_analysis_topics_match_control_and_sensor(self):
        app_filter = f'airmx/01/+/+/1/+/{r.DEVICE_ID}'
        self.assertIn(app_filter, r.FAN_SUBSCRIPTIONS)
        self.assertTrue(r.mqtt_topic_matches(app_filter, r.SENSOR_TOPIC))
        self.assertFalse(r.mqtt_topic_matches(app_filter, r.COMMAND_TOPIC))
        self.assertTrue(r.mqtt_topic_matches(app_filter, r.FAN_STATUS_TOPIC))
        self.assertTrue(r.mqtt_topic_matches(r.COMMAND_TOPIC, r.COMMAND_TOPIC))

    def test_status_request_is_bounded_and_cmd40_only(self):
        payload = json.loads(r.status_request(PUBLIC_KEY, timestamp=1752675701))
        self.assertEqual(payload['cmdId'], 40)
        self.assertEqual(payload['from'], 4)
        self.assertEqual(payload['data'], {'frequencyTime': 2, 'durationTime': 10})
        signature = payload.pop('sig')
        self.assertEqual(signature, r.sign_text(r.compact(payload)[1:-1], PUBLIC_KEY))
        for frequency, duration in ((0, 10), (2, 61), (True, 10), (2, 2.5)):
            with self.assertRaises(ValueError):
                r.status_request(PUBLIC_KEY, frequency, duration)

    def test_fan_speed_request_signs_manual_speed_and_preserves_safety_state(self):
        payload = json.loads(r.fan_speed_request(PUBLIC_KEY,
            {'power': 1, 'heatStatus': 1, 'denoise': 1}, 35, timestamp=1752675701))
        self.assertEqual(payload['cmdId'], 100)
        self.assertEqual(payload['name'], 'control')
        self.assertEqual(payload['from'], 4)
        self.assertEqual(payload['data'], {'power': 1, 'heatStatus': 1, 'mode': 0, 'cadr': 35, 'denoise': 1})
        signature = payload.pop('sig')
        self.assertEqual(signature, r.sign_text(r.compact(payload)[1:-1], PUBLIC_KEY))
        for percent in (-1, 101, True):
            with self.assertRaises(ValueError):
                r.fan_speed_request(PUBLIC_KEY, {'power': 1, 'heatStatus': 0, 'denoise': 0}, percent)

    def test_fan_mode_request_uses_observed_topic_and_preserves_current_state(self):
        self.assertEqual(r.COMMAND_TOPIC, f'airmx/01/1/1/0/1/{r.DEVICE_ID}')
        payload = json.loads(r.fan_control_request(PUBLIC_KEY,
            {'power': 1, 'heatStatus': 1, 'denoise': 0}, mode=1, cadr=35,
            timestamp=1752675701))
        self.assertEqual((payload['cmdId'], payload['name'], payload['from']), (100, 'control', 4))
        self.assertEqual(payload['data'], {'power': 1, 'heatStatus': 1, 'mode': 1, 'cadr': 35, 'denoise': 0})
        signature = payload.pop('sig')
        self.assertEqual(signature, r.sign_text(r.compact(payload)[1:-1], PUBLIC_KEY))
        for mode in (-1, 4, True):
            with self.assertRaises(ValueError):
                r.fan_control_request(PUBLIC_KEY, {'power': 1, 'heatStatus': 0, 'denoise': 1}, mode, 35)

    def test_set_mode_reuses_recent_signed_status_and_only_changes_mode(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        self.state.latest_status = {'received_at': int(time.time()), 'signature_verified': True,
            'data': {'power': 1, 'heatStatus': 0, 'denoise': 1, 'cadr': 35, 'mode': 0}}
        client = mock.Mock(subscriptions={f'airmx/01/1/+/+/+/{r.DEVICE_ID}'})
        self.state.clients[self.state.fan_ip] = client
        result = self.state.set_mode(1)
        self.assertEqual(result, {'sent': True, 'command': 'set_mode', 'mode': 1, 'deviceId': r.DEVICE_ID})
        client.send.assert_called_once()
        _, payload = client.send.call_args.args
        topic, offset = r.read_field(payload, 0)
        command = json.loads(payload[offset:])
        self.assertEqual(topic, r.COMMAND_TOPIC)
        self.assertEqual(command['data'], {'power': 1, 'heatStatus': 0, 'mode': 1, 'cadr': 35, 'denoise': 1})

    def test_set_speed_reuses_recent_signed_state_for_normal_five_minute_report_interval(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        self.state.latest_status = {'received_at': int(time.time()) - 300, 'signature_verified': True,
            'data': {'power': 1, 'heatStatus': 0, 'denoise': 1, 'cadr': 35, 'mode': 0}}
        client = mock.Mock(subscriptions={f'airmx/01/1/+/+/+/{r.DEVICE_ID}'})
        self.state.clients[self.state.fan_ip] = client
        result = self.state.set_speed(40)
        client.send.assert_called_once()
        self.assertEqual(result['percent'], 40)
        _, payload = client.send.call_args.args
        _, offset = r.read_field(payload, 0)
        command = json.loads(payload[offset:])
        self.assertEqual(command['data'], {'power': 1, 'heatStatus': 0, 'mode': 0, 'cadr': 40, 'denoise': 1})


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = r.KeyStore(Path(self.tmp.name) / 'protected')
        self.state = r.RecoveryState(self.store, fan_ip='127.0.0.1', sensor_ip='127.0.0.2')
        self.server = r.MQTTServer(('127.0.0.1', 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.sock = socket.create_connection(self.server.server_address, timeout=2)
        self.sock.settimeout(2)

    def tearDown(self):
        self.sock.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()


    def test_registration_subscription_signed_query_and_qos012(self):
        self.assertEqual(connect(self.sock), (0x20, b'\0\0'))
        with self.assertRaises(ValueError):
            self.state.request_status()
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        send(self.sock, 0x82, b'\0\1' + r.utf('#') + b'\0')
        self.assertEqual(r.recv_packet(self.sock), (0x90, b'\0\1\x80'))
        with self.assertRaises(ValueError):
            self.state.request_status()
        send(self.sock, 0x82, b'\0\2' + r.utf(f'airmx/01/1/+/+/+/{r.DEVICE_ID}') + b'\2')
        self.assertEqual(r.recv_packet(self.sock), (0x90, b'\0\2\0'))
        self.assertTrue(self.state.request_status()['sent'])
        header, body = r.recv_packet(self.sock)
        topic, offset = r.read_field(body, 0)
        self.assertEqual((header, topic), (0x30, r.COMMAND_TOPIC))
        payload = json.loads(body[offset:])
        signature = payload.pop('sig')
        self.assertEqual(payload['cmdId'], 40)
        self.assertEqual(signature, r.sign_text(r.compact(payload)[1:-1], PUBLIC_KEY))
        # QoS0 has no acknowledgment; PINGRESP is a processing barrier.
        send(self.sock, 0x30, r.utf(r.FAN_STATUS_TOPIC) + signed_status(31))
        send(self.sock, 0xc0)
        self.assertEqual(r.recv_packet(self.sock), (0xd0, b''))
        self.assertEqual(self.state.snapshot()['status']['data']['cadr'], 31)
        send(self.sock, 0x32, r.utf(r.FAN_STATUS_TOPIC) + b'\0\3' + signed_status(32))
        self.assertEqual(r.recv_packet(self.sock), (0x40, b'\0\3'))
        self.assertEqual(self.state.snapshot()['status']['data']['cadr'], 32)
        message = r.utf(r.FAN_STATUS_TOPIC) + b'\0\4' + signed_status(33)
        send(self.sock, 0x34, message)
        self.assertEqual(r.recv_packet(self.sock), (0x50, b'\0\4'))
        send(self.sock, 0x3c, message)  # duplicate QoS2 PUBLISH
        self.assertEqual(r.recv_packet(self.sock), (0x50, b'\0\4'))
        self.assertEqual(self.state.snapshot()['status']['data']['cadr'], 32)
        send(self.sock, 0x62, b'\0\4')
        self.assertEqual(r.recv_packet(self.sock), (0x70, b'\0\4'))
        self.assertEqual(self.state.snapshot()['status']['data']['cadr'], 33)
        send(self.sock, 0x62, b'\0\4')
        self.assertEqual(r.recv_packet(self.sock), (0x70, b'\0\4'))
        serialized = json.dumps(self.state.snapshot())
        self.assertNotIn('PRIVATE-MARKER', serialized)
        self.assertNotIn(PUBLIC_KEY, serialized)

    def test_set_speed_refreshes_state_then_sends_signed_allowlisted_control(self):
        self.assertEqual(connect(self.sock), (0x20, b'\0\0'))
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        subscription = b'\0\1' + r.utf(f'airmx/01/1/+/+/+/{r.DEVICE_ID}') + b'\0'
        send(self.sock, 0x82, subscription)
        self.assertEqual(r.recv_packet(self.sock), (0x90, b'\0\1\0'))
        result = {}
        errors = []
        def set_speed():
            try:
                result.update(self.state.set_speed(60))
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=set_speed)
        worker.start()
        header, body = r.recv_packet(self.sock)
        topic, offset = r.read_field(body, 0)
        refresh = json.loads(body[offset:])
        self.assertEqual((header, topic, refresh['cmdId']), (0x30, r.COMMAND_TOPIC, 40))
        send(self.sock, 0x30, r.utf(r.FAN_STATUS_TOPIC) + signed_full_status())
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result['percent'], 60)
        header, body = r.recv_packet(self.sock)
        topic, offset = r.read_field(body, 0)
        control = json.loads(body[offset:])
        self.assertEqual((header, topic, control['cmdId']), (0x30, r.COMMAND_TOPIC, 100))
        self.assertEqual(control['data'], {'power': 1, 'heatStatus': 1, 'mode': 0, 'cadr': 60, 'denoise': 1})
        signature = control.pop('sig')
        self.assertEqual(signature, r.sign_text(r.compact(control)[1:-1], PUBLIC_KEY))

    def test_auto_request_opt_in_once_per_process_and_bad_subscription_rejected(self):
        self.state.request_on_subscribe = True
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        connect(self.sock)
        body = b'\0\1' + r.utf(r.COMMAND_TOPIC) + b'\0'
        send(self.sock, 0x82, body)
        self.assertEqual(r.recv_packet(self.sock)[0], 0x90)
        self.assertEqual(r.recv_packet(self.sock)[0], 0x30)
        send(self.sock, 0x82, body)
        self.assertEqual(r.recv_packet(self.sock)[0], 0x90)
        send(self.sock, 0xc0)
        self.assertEqual(r.recv_packet(self.sock), (0xd0, b''))

    def test_auto_request_when_registration_follows_existing_subscription(self):
        self.state.request_on_subscribe = True
        connect(self.sock)
        send(self.sock, 0x82, b'\0\1' + r.utf(r.COMMAND_TOPIC) + b'\0')
        self.assertEqual(r.recv_packet(self.sock)[0], 0x90)
        send(self.sock, 0xc0)
        self.assertEqual(r.recv_packet(self.sock), (0xd0, b''))
        code, _ = self.state.http_get('127.0.0.1', genid())
        self.assertEqual(code, 200)
        self.assertEqual(r.recv_packet(self.sock)[0], 0x30)
        self.state.http_get('127.0.0.1', genid())
        send(self.sock, 0xc0)
        self.assertEqual(r.recv_packet(self.sock), (0xd0, b''))

    def test_sensor_observed_subscription_and_publish_without_relay(self):
        state = r.RecoveryState(self.store, fan_ip='127.0.0.2', sensor_ip='127.0.0.1')
        fan = mock.Mock(subscriptions={r.SENSOR_TOPIC})
        state.clients[state.fan_ip] = fan
        endpoint = r.MQTTServer(('127.0.0.1', 0), state)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(endpoint.server_address, timeout=2) as sensor:
                sensor.settimeout(2)
                self.assertEqual(connect(sensor)[0], 0x20)
                subscription = f'airmx/01/+/1/+/+/{r.DEVICE_ID}'
                send(sensor, 0x82, b'\0\1' + r.utf(subscription) + b'\1')
                self.assertEqual(r.recv_packet(sensor), (0x90, b'\0\1\0'))
                send(sensor, 0x32, r.utf(r.SENSOR_TOPIC) + b'\0\2' + json.dumps(OBSERVED_SENSOR).encode())
                self.assertEqual(r.recv_packet(sensor), (0x40, b'\0\2'))
                result = state.snapshot()['sensor']['data']['sensorData'][0]
                self.assertEqual(result, OBSERVED_SENSOR['sensorData'][0])
                fan.send.assert_not_called()
        finally:
            state.clients.pop(state.fan_ip, None)  # mock is not a socket client
            endpoint.shutdown()
            endpoint.server_close()
            thread.join(timeout=2)

    def test_sensor_relay_forwards_only_real_payload_to_subscribed_fan(self):
        state = r.RecoveryState(self.store, fan_ip='127.0.0.2', sensor_ip='127.0.0.1', relay_sensor=True)
        app_filter = f'airmx/01/+/+/1/+/{r.DEVICE_ID}'
        fan = mock.Mock(subscriptions={app_filter})
        state.clients[state.fan_ip] = fan
        raw = json.dumps(OBSERVED_SENSOR, separators=(',', ':')).encode()
        state.ingest(state.sensor_ip, r.SENSOR_TOPIC, raw)
        fan.send.assert_called_once_with(0x30, r.utf(r.SENSOR_TOPIC) + raw)
        self.assertTrue(state.snapshot()['sensor_relay_enabled'])

        unsubscribed_fan = mock.Mock(subscriptions=set())
        state.clients[state.fan_ip] = unsubscribed_fan
        self.assertFalse(state.relay_sensor_update(r.SENSOR_TOPIC, raw))
        unsubscribed_fan.send.assert_not_called()

    def test_publish_wrong_id_closes_connection(self):
        connect(self.sock)
        send(self.sock, 0x30, r.utf(r.FAN_STATUS_TOPIC.replace('10001', '1')) + PUBLIC_MESSAGE)
        self.assertEqual(self.sock.recv(1), b'')

    def test_mqtt_requires_connect(self):
        send(self.sock, 0xc0)
        self.assertEqual(self.sock.recv(1), b'')

    def test_mqtt_honors_keepalive(self):
        self.assertEqual(connect(self.sock, keepalive=1), (0x20, b'\0\0'))
        self.assertEqual(self.sock.recv(1), b'')  # server timeout at 1.5s

    def test_sensor_topic_subscription_does_not_authorize_status_query(self):
        self.store.register(r.FAN_MAC, PUBLIC_KEY)
        connect(self.sock)
        send(self.sock, 0x82, b'\0\1' + r.utf(r.SENSOR_TOPIC) + b'\0')
        self.assertEqual(r.recv_packet(self.sock), (0x90, b'\0\1\0'))
        with self.assertRaises(ValueError):
            self.state.request_status()

    def test_registration_http_does_not_log_or_return_key(self):
        endpoint = r.HTTPServer(('127.0.0.1', 0), self.state)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*endpoint.server_address, timeout=2)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                connection.request('GET', genid())
                response = connection.getresponse()
                body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertNotIn(PUBLIC_KEY, body + output.getvalue())
            self.assertNotIn('params=', output.getvalue())
            self.assertEqual(json.loads(body)['data']['eagleId'], 10001)
            connection.request('GET', '/exchange?device=10001')
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
            endpoint.shutdown()
            endpoint.server_close()
            thread.join(timeout=2)

    def test_loopback_admin_health_status_origin_and_post_header(self):
        admin = r.HTTPServer(('127.0.0.1', 0), self.state, admin=True)
        thread = threading.Thread(target=admin.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*admin.server_address, timeout=2)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read()), {'ready': True, 'key_present': False})
            connection.request('GET', '/status', headers={'Origin': 'https://untrusted.example'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request('POST', '/request-status', body='{}')
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.request('POST', '/request-status', body='{}', headers={'X-Airmx-Local': '1'})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
            connection.request('GET', '/status', headers={'Host': 'untrusted.example'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
            admin.shutdown()
            admin.server_close()
            thread.join(timeout=2)
        with self.assertRaises(ValueError):
            r.HTTPServer(('0.0.0.0', 0), self.state, admin=True)

    def test_nas_api_requires_exact_host_bearer_and_source(self):
        token = 't' * 48
        api = r.HTTPServer(('127.0.0.1', 0), self.state, api_token=token, api_client_ip='127.0.0.1')
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*api.server_address, timeout=2)
        headers = {'Authorization': f'Bearer {token}'}
        try:
            connection.request('GET', '/api/status')
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request('GET', '/api/status', headers={**headers, 'Host': 'untrusted.example'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            with mock.patch.object(self.state, 'set_speed', return_value={'sent': True, 'percent': 35}) as control:
                connection.request('POST', '/api/set-speed', body='{"percent":35}', headers={**headers, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual((response.status, json.loads(response.read())), (200, {'sent': True, 'percent': 35}))
                control.assert_called_once_with(35)
            with mock.patch.object(self.state, 'set_mode', return_value={'sent': True, 'mode': 1}) as control:
                connection.request('POST', '/api/set-mode', body='{"mode":1}', headers={**headers, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual((response.status, json.loads(response.read())), (200, {'sent': True, 'mode': 1}))
                control.assert_called_once_with(1)
        finally:
            connection.close()
            api.shutdown()
            api.server_close()
            thread.join(timeout=2)

    def test_fragmented_packet_and_oversize_remaining_length(self):
        first, second = socket.socketpair()
        try:
            data = b'x' * 300
            packet = b'\x30' + r.encode_remaining(len(data)) + data
            def writer():
                for chunk in (packet[:1], packet[1:2], packet[2:4], packet[4:]):
                    first.sendall(chunk)
            thread = threading.Thread(target=writer)
            thread.start()
            self.assertEqual(r.recv_packet(second), (0x30, data))
            thread.join(timeout=2)
            first.sendall(b'\x30\xff\xff\x7f')
            with self.assertRaises(ValueError):
                r.recv_packet(second)
        finally:
            first.close()
            second.close()


class MqttCredentialCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = r.KeyStore(Path(self.tmp.name) / 'protected')
        self.state = r.RecoveryState(self.store, fan_ip='127.0.0.1', sensor_ip='127.0.0.2')
        self.server = r.MQTTServer(('127.0.0.1', 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.sock = socket.create_connection(self.server.server_address, timeout=2)
        self.sock.settimeout(2)

    def tearDown(self):
        self.sock.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def publish_status(self, cadr):
        send(self.sock, 0x30, r.utf(r.FAN_STATUS_TOPIC) + signed_status(cadr))
        threading.Event().wait(0.05)

    def test_password_candidate_is_saved_only_after_signature_verifies(self):
        self.assertEqual(connect(self.sock, password=PUBLIC_KEY), (0x20, b'\0\0'))
        self.publish_status(31)
        self.assertEqual(self.store.key(), PUBLIC_KEY)
        self.assertTrue(self.state.snapshot()['status']['signature_verified'])

    def test_unrelated_connect_password_is_not_saved(self):
        self.assertEqual(connect(self.sock, password='UNRELATED-MQTT-PASSWORD'), (0x20, b'\0\0'))
        self.publish_status(31)
        self.assertFalse(self.store.registered)
        self.assertIsNone(self.state.snapshot()['status'])


if __name__ == '__main__':
    unittest.main()
