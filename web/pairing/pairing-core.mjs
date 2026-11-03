const FRAME_PAYLOAD_BYTES = 16;

export function makeFrames(command, payload, initialSequence = 1) {
  const total = Math.max(1, Math.ceil(payload.length / FRAME_PAYLOAD_BYTES));
  if (total > 15) throw new RangeError('BLE message exceeds the packet header limit');
  const frames = [];
  for (let index = 0; index < total; index += 1) {
    const chunk = payload.slice(index * FRAME_PAYLOAD_BYTES, (index + 1) * FRAME_PAYLOAD_BYTES);
    const frame = new Uint8Array(4 + chunk.length);
    frame[0] = (initialSequence + index) & 0xff;
    frame[1] = ((index + 1) << 4) | total;
    frame[2] = 0;
    frame[3] = command;
    frame.set(chunk, 4);
    frames.push(frame);
  }
  return { frames, nextSequence: (initialSequence + total) & 0xff };
}

export function credentialLengthError(ssid, password) {
  const encoder = new TextEncoder();
  const ssidBytes = encoder.encode(ssid).length;
  const passwordBytes = encoder.encode(password).length;
  if (ssidBytes < 1 || ssidBytes > 32) return 'ssid-length';
  if (passwordBytes < 8 || passwordBytes > 63) return 'password-length';
  return null;
}

export async function acceptDeviceSelection(selection, isCurrent) {
  const device = await selection;
  if (isCurrent()) return device;
  try { device?.gatt?.disconnect(); } catch {}
  return null;
}
