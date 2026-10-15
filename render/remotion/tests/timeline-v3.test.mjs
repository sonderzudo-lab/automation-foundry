import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { clipWindow, parseTimeline, videoMetadata } from '../src/timeline.ts';
import { clipState, clipVideoTiming } from '../src/timing.ts';

const example = JSON.parse(readFileSync(new URL('../../../docs/reference/schemas/timeline-v3.example.json', import.meta.url), 'utf8'));
const fresh = () => structuredClone(example);
const videoFields = ['video', 'video_sha256', 'video_ms'];
function animate(clip, video_ms = 6200) {
  Object.assign(clip, { video: `scene-${clip.index}.mp4`, video_sha256: 'e'.repeat(64), video_ms, effect: 'none' });
}

test('canonical TimelineV3 and all-still fallback retain metadata and image motion', () => {
  assert.deepEqual(parseTimeline(example), example);
  assert.deepEqual(videoMetadata(example), { width: 1080, height: 1920, fps: 30, durationInFrames: 360 });
  const t = fresh();
  Object.assign(t.clips[0], { video: null, video_sha256: null, video_ms: null, effect: 'pan_left' });
  assert.equal(parseTimeline(t), t);
  assert.equal(clipVideoTiming(t, 0), null);
  assert.equal(clipVideoTiming(t, 1), null);
  assert.equal(clipState(t, 0, 0).xPercent, 5);
  assert.ok(clipState(t, 1, 9000).scale > 1);
});

for (let mask = 1; mask < 7; mask++) {
  test(`v3 rejects partially null video fields (mask ${mask})`, () => {
    const t = fresh();
    videoFields.forEach((field, i) => { if (mask & (1 << i)) t.clips[0][field] = null; });
    assert.throws(() => parseTimeline(t), /clip 1 must set video, video_sha256 and video_ms together/);
  });
}
for (const field of videoFields) {
  test(`v3 requires explicit ${field}, including on a still`, () => {
    for (const index of [0, 1]) {
      const t = fresh(); delete t.clips[index][field];
      assert.throws(() => parseTimeline(t), /campos ausentes ou extras/);
    }
  });
}
for (const value of ['', '../scene.mp4', 'dir/scene.mp4', 'dir\\scene.mp4', 'https://host/scene.mp4', '/scene.mp4', '.scene.mp4', 'scene.png', 'scene.MP4', 'a'.repeat(129) + '.mp4', 42, true]) {
  test(`v3 rejects invalid video filename ${JSON.stringify(value)}`, () => {
    const t = fresh(); t.clips[0].video = value;
    assert.throws(() => parseTimeline(t), /clips\[0\]\.video: expected a local MP4 filename/);
  });
}
for (const value of ['', 'f'.repeat(63), 'f'.repeat(65), 'F'.repeat(64), 'g'.repeat(64), 42]) {
  test(`v3 rejects invalid video hash ${JSON.stringify(value)}`, () => {
    const t = fresh(); t.clips[0].video_sha256 = value;
    assert.throws(() => parseTimeline(t), /clips\[0\]\.video_sha256/);
  });
}
for (const value of [0, -1, 14_400_001, 6200.5, '6200', true, NaN, Infinity]) {
  test(`v3 rejects invalid video duration ${String(value)}`, () => {
    const t = fresh(); t.clips[0].video_ms = value;
    assert.throws(() => parseTimeline(t), /clips\[0\]\.video_ms/);
  });
}
for (const effect of ['zoom_in', 'zoom_out', 'pan_left', 'pan_right']) {
  test(`v3 rejects video effect ${effect}`, () => {
    const t = fresh(); t.clips[0].effect = effect;
    assert.throws(() => parseTimeline(t), /clip 1 plays a video and must have the effect none/);
  });
}
for (const [name, mutate, message] of [
  ['extra root field', t => { t.video = 'scene.mp4'; }, /props: campos/],
  ['extra clip field', t => { t.clips[0].speed = 1; }, /clips\[0\]: campos/],
  ['extra word field', t => { t.words[0].extra = true; }, /words\[0\]: campos/],
  ['extra overlay field', t => { t.overlays[0].extra = true; }, /overlays\[0\]: campos/],
  ['duplicate video', t => { animate(t.clips[1]); t.clips[1].video = t.clips[0].video; }, /two clips play the same video/],
  ['wrong dimensions', t => { t.width = 1920; }, /dimensões/],
  ['clip gap', t => { t.clips[1].start_ms++; }, /continuidade/],
  ['first crossfade', t => { t.clips[0].transition_in = 'crossfade'; }, /primeiro clip/],
  ['zero crossfade', t => { t.transition_ms = 0; }, /crossfade/],
  ['overlapping words', t => { t.words[1].start_ms = 999; }, /words\[1\]/],
  ['overlay overlap', t => { t.overlays[1].start_ms = 2000; }, /overlays\[1\]/],
  ['item outside card', t => { t.overlays[0].item_end_ms = 2400; }, /item fora/],
  ['inconsistent list', t => { t.overlays[1].items[0] = 'Other'; }, /list:/],
  ['closing outside words', t => { t.overlays.at(-1).highlight_from = 12; }, /índices/],
]) test(`v3 rejects ${name} with context`, () => {
  const t = fresh(); mutate(t);
  assert.throws(() => parseTimeline(t), error => /TimelineV3 inválido/.test(error.message) && message.test(error.message));
});

test('v2 still rejects video fields and unsupported versions fail explicitly', () => {
  const t = fresh(); t.schema_version = 2;
  assert.throws(() => parseTimeline(t), /TimelineV2 inválido: clips\[0\]: campos/);
  for (const version of [1, 4, '3', null]) {
    t.schema_version = version;
    assert.throws(() => parseTimeline(t), /schema_version: expected 2 or 3/);
  }
});

test('stretch limit uses the padded window, accepts exactly 1.25x and rejects one ms less', () => {
  const t = fresh();
  t.clips[0].video_ms = 4960; // 6200 / 1.25, not 6000 / 1.25.
  assert.equal(parseTimeline(t), t);
  assert.equal(clipVideoTiming(t, 0).playbackRate, .8);
  t.clips[0].video_ms--;
  assert.throws(() => parseTimeline(t), /clip 1 has a video too short for its window/);
});

test('video starts at the window origin, slows down only when needed, and cuts surplus', () => {
  const t = fresh(); animate(t.clips[1], 5000);
  parseTimeline(t);
  assert.deepEqual(clipVideoTiming(t, 0), { from: 0, durationInFrames: 186, playbackRate: 1 });
  const timing = clipVideoTiming(t, 1);
  assert.deepEqual(timing, { from: 174, durationInFrames: 186, playbackRate: 5000 / 6200 });
  const sourceMs = frame => (frame - timing.from) / t.fps * 1000 * timing.playbackRate;
  assert.equal(sourceMs(174), 0);
  assert.ok(Math.abs(sourceMs(180) - 200 * 5000 / 6200) < 1e-9);
  assert.equal(sourceMs(360), 5000);
  t.clips[1].video_ms = 14_400_000;
  assert.equal(clipVideoTiming(parseTimeline(t), 1).playbackRate, 1);
  // Last included frame is 359; neither a long source nor slowing changes the window.
  assert.equal(timing.from + timing.durationInFrames, 360);
});

test('odd transition lengths and off-grid cuts retain exact fractional sequence clocks', () => {
  const t = fresh();
  t.transition_ms = 401;
  t.clips[0].end_ms = t.clips[1].start_ms = 6001;
  animate(t.clips[1], 5000);
  assert.deepEqual(clipWindow(t, t.clips[0]), { start_ms: 0, end_ms: 6202 });
  assert.deepEqual(clipWindow(t, t.clips[1]), { start_ms: 5800, end_ms: 12000 });
  assert.equal(clipVideoTiming(parseTimeline(t), 0).durationInFrames, 186.06);
  t.clips[0].end_ms = t.clips[1].start_ms = 6000;
  assert.equal(clipVideoTiming(parseTimeline(t), 1).from, 173.97);
});

test('middle clips pad both sides, while cuts preserve visibility but still use contract windows', () => {
  const t = fresh();
  t.clips = [0, 1, 2].map(i => ({ ...t.clips[0], index: i + 1, start_ms: i * 4000, end_ms: (i + 1) * 4000, transition_in: 'cut' }));
  t.clips.forEach(c => animate(c, 4000));
  assert.deepEqual(clipWindow(t, t.clips[1]), { start_ms: 3800, end_ms: 8200 });
  assert.deepEqual(clipVideoTiming(parseTimeline(t), 1), { from: 114, durationInFrames: 132, playbackRate: 4000 / 4400 });
  assert.equal(clipState(t, 1, 3800).visible, false);
  assert.equal(clipState(t, 1, 4000).visible, true);
  assert.equal(clipState(t, 1, 8000).visible, false);
  t.transition_ms = 0;
  assert.deepEqual(clipVideoTiming(parseTimeline(t), 1), { from: 120, durationInFrames: 120, playbackRate: 1 });
});

test('a single video window is clamped to both audio boundaries', () => {
  const t = fresh(); t.clips = [t.clips[0]]; t.clips[0].end_ms = t.duration_ms;
  t.clips[0].video_ms = 12000;
  assert.deepEqual(clipVideoTiming(parseTimeline(t), 0), { from: 0, durationInFrames: 360, playbackRate: 1 });
});
