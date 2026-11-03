import test from 'node:test';
import assert from 'node:assert/strict';
import { acceptDeviceSelection, credentialLengthError, makeFrames } from './pairing-core.mjs';

test('frames split into bounded ordered packets and wrap the sequence byte', () => {
  const payload = Uint8Array.from({ length: 17 }, (_, index) => index);
  const { frames, nextSequence } = makeFrames(0x15, payload, 0xff);
  assert.equal(frames.length, 2);
  assert.deepEqual([...frames[0]], [0xff, 0x12, 0, 0x15, ...payload.slice(0, 16)]);
  assert.deepEqual([...frames[1]], [0x00, 0x22, 0, 0x15, 16]);
  assert.equal(nextSequence, 1);
  assert.throws(() => makeFrames(0x16, new Uint8Array(16 * 15 + 1)), RangeError);
});

test('Wi-Fi field limits are measured in UTF-8 bytes', () => {
  assert.equal(credentialLengthError('猫', 'password'), null);
  assert.equal(credentialLengthError('界'.repeat(11), 'password'), 'ssid-length');
  assert.equal(credentialLengthError('wifi', '短pass'), 'password-length');
  assert.equal(credentialLengthError('wifi', '1234567890'.repeat(7)), 'password-length');
});

test('a device selected after cancellation is disconnected immediately', async () => {
  let resolveSelection;
  let disconnects = 0;
  const selection = new Promise((resolve) => { resolveSelection = resolve; });
  let current = true;
  const accepting = acceptDeviceSelection(selection, () => current);
  current = false;
  resolveSelection({ gatt: { disconnect() { disconnects += 1; } } });
  assert.equal(await accepting, null);
  assert.equal(disconnects, 1);
});
