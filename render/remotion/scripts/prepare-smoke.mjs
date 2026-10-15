/** Offline synthetic media; does not start Chromium or use the GPU. Node >=22.18. */
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';
import { parseTimeline } from '../src/timeline.ts';

const source = new URL('../../../docs/reference/schemas/timeline-v2.example.json', import.meta.url);
const timeline = parseTimeline(JSON.parse(readFileSync(source, 'utf8')));
const output = new URL('../out/smoke/', import.meta.url);
const media = new URL('public/', output);
mkdirSync(media, { recursive: true });
writeFileSync(new URL('timeline.json', output), readFileSync(source));

const samples = Math.round(timeline.duration_ms * 48);
const wav = Buffer.alloc(44 + samples * 2);
wav.write('RIFF', 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
wav.writeUInt32LE(48000, 24); wav.writeUInt32LE(96000, 28);
wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
wav.write('data', 36); wav.writeUInt32LE(samples * 2, 40);
writeFileSync(new URL('narration.wav', media), wav);

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, bytes) {
  const body = Buffer.concat([Buffer.from(type), bytes]);
  const size = Buffer.alloc(4); size.writeUInt32BE(bytes.length);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
  return Buffer.concat([size, body, crc]);
}
for (const [index, clip] of timeline.clips.entries()) {
  const { width, height } = timeline;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4); ihdr[8] = 8; ihdr[9] = 2;
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const at = y * (width * 3 + 1) + 1 + x * 3;
    const grid = x % 180 < 8 || y % 240 < 8;
    raw[at] = grid ? 230 : index ? 110 + Math.floor(x * 80 / width) : 20;
    raw[at + 1] = grid ? 230 : 35 + Math.floor(y * 70 / height);
    raw[at + 2] = grid ? 230 : index ? 25 : 100 + Math.floor(x * 90 / width);
  }
  writeFileSync(new URL(clip.image, media), Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0)),
  ]));
}
console.log(`Mídia sintética pronta: ${fileURLToPath(output)} (12 s, 1080×1920). Nenhum render iniciado.`);
