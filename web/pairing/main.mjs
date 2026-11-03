import { acceptDeviceSelection, credentialLengthError, makeFrames } from './pairing-core.mjs';

const SERVICE_UUID = '22210000-554a-4546-5542-46534450464d';
const WRITE_UUID = '22210001-554a-4546-5542-46534450464d';
const NOTIFY_UUID = '22210002-554a-4546-5542-46534450464d';
const RESPONSE_TIMEOUT_MS = 20_000;
const ASSEMBLY_TIMEOUT_MS = 5_000;
const MAX_MESSAGE_BYTES = 256;
const encoder = new TextEncoder();

const $ = (id) => document.getElementById(id);
const ui = {
  readiness: $('readiness'), readinessDot: $('readiness-dot'),
  connectionDot: $('connection-dot'), connectionSummary: $('connection-summary'),
  sensorReadings: $('sensor-readings'), fanReadings: $('fan-readings'),
  unsupported: $('unsupported'), credentials: $('credentials'),
  ssid: $('ssid'), password: $('password'), inputError: $('input-error'),
  continue: $('continue'), activation: $('activation'), start: $('start'),
  bluetoothError: $('bluetooth-error'), progressPanel: $('progress-panel'),
  phase: $('phase'), progress: $('progress'), cancel: $('cancel'),
  result: $('result'), resultIcon: $('result-icon'), resultTitle: $('result-title'),
  registrationCheck: $('registration-check'), deviceId: $('device-id'), finish: $('finish'),
  failure: $('failure'), failureMessage: $('failure-message'),
  retry: $('retry'),
};

let serviceReady = false;
let credentials = null;
let bluetoothDevice = null;
let server = null;
let writeCharacteristic = null;
let notifyCharacteristic = null;
let notificationListener = null;
let disconnectListener = null;
let expectedCommand = null;
let responseTimer = null;
let assemblyTimer = null;
let assembly = null;
let sequence = 1;
let finished = false;
let attemptGeneration = 0;
let attemptPending = false;
let sendQueue = Promise.resolve();

function setReadiness(ready, message) {
  serviceReady = ready;
  ui.readiness.textContent = message;
  ui.readinessDot.classList.toggle('ready', ready);
  ui.readinessDot.classList.toggle('bad', !ready);
  ui.continue.disabled = !ready || !('bluetooth' in navigator);
}

async function requestReadiness() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4_000);
  try {
    const response = await fetch(new URL('/api/readiness', location.origin), {
      method: 'GET',
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error('unavailable');
    const status = await response.json();
    return status?.ready === true;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

async function checkReadiness() {
  const ready = await requestReadiness();
  setReadiness(ready, ready ? '本地注册服务已就绪' : '无法确认本地注册服务状态，配网已禁用');
}

function measurementValue(entry) {
  const value = entry && typeof entry === 'object' ? entry.value : entry;
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—';
}

async function verifyLocalRegistration(deviceId) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      const response = await fetch(new URL('/api/status', location.origin), {
        method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error',
        referrerPolicy: 'no-referrer', headers: { Accept: 'application/json' },
      });
      if (response.ok) {
        const status = await response.json();
        if (status.registered === true && status.deviceId === deviceId) {
          ui.resultIcon.textContent = '✓';
          ui.resultIcon.classList.remove('pending', 'warning');
          ui.resultTitle.textContent = '本地密钥已登记';
          ui.registrationCheck.textContent = '本地服务已保存这台风机的注册密钥。';
          ui.registrationCheck.classList.add('success');
          return;
        }
      }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  ui.resultIcon.textContent = '!';
  ui.resultIcon.classList.remove('pending');
  ui.resultIcon.classList.add('warning');
  ui.resultTitle.textContent = '设备有响应，本地注册未完成';
  ui.registrationCheck.textContent = '本地服务没有收到或保存新的 genId 注册。设备返回的 ID 可能来自旧绑定；目前不能查询状态或恢复联动。';
  ui.registrationCheck.classList.add('warning');
}

async function refreshDeviceStatus() {
  try {
    const response = await fetch(new URL('/api/status', location.origin), {
      method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error',
      referrerPolicy: 'no-referrer', headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error('unavailable');
    const status = await response.json();
    const connected = status.fan_connected === true || status.sensor_connected === true;
    ui.connectionDot.classList.toggle('ready', connected);
    ui.connectionDot.classList.toggle('bad', !connected);
    ui.connectionSummary.textContent = `风机 MQTT：${status.fan_connected ? '已连接' : '未连接'} · 检测仪 MQTT：${status.sensor_connected ? '已连接' : '未连接'} · 注册密钥：${status.registered ? '已保存' : '待配对'}`;

    const readings = status.sensor?.data?.sensorData?.[0];
    if (readings) {
      const labels = [
        ['温度', readings.temperature, '°C'], ['湿度', readings.humidity, '%'],
        ['PM2.5', readings.pm25, ''], ['PM10', readings.pm10, ''], ['CO₂', readings.co2, ' ppm'],
      ];
      ui.sensorReadings.textContent = `检测仪：${labels.map(([label, value, unit]) => `${label} ${measurementValue(value)}${unit}`).join(' · ')}`;
    } else {
      ui.sensorReadings.textContent = '等待检测仪读数…';
    }
    const fan = status.status?.data;
    if (fan && Object.keys(fan).length) {
      const fields = [['风量', fan.cadr == null ? null : `${fan.cadr}%`], ['电源', fan.power == null ? null : (fan.power ? '开' : '关')], ['模式', fan.mode], ['滤芯', fan.hepaPercent == null ? fan.g4Percent : `${fan.hepaPercent}%`]];
      ui.fanReadings.textContent = `风机状态：${fields.filter(([, value]) => value != null).map(([label, value]) => `${label} ${value}`).join(' · ')}`;
    } else {
      ui.fanReadings.textContent = status.registered ? '等待风机签名状态…' : '完成配对并注册后，才能查询风机状态。';
    }
  } catch {
    ui.connectionDot.classList.remove('ready');
    ui.connectionDot.classList.add('bad');
    ui.connectionSummary.textContent = '本地状态暂不可用';
    ui.sensorReadings.textContent = '请确认本机到 WRT 的 SSH 隧道仍然连通。';
    ui.fanReadings.textContent = '';
  }
}

function setStage(stage, text) {
  ui.phase.textContent = text;
  for (const item of ui.progress.querySelectorAll('li')) {
    item.classList.toggle('current', item.dataset.stage === stage);
  }
}

function markComplete(stage) {
  const item = ui.progress.querySelector(`[data-stage="${stage}"]`);
  item?.classList.remove('current');
  item?.classList.add('complete');
}

function isCurrentAttempt(generation) {
  return generation === attemptGeneration && !finished;
}

async function stopStaleAttempt(generation, device) {
  if (isCurrentAttempt(generation)) return false;
  try { device?.gatt?.disconnect(); } catch {}
  await disconnect();
  return true;
}

function send(command, payload, generation) {
  const operation = sendQueue.then(async () => {
    if (!isCurrentAttempt(generation) || !writeCharacteristic) {
      payload.fill(0);
      return false;
    }
    const framed = makeFrames(command, payload, sequence);
    sequence = framed.nextSequence;
    try {
      for (const frame of framed.frames) {
        if (!isCurrentAttempt(generation) || !writeCharacteristic) return false;
        await writeCharacteristic.writeValueWithResponse(frame);
        if (!isCurrentAttempt(generation)) return false;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      return isCurrentAttempt(generation);
    } finally {
      for (const frame of framed.frames) frame.fill(0);
      payload.fill(0);
    }
  });
  sendQueue = operation.catch(() => false);
  return operation;
}

function expectResponse(command, generation) {
  clearTimeout(responseTimer);
  expectedCommand = command;
  responseTimer = setTimeout(() => {
    if (isCurrentAttempt(generation)) void fail('设备响应超时。请确认设备仍处于蓝牙配对模式，并检查 Wi‑Fi 与本地服务状态。');
  }, RESPONSE_TIMEOUT_MS);
}

function handshakePayload() {
  return Uint8Array.of(
    0x08, 0, 0, 0, 0, 0, 0, 0, 0,
    0x05, 0x31, 0x2e, 0x30, 0x2e, 0x30,
  );
}

function wifiPayload() {
  const ssid = encoder.encode(credentials.ssid);
  const password = encoder.encode(credentials.password);
  const payload = new Uint8Array(2 + ssid.length + password.length);
  payload[0] = ssid.length;
  payload.set(ssid, 1);
  payload[1 + ssid.length] = password.length;
  payload.set(password, 2 + ssid.length);
  return payload;
}

function onNotification(event, generation) {
  if (!isCurrentAttempt(generation)) return;
  try {
    const view = event.target.value;
    if (view.byteLength < 4) throw new Error('packet');
    const sequenceNumber = view.getUint8(0);
    const packetByte = view.getUint8(1);
    const current = packetByte >> 4;
    const total = packetByte & 0x0f;
    const encrypted = view.getUint8(2);
    const command = view.getUint8(3);
    if (encrypted !== 0 || total < 1 || current < 1 || current > total || total > 15) throw new Error('packet');
    const payload = new Uint8Array(view.buffer, view.byteOffset + 4, view.byteLength - 4).slice();

    if (current === 1) {
      clearTimeout(assemblyTimer);
      assembly = { command, total, next: 1, lastSequence: sequenceNumber, parts: [], length: 0 };
      assemblyTimer = setTimeout(() => {
        assembly = null;
        fail('设备响应分包不完整，已停止配对。');
      }, ASSEMBLY_TIMEOUT_MS);
    }
    if (!assembly || command !== assembly.command || total !== assembly.total ||
        current !== assembly.next || (current > 1 && sequenceNumber !== ((assembly.lastSequence + 1) & 0xff))) {
      throw new Error('packet');
    }
    assembly.parts.push(payload);
    assembly.length += payload.length;
    assembly.lastSequence = sequenceNumber;
    assembly.next += 1;
    if (assembly.length > MAX_MESSAGE_BYTES) throw new Error('packet');
    if (current !== total) return;

    clearTimeout(assemblyTimer);
    const message = {
      command,
      payload: concat(assembly.parts, assembly.length),
    };
    assembly = null;
    void handleMessage(message, generation);
  } catch {
    fail('收到无法识别或不完整的蓝牙响应，已停止配对。');
  }
}

function concat(parts, length) {
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    bytes.set(part, offset);
    offset += part.length;
  }
  return bytes;
}

async function handleMessage(message, generation) {
  if (!isCurrentAttempt(generation)) return;
  if (expectedCommand === null || message.command !== expectedCommand) {
    fail('收到意外的设备响应，已停止配对。');
    return;
  }
  clearTimeout(responseTimer);
  const respondedTo = expectedCommand;
  expectedCommand = null;

  try {
    if (respondedTo === 0x0b) {
      markComplete('handshake');
      setStage('wifi', '已收到握手响应；响应状态格式未由参考实现说明。');
      expectResponse(0x15, generation);
      const sent = await send(0x15, wifiPayload(), generation);
      if (!sent && isCurrentAttempt(generation)) await fail('蓝牙通信中断，配对未完成。请检查设备状态后重试。');
      return;
    }
    if (respondedTo === 0x15) {
      markComplete('wifi');
      credentials = null;
      ui.password.value = '';
      ui.ssid.value = '';
      setStage('register', '已收到 Wi‑Fi 设置响应；正在等待注册响应。');
      expectResponse(0x16, generation);
      const sent = await send(0x16, new Uint8Array(), generation);
      if (!sent && isCurrentAttempt(generation)) await fail('蓝牙通信中断，配对未完成。请检查设备状态后重试。');
      return;
    }
    if (respondedTo === 0x16) {
      if (message.payload.length !== 5 || message.payload[0] !== 4) {
        fail('注册响应格式未知或无效。页面未判定配对成功。');
        return;
      }
      const id = new DataView(message.payload.buffer, message.payload.byteOffset, message.payload.byteLength).getUint32(1);
      if (id === 0) {
        fail('注册响应中的设备 ID 无效。');
        return;
      }
      finished = true;
      markComplete('register');
      ui.progressPanel.hidden = true;
      ui.result.hidden = false;
      ui.deviceId.textContent = `设备 ID：${id}`;
      await disconnect();
      void verifyLocalRegistration(id);
    }
  } catch {
    fail('蓝牙通信中断，配对未完成。请检查设备状态后重试。');
  }
}

function clearCredentials() {
  credentials = null;
  ui.password.value = '';
  ui.ssid.value = '';
}

async function disconnect() {
  clearTimeout(responseTimer);
  clearTimeout(assemblyTimer);
  responseTimer = null;
  assemblyTimer = null;
  assembly = null;
  expectedCommand = null;
  if (notifyCharacteristic && notificationListener) {
    notifyCharacteristic.removeEventListener('characteristicvaluechanged', notificationListener);
    try { await notifyCharacteristic.stopNotifications(); } catch {}
  }
  if (bluetoothDevice && disconnectListener) {
    bluetoothDevice.removeEventListener('gattserverdisconnected', disconnectListener);
  }
  if (server?.connected) server.disconnect();
  bluetoothDevice = null;
  server = null;
  notifyCharacteristic = null;
  writeCharacteristic = null;
  notificationListener = null;
  disconnectListener = null;
}

async function fail(message) {
  if (finished || ui.failure.hidden === false) return;
  finished = true;
  await disconnect();
  clearCredentials();
  ui.progressPanel.hidden = true;
  ui.activation.hidden = true;
  ui.failureMessage.textContent = message;
  ui.failure.hidden = false;
}

async function beginPairing() {
  if (!serviceReady || !credentials || !('bluetooth' in navigator) || attemptPending) return;
  const generation = ++attemptGeneration;
  attemptPending = true;
  finished = false;
  ui.start.disabled = true;
  ui.bluetoothError.hidden = true;
  ui.activation.hidden = true;
  ui.progressPanel.hidden = false;
  ui.phase.textContent = '请选择 AIRMX Pro 蓝牙设备…';
  ui.progress.querySelectorAll('li').forEach((item) => item.classList.remove('current', 'complete'));

  try {
    // requestDevice is called directly from the user's click to preserve the
    // browser's required transient user activation.
    const selection = navigator.bluetooth.requestDevice({
      filters: [{ name: 'AIRMX Pro' }],
      optionalServices: [SERVICE_UUID],
    });
    const selected = await acceptDeviceSelection(selection, () => isCurrentAttempt(generation));
    if (!selected) return;
    bluetoothDevice = selected;
    disconnectListener = () => {
      if (isCurrentAttempt(generation)) void fail('蓝牙连接已断开，配对未完成。');
    };
    selected.addEventListener('gattserverdisconnected', disconnectListener);

    // Recheck immediately after the chooser. Registration readiness can change
    // while the native Bluetooth picker is open.
    const readyAfterSelection = await requestReadiness();
    if (await stopStaleAttempt(generation, selected)) return;
    setReadiness(readyAfterSelection, readyAfterSelection ? '本地注册服务已就绪' : '本地注册服务尚未就绪，配网已禁用');
    if (!readyAfterSelection) {
      await fail('本地注册服务当前未就绪。设备尚未收到配网命令，请稍后重试。');
      return;
    }

    server = await selected.gatt.connect();
    if (await stopStaleAttempt(generation, selected)) return;
    const service = await server.getPrimaryService(SERVICE_UUID);
    if (await stopStaleAttempt(generation, selected)) return;
    writeCharacteristic = await service.getCharacteristic(WRITE_UUID);
    if (await stopStaleAttempt(generation, selected)) return;
    notifyCharacteristic = await service.getCharacteristic(NOTIFY_UUID);
    if (await stopStaleAttempt(generation, selected)) return;
    notificationListener = (event) => onNotification(event, generation);
    notifyCharacteristic.addEventListener('characteristicvaluechanged', notificationListener);
    await notifyCharacteristic.startNotifications();
    if (await stopStaleAttempt(generation, selected)) return;
    sequence = 1;

    // Last check is immediately before the first BLE write.
    const readyBeforeWrite = await requestReadiness();
    if (await stopStaleAttempt(generation, selected)) return;
    setReadiness(readyBeforeWrite, readyBeforeWrite ? '本地注册服务已就绪' : '本地注册服务尚未就绪，配网已禁用');
    if (!readyBeforeWrite) {
      await fail('本地注册服务当前未就绪。设备尚未收到配网命令，请稍后重试。');
      return;
    }

    setStage('handshake', '正在发送握手请求…');
    expectResponse(0x0b, generation);
    const sent = await send(0x0b, handshakePayload(), generation);
    if (!sent && isCurrentAttempt(generation)) await fail('蓝牙通信中断，配对未完成。请检查设备状态后重试。');
  } catch {
    if (isCurrentAttempt(generation)) {
      await fail('无法连接 AIRMX Pro。请确认绿色蓝牙图标正在闪烁，并重新尝试。');
    }
  } finally {
    attemptPending = false;
    ui.start.disabled = false;
  }
}

function validateCredentials() {
  const issue = credentialLengthError(ui.ssid.value, ui.password.value);
  if (issue === 'ssid-length') return 'SSID 的 UTF‑8 长度须为 1–32 字节。';
  if (issue === 'password-length') return 'Wi‑Fi 密码的 UTF‑8 长度须为 8–63 字节。';
  return null;
}

ui.credentials.addEventListener('submit', (event) => {
  event.preventDefault();
  ui.inputError.hidden = true;
  if (!serviceReady) return;
  const error = validateCredentials();
  if (error) {
    ui.inputError.textContent = error;
    ui.inputError.hidden = false;
    return;
  }
  credentials = { ssid: ui.ssid.value, password: ui.password.value };
  ui.credentials.hidden = true;
  ui.activation.hidden = false;
});

ui.start.addEventListener('click', () => { void beginPairing(); });
ui.cancel.addEventListener('click', async () => {
  attemptGeneration += 1;
  finished = true;
  await disconnect();
  clearCredentials();
  ui.progressPanel.hidden = true;
  ui.activation.hidden = true;
  ui.credentials.hidden = false;
});
ui.finish.addEventListener('click', () => {
  attemptGeneration += 1;
  finished = true;
  clearCredentials();
  ui.result.hidden = true;
  ui.credentials.hidden = false;
});
ui.retry.addEventListener('click', () => {
  attemptGeneration += 1;
  finished = false;
  ui.failure.hidden = true;
  ui.credentials.hidden = false;
  ui.password.focus();
});

if (!('bluetooth' in navigator)) {
  ui.unsupported.hidden = false;
  setReadiness(false, '此浏览器不支持 Web Bluetooth');
} else {
  void checkReadiness();
}
// Browser password managers may autofill even autocomplete=off. Do not let an
// unrelated saved login become Wi-Fi credentials on this transient setup page.
ui.ssid.value = '';
ui.password.value = '';
void refreshDeviceStatus();
setInterval(() => { void refreshDeviceStatus(); }, 5_000);
