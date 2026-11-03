"""Bounded AIRMX controller, invoked by HA shell_command; no background daemon.

Credentials stay in HA's secrets.yaml and never enter arguments or output.
Every operation uses one file lock. The observation mode never sends controls.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import time
import urllib.request

MIN_SPEED = 9
TARGET = 700
LOW = 650
HIGH = 800
MAX_DOWN_STEP = 2
POLICY_VERSION = '2026-11-02-gentle-down'


def clamp(value, low, high):
    return max(low, min(high, value))


def trend(samples):
    if len(samples) < 3 or samples[-1][0] - samples[0][0] < 480:
        return None
    rows = samples[-3:]
    xs = [(p[0] - rows[0][0]) / 60 for p in rows]
    ys = [p[1] for p in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denominator if denominator else None


def decide(state, sample_ts, co2, pm25, speed, learning):
    """One NEW measurement only. Returns a target; performs no I/O."""
    old = state.get('samples', [])
    if old and sample_ts <= old[-1][0]:
        return None
    if old and sample_ts - old[-1][0] > 750:
        old = []
    samples = (old + [[sample_ts, co2]])[-3:]
    state['samples'] = samples
    state['sample_interval_s'] = sample_ts - old[-1][0] if old else None
    slope = trend(samples)
    credible = not old or abs(co2 - old[-1][1]) <= 100
    state['slope'] = round(slope, 3) if slope is not None else None
    state['co2'] = co2
    state['pm25'] = pm25
    state['sample_ts'] = sample_ts

    # Select a bounded forecast weight by past one-step errors. This adjusts
    # prediction only; it cannot remove concentration or speed constraints.
    prior = state.get('forecast_candidates')
    if prior and 240 <= sample_ts - prior['ts'] <= 390:
        errors = state.setdefault('forecast_errors', {})
        for weight, prediction in prior['values'].items():
            error = abs(co2 - prediction)
            errors[weight] = .9 * errors.get(weight, error) + .1 * error
        state['forecast_count'] = state.get('forecast_count', 0) + 1
    weight = state.get('trend_weight', .25)
    if state.get('forecast_count', 0) >= 6:
        weight = float(min(state['forecast_errors'], key=state['forecast_errors'].get))
    state['trend_weight'] = weight
    smooth = .75 * co2 + .25 * old[-1][1] if old else co2
    bounded_slope = clamp(slope or 0, -20, 20)
    used_weight = weight if credible else 0
    predicted = smooth + used_weight * 5 * bounded_slope
    state['predicted_co2'] = round(predicted, 1)
    if slope is not None and credible:
        state['forecast_candidates'] = {
            'ts': sample_ts,
            'values': {str(w): smooth + w * 5 * bounded_slope for w in (0, .25, .5)},
        }
    else:
        state.pop('forecast_candidates', None)

    base = state.get('base_speed', speed)
    gain = state.get('response_gain', .15)
    probe = state.get('response_probe')
    if learning and probe and sample_ts - probe['ts'] >= 600:
        if sample_ts - probe['ts'] <= 1000 and slope is not None and credible:
            measured = -(slope - probe['slope']) / probe['delta']
            if .03 <= measured <= 1.5:
                bounded = clamp(measured, gain * .8, gain * 1.2)
                gain = .95 * gain + .05 * bounded
                state['response_samples'] = state.get('response_samples', 0) + 1
        state.pop('response_probe', None)
    state['response_gain'] = gain

    # A slow bounded baseline adaptation, never an unconstrained integrator.
    if learning and slope is not None and credible:
        if abs(slope) < .5 and LOW <= co2 <= HIGH:
            # A settled in-band speed is better evidence than adding a fixed
            # increment merely because concentration is above the midpoint.
            base = .95 * base + .05 * speed
        elif co2 > HIGH:
            base += 1 if slope >= 0 else .25
        elif co2 > 725 and slope >= -.5:
            base += .5
        elif co2 < LOW:
            base -= .5
        elif co2 < 675 and slope < 0:
            base -= .25
    base = clamp(base, MIN_SPEED, 90)
    state['base_speed'] = round(base, 3)

    if slope is None:
        wanted = speed
        reason = '积累真实采样，暂时保持当前风量'
    else:
        # Retain the original rising response; falling trends should not pull
        # the ventilation target down as strongly after a successful increase.
        trend_adjustment = (clamp(.25 * bounded_slope / gain, 0, 10)
                            if bounded_slope >= 0 else
                            clamp(.08 * bounded_slope / gain, -3, 0)) if credible else 0
        wanted = base + .1 * (predicted - TARGET) + trend_adjustment
        reason = '浓度偏差与短期趋势调节'
        if LOW <= co2 <= HIGH and abs(slope) < .5:
            wanted = speed
            reason = '浓度在目标带且趋势平稳，保持'
        elif LOW <= co2 <= HIGH and slope < 0 and LOW <= predicted <= HIGH:
            wanted = speed
            reason = '浓度在目标带内回落，保持通风避免过早降速'

    # These are controller intervention settings, not health classifications.
    rapid = False
    if co2 >= 1000:
        wanted = max(wanted, 40)
        rapid = True
        reason = 'CO2明显升高，优先增加通风'
    if co2 >= 1200:
        wanted = max(wanted, 60)
    if co2 >= 1500:
        wanted = max(wanted, 80)
    if pm25 >= 35:
        wanted = max(wanted, 40)
        rapid = True
        reason = '颗粒物需求覆盖普通调速'
    if pm25 >= 75:
        wanted = max(wanted, 60)
    wanted = int(round(clamp(wanted, MIN_SPEED, 100)))
    max_step = 20 if co2 >= 1200 or pm25 >= 75 else 10 if rapid else 5
    candidate = int(clamp(wanted, speed - MAX_DOWN_STEP, speed + max_step))
    candidate = int(clamp(candidate, MIN_SPEED, 100))
    # The previous symmetric three-point deadband would suppress every new
    # two-point downward step. Keep the rising deadband, allow two-point falls
    # and the final one-point return to the minimum.
    if ((0 < candidate - speed < 3) or
            (0 < speed - candidate < MAX_DOWN_STEP and candidate != MIN_SPEED)):
        candidate = speed
        reason = '建议变化较小，保持以减少变档'
    elif candidate < speed and wanted < speed - MAX_DOWN_STEP:
        reason += '；每个新样本最多降低2个百分点'
    state['reason'] = reason
    state['suggested_speed'] = candidate
    state['unlimited_target'] = wanted
    return candidate


class API:
    def __init__(self, root):
        import yaml
        secrets = yaml.safe_load((root.parent / 'secrets.yaml').read_text())
        self.authorization = secrets['airmx_local_api']

    def call(self, path, body=None):
        request = urllib.request.Request(
            'http://192.0.2.1:25887/api/' + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': self.authorization, 'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(request, timeout=18) as response:
            return json.load(response)


def telemetry(snapshot, now):
    status = snapshot.get('status') or {}
    data = status.get('data') or {}
    if not snapshot.get('fan_connected') or not status.get('signature_verified'):
        raise ValueError('风机离线或缺少验签状态')
    age = now - status.get('received_at', 0)
    if not -120 <= age <= 360:
        raise ValueError('风机状态已过期')
    if type(data.get('cadr')) is not int or not 0 <= data['cadr'] <= 100:
        raise ValueError('风量状态无效')
    if data.get('power') not in (0, 1) or data.get('mode') not in (0, 1, 2, 3):
        raise ValueError('运行状态无效')
    return status, data


def process(state, snapshot, now, api, action, mode, native_mode=-1, percent=-1):
    if action == 'stop':
        state.update(mode='off', state='off', reason='已停止HA调速')
        state.pop('expected_speed', None)
        return
    status, fan = telemetry(snapshot, now)
    state.update(actual_mode=fan['mode'], actual_speed=fan['cadr'], actual_power=fan['power'])
    if action in ('native', 'manual'):
        if action == 'native' and native_mode not in (0, 1, 2, 3):
            raise ValueError('模式无效')
        if action == 'manual' and not MIN_SPEED <= percent <= 100:
            raise ValueError('手动风量必须在9到100之间')
        state.update(mode='off', state='off', reason='已交还原生模式或手动控制')
        state.pop('expected_speed', None)
        api.call('set-mode' if action == 'native' else 'set-speed',
                 {'mode': native_mode} if action == 'native' else {'percent': percent})
        return
    if action == 'start':
        state.clear()
        state.update(mode=mode, state='warming', base_speed=max(MIN_SPEED, fan['cadr']),
                     trend_weight=.25, response_gain=.15, started_at=now,
                     actual_mode=fan['mode'], actual_speed=fan['cadr'], actual_power=fan['power'])
    if state.get('mode') not in ('control', 'observe'):
        state.update(state='off', reason='HA调速未启用')
        return
    if state.get('suspended'):
        state.update(state='paused', reason='已检测到人工接管；重新应用HA模式后恢复')
        return
    if fan['power'] == 0:
        state.update(state='paused', reason='风机已关机，HA不会自动开机')
        if state['mode'] == 'control':
            state['suspended'] = True
        return

    # A physical change of mode/speed pauses control. Initial takeover is the
    # sole exception. Never fight an external writer with repeated commands.
    if state['mode'] == 'control' and action != 'start' and 'expected_speed' in state:
        fresh = status.get('received_at', 0) > state.get('last_command_at', 0)
        if fan['mode'] == 0 and fan['cadr'] == state['expected_speed']:
            if fresh:
                state['acknowledged'] = True
        elif fresh and (state.get('acknowledged') or
                        status['received_at'] > state.get('last_command_at', 0) + 90):
            state.update(suspended=True, state='paused', reason='检测到实体按键或其他控制，暂停HA调速')
            return
        if not state.get('acknowledged') and now - state.get('last_command_at', 0) > 390:
            state.update(suspended=True, state='paused', reason='未确认控制结果，暂停以免重复发送')
            return

    sensor = snapshot.get('sensor') or {}
    ts = sensor.get('received_at', 0)
    state['sample_ts'] = ts
    if not -120 <= now - ts <= 900:
        state.update(state='stale', reason='检测仪超过15分钟无新数据，进入备用风量')
        if state['mode'] == 'control':
            fallback = max(25, fan['cadr'])
            if fan['mode'] != 0 or fallback != fan['cadr']:
                api.call('set-speed', {'percent': fallback})
                state.update(expected_speed=fallback, last_command_at=now, acknowledged=False)
        return
    if not snapshot.get('sensor_connected'):
        state.update(state='waiting', reason='检测仪连接中断，等待或在数据过期后进入备用风量')
        return
    samples = sensor.get('data', {}).get('sensorData', [])
    if not samples:
        raise ValueError('缺少检测仪数据')
    co2 = samples[0].get('co2', {}).get('value')
    pm25 = samples[0].get('pm25', {}).get('value')
    if (type(co2) not in (int, float) or not math.isfinite(co2) or not 250 <= co2 <= 10000
            or type(pm25) not in (int, float) or not math.isfinite(pm25) or not 0 <= pm25 <= 1000):
        raise ValueError('检测仪数值无效')
    target = decide(state, ts, co2, pm25, fan['cadr'], state['mode'] == 'control')
    if target is None:
        return
    state['state'] = 'warming' if state.get('slope') is None else 'active' if state['mode'] == 'control' else 'observing'
    state['updated_at'] = now
    if state['mode'] == 'observe':
        state['state'] = 'observing'
        return
    if action == 'start' or fan['mode'] != 0 or target != fan['cadr']:
        api.call('set-speed', {'percent': target})
        if state.get('slope') is not None and abs(target - fan['cadr']) >= 3:
            state['response_probe'] = {'ts': ts, 'slope': state['slope'], 'delta': target - fan['cadr']}
        state.update(expected_speed=target, last_command_at=now, acknowledged=False,
                     last_action='设置风量%d%%' % target)
    else:
        state['last_action'] = '保持风量%d%%' % fan['cadr']


def report(state, now):
    keys = ['state', 'mode', 'reason', 'co2', 'pm25', 'slope', 'predicted_co2', 'suggested_speed',
            'base_speed', 'trend_weight', 'response_gain', 'response_samples', 'forecast_count',
            'sample_ts', 'sample_interval_s', 'actual_mode', 'actual_speed', 'actual_power',
            'last_action', 'error', 'updated_at', 'acknowledged']
    result = {k: state.get(k) for k in keys}
    result['state'] = state.get('state', 'off')
    result['min_speed'] = MIN_SPEED
    result['target_co2'] = TARGET
    result['policy_version'] = POLICY_VERSION
    result['max_down_step'] = MAX_DOWN_STEP
    result['sample_age_s'] = max(0, int(now - state['sample_ts'])) if state.get('sample_ts') else None
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/config/airmx_smart'))
    parser.add_argument('--action', choices=['report', 'start', 'stop', 'tick', 'native', 'manual'], default='report')
    parser.add_argument('--mode', choices=['control', 'observe'], default='observe')
    parser.add_argument('--native-mode', type=int, default=-1)
    parser.add_argument('--percent', type=int, default=-1)
    args = parser.parse_args()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = args.root / 'state.json'
    os.umask(0o077)
    with (args.root / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        now = time.time()
        code = 0
        if args.action != 'report':
            try:
                if args.action == 'stop':
                    process(state, None, now, None, 'stop', args.mode)
                else:
                    api = API(args.root)
                    process(state, api.call('status'), now, api, args.action, args.mode, args.native_mode, args.percent)
                state.pop('error', None)
            except Exception as exc:
                # No network headers, body, exception arguments or credentials.
                state.update(state='error', error=type(exc).__name__, reason='本轮未完成；暂停下发并等待检查')
                code = 1
            temporary = state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False))
            os.chmod(temporary, 0o600)
            temporary.replace(state_path)
        print(json.dumps(report(state, now), ensure_ascii=False))
        return code


if __name__ == '__main__':
    raise SystemExit(main())
