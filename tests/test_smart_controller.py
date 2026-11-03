import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'skills/airmx-local-recovery/scripts/controller.py'
spec = importlib.util.spec_from_file_location('smart_controller', path)
smart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smart)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, path, body):
        self.calls.append((path, body))
        return {'ok': True}


def snapshot(now, co2=550, pm25=3, speed=9, mode=0, power=1, sensor_age=0):
    return {'fan_connected': True, 'sensor_connected': True,
            'status': {'received_at': now, 'signature_verified': True,
                       'data': {'cadr': speed, 'mode': mode, 'power': power}},
            'sensor': {'received_at': now - sensor_age,
                       'data': {'sensorData': [{'co2': {'value': co2}, 'pm25': {'value': pm25}}]}}}


class ControllerTests(unittest.TestCase):
    def test_takeover_at_low_concentration_keeps_nine_and_deduplicates(self):
        state, api, now = {}, FakeAPI(), 1800000000
        smart.process(state, snapshot(now, mode=1), now, api, 'start', 'control')
        self.assertEqual(api.calls, [('set-speed', {'percent': 9})])
        smart.process(state, snapshot(now), now + 30, api, 'tick', 'control')
        self.assertEqual(len(api.calls), 1)

    def test_observation_never_writes_even_when_stale_or_high(self):
        state, api, now = {}, FakeAPI(), 1800000000
        smart.process(state, snapshot(now, co2=1700, mode=1), now, api, 'start', 'observe')
        smart.process(state, snapshot(now + 1000, sensor_age=1200), now + 1000, api, 'tick', 'observe')
        self.assertEqual(api.calls, [])

    def test_power_off_suspends_without_starting(self):
        state, api, now = {}, FakeAPI(), 1800000000
        smart.process(state, snapshot(now, power=0), now, api, 'start', 'control')
        self.assertTrue(state['suspended'])
        smart.process(state, snapshot(now + 300), now + 300, api, 'tick', 'control')
        self.assertEqual(api.calls, [])

    def test_physical_mode_change_pauses_control(self):
        state = {'mode': 'control', 'expected_speed': 9, 'last_command_at': 1800000000}
        api = FakeAPI()
        smart.process(state, snapshot(1800000300, mode=1), 1800000300, api, 'tick', 'control')
        self.assertTrue(state['suspended'])
        self.assertEqual(api.calls, [])

    def test_stale_data_uses_backup_once(self):
        state, api, now = {'mode': 'control'}, FakeAPI(), 1800000000
        smart.process(state, snapshot(now, sensor_age=1000), now, api, 'tick', 'control')
        self.assertEqual(api.calls, [('set-speed', {'percent': 25})])
        smart.process(state, snapshot(now + 30, speed=25, sensor_age=1030), now + 30, api, 'tick', 'control')
        self.assertEqual(len(api.calls), 1)

    def test_unverified_status_never_controls(self):
        state, api, now = {}, FakeAPI(), 1800000000
        source = snapshot(now)
        source['status']['signature_verified'] = False
        with self.assertRaises(ValueError):
            smart.process(state, source, now, api, 'start', 'control')
        self.assertEqual(api.calls, [])

    def test_flat_but_high_co2_increases_speed(self):
        state = {'samples': [[0, 1100], [300, 1100]], 'base_speed': 9}
        target = smart.decide(state, 600, 1100, 3, 9, True)
        self.assertGreater(target, 9)

    def test_stable_in_band_holds(self):
        state = {'samples': [[0, 720], [300, 720]], 'base_speed': 20}
        self.assertEqual(smart.decide(state, 600, 720, 3, 27, True), 27)

    def test_bounds_and_ordinary_rate_limit(self):
        for current in [9, 20, 50, 100]:
            for co2 in [300, 550, 650, 700, 800, 900, 1300, 2000]:
                state = {'samples': [[0, co2], [300, co2]], 'base_speed': current}
                target = smart.decide(state, 600, co2, 3, current, True)
                self.assertGreaterEqual(target, 9)
                self.assertLessEqual(target, 100)
                if co2 < 1000:
                    self.assertLessEqual(abs(target - current), 5)

    def test_out_of_order_sample_is_ignored(self):
        state = {'samples': [[300, 700], [600, 710]]}
        self.assertIsNone(smart.decide(state, 600, 900, 3, 20, True))
        self.assertIsNone(smart.decide(state, 590, 900, 3, 20, True))

    def test_recorded_falling_co2_does_not_cut_ventilation_inside_band(self):
        # 2026-11-01 17:25/17:30: the old controller cut 24 -> 19 -> 14
        # while CO2 was still 754 -> 740 ppm, then had to increase again.
        state = {'samples': [[0, 781], [300, 773]], 'base_speed': 10,
                 'response_gain': .15147, 'trend_weight': .25}
        self.assertEqual(smart.decide(state, 600, 754, 8, 24, True), 24)
        self.assertEqual(smart.decide(state, 900, 740, 8, 24, True), 24)

    def test_low_co2_releases_speed_by_two_and_reaches_floor(self):
        state = {'samples': [[0, 620], [300, 600]], 'base_speed': 10}
        self.assertEqual(smart.decide(state, 600, 580, 3, 24, True), 22)
        self.assertEqual(smart.decide(state, 900, 570, 3, 22, True), 20)
        self.assertEqual(smart.decide(state, 1200, 560, 3, 10, True), 9)

    def test_predicted_exit_below_band_allows_gentle_reduction(self):
        state = {'samples': [[0, 740], [300, 700]], 'base_speed': 10,
                 'trend_weight': .5}
        self.assertEqual(smart.decide(state, 600, 655, 3, 24, True), 22)
        self.assertLess(state['predicted_co2'], smart.LOW)

    def test_rising_response_remains_five_points(self):
        state = {'samples': [[0, 663], [300, 724]], 'base_speed': 9,
                 'response_gain': .15147, 'trend_weight': .5}
        self.assertEqual(smart.decide(state, 600, 781, 7, 14, True), 19)

    def test_polling_does_not_repeat_a_downward_step(self):
        now, api = 1800000000, FakeAPI()
        state = {'mode': 'control', 'samples': [[now - 600, 600], [now - 300, 590]],
                 'base_speed': 10}
        smart.process(state, snapshot(now, co2=580, speed=24), now, api, 'tick', 'control')
        self.assertEqual(api.calls, [('set-speed', {'percent': 22})])
        smart.process(state, snapshot(now, co2=580, speed=22), now + 60, api, 'tick', 'control')
        self.assertEqual(len(api.calls), 1)

    def test_high_demand_overrides_in_band_hold(self):
        for co2, pm25 in [(1500, 3), (754, 75)]:
            state = {'samples': [[0, co2 + 20], [300, co2 + 10]], 'base_speed': 9}
            self.assertGreater(smart.decide(state, 600, co2, pm25, 24, True), 24)

    def test_settled_in_band_baseline_moves_towards_actual_speed(self):
        for base, speed in [(9, 24), (30, 24)]:
            state = {'samples': [[0, 760], [300, 760]], 'base_speed': base}
            self.assertEqual(smart.decide(state, 600, 760, 3, speed, True), speed)
            self.assertLess(abs(state['base_speed'] - speed), abs(base - speed))

    def test_matching_cached_status_cannot_confirm_a_new_command(self):
        now, state, api = 1800000000, {}, FakeAPI()
        source = snapshot(now)
        source['status']['received_at'] = now - 1
        smart.process(state, source, now, api, 'start', 'control')
        smart.process(state, source, now + 60, api, 'tick', 'control')
        self.assertFalse(state['acknowledged'])
        self.assertEqual(len(api.calls), 1)

    def test_confirmed_command_does_not_use_grace_to_override_manual_takeover(self):
        now, state, api = 1800000000, {}, FakeAPI()
        smart.process(state, snapshot(now), now, api, 'start', 'control')
        smart.process(state, snapshot(now + 2), now + 3, api, 'tick', 'control')
        self.assertTrue(state['acknowledged'])
        smart.process(state, snapshot(now + 40, mode=1, speed=70), now + 41,
                      api, 'tick', 'control')
        self.assertTrue(state['suspended'])
        self.assertEqual(state['state'], 'paused')
        self.assertEqual(len(api.calls), 1)


if __name__ == '__main__':
    unittest.main()
