import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { parseTimeline, videoMetadata } from '../src/timeline.ts';
import { activeWord, badgeFlight, clipState, closingWords, listLayout, overlayOpacity, phraseBlocks, progress } from '../src/timing.ts';

const example = JSON.parse(readFileSync(new URL('../../../docs/reference/schemas/timeline-v2.example.json', import.meta.url), 'utf8'));
const fresh = () => structuredClone(example);

test('canonical TimelineV2 needs no extra props; dimensions and duration come from props', () => {
  assert.deepEqual(parseTimeline(example), example);
  assert.deepEqual(videoMetadata(example), { width: 1080, height: 1920, fps: 30, durationInFrames: 360 });
  const t = fresh();
  Object.assign(t, { format: 'long', width: 1920, height: 1080, duration_ms: 12001 });
  t.clips.at(-1).end_ms = 12001;
  t.overlays.at(-1).end_ms = 12001;
  assert.deepEqual(videoMetadata(t), { width: 1920, height: 1080, fps: 30, durationInFrames: 361 });
});

test('crossfade is centred on the cut with no black dip and no fade at the end', () => {
  assert.equal(clipState(example, 1, 5799).visible, false);
  for (const [now, opacity] of [[5800, 0], [6000, .5], [6200, 1]]) {
    assert.equal(clipState(example, 1, now).opacity, opacity);
    if (now < 6200) {
      assert.equal(clipState(example, 0, now).visible, true);
      assert.equal(clipState(example, 0, now).opacity, 1);
    }
  }
  assert.equal(clipState(example, 0, 6200).visible, false);
  assert.equal(clipState(example, 1, 11999).opacity, 1);
});

test('cut has no pre-roll; all five effects use their own clip duration', () => {
  const t = fresh(); t.clips[1].transition_in = 'cut'; t.transition_ms = 0;
  assert.equal(clipState(t, 1, 5999).visible, false);
  assert.equal(clipState(t, 0, 6000).visible, false);
  assert.equal(clipState(t, 1, 6000).opacity, 1);
  assert.equal(clipState(t, 0, 3000).scale, 1.06);
  assert.ok(Math.abs(clipState(t, 1, 9000).scale - 1.06) < 1e-9);
  for (const effect of ['none', 'pan_left', 'pan_right']) {
    t.clips[1].effect = effect;
    const start = clipState(t, 1, 6000); const end = clipState(t, 1, 12000);
    if (effect === 'none') assert.deepEqual([start.scale, end.scale, start.xPercent], [1, 1, 0]);
    else {
      assert.equal(start.scale, 1.12);
      assert.equal(start.xPercent, -end.xPercent);
      assert.equal(start.xPercent > end.xPercent, effect === 'pan_left');
      assert.ok(Math.abs(start.xPercent) <= (start.scale - 1) * 50);
    }
  }
});

test('captions exclude silence and zero-length words; phrase blocks preserve word indices', () => {
  for (const [now, index] of [[499, -1], [500, 0], [1000, 1], [1700, -1], [3999, -1]]) assert.equal(activeWord(example.words, now), index);
  assert.equal(activeWord([{ text: 'zero', start_ms: 0, end_ms: 0 }], 0), -1);
  assert.equal(activeWord([], 0), -1);
  assert.deepEqual(phraseBlocks(example.words), [{ from: 0, to: 3 }, { from: 3, to: 6 }, { from: 6, to: 9 }, { from: 9, to: 12 }]);
});

test('badge leaves after item completion, holds the label, returns checked', () => {
  assert.equal(badgeFlight(example, 1969), null);
  assert.equal(badgeFlight(example, 1970).corner, 0);
  assert.equal(badgeFlight(example, 2800).corner, 1);
  const home = badgeFlight(example, 3900);
  assert.equal(home.card.active, 0); assert.equal(home.corner, .5); assert.equal(home.check, .5);
  assert.equal(badgeFlight(example, 4000), null);
  assert.equal(badgeFlight(example, 8000).card.active, 1);
  assert.equal(badgeFlight(example, 8900), null);
  assert.equal(progress(1700, 500, 1700), 1);
  assert.equal(progress(500, 500, 500), 1);
});

test('closing sentence uses absolute indices and the original word timings', () => {
  const words = closingWords(example.words, example.overlays.at(-1));
  assert.deepEqual(words.map(w => [w.text, w.highlighted, w.start_ms]), [
    ['Deixar', false, 9000], ['longe', true, 9400], ['ajuda.', true, 9900],
  ]);
});

test('adjacent cards and item ending at the cut never produce invalid badge values', () => {
  const t = fresh();
  t.overlays[0].item_end_ms = t.overlays[0].end_ms;
  t.overlays[1].start_ms = t.overlays[0].end_ms;
  t.overlays[1].item_start_ms = t.overlays[1].start_ms;
  assert.equal(badgeFlight(t, 2300), null);
  for (let now = 0; now < 12000; now += 1000 / 30) {
    const f = badgeFlight(t, now);
    if (f) for (const n of [f.corner, f.check, f.opacity]) assert.ok(Number.isFinite(n) && n >= 0 && n <= 1);
  }
});

test('five- and six-item layouts fit above captions in both formats; overlay fade is local', () => {
  for (const [w, h] of [[1080, 1920], [1920, 1080]]) {
    for (const count of [5, 6]) {
      const l = listLayout(w, h, count);
      assert.ok(l.titleY > h * .08);
      assert.ok(l.firstY + l.gap * (count - 1) + l.badge / 2 < h * .76);
      assert.ok(l.gap > l.badge);
    }
  }
  assert.equal(overlayOpacity(300, 300, 2300), 0);
  assert.equal(overlayOpacity(1000, 300, 2300), 1);
  assert.equal(overlayOpacity(2300, 300, 2300), 0);
});

for (const [name, mutate] of [
  ['extra props', t => { t.audio = 'elsewhere.wav'; }],
  ['remote image', t => { t.clips[0].image = 'https://host/image.png'; }],
  ['path traversal', t => { t.clips[0].image = '../image.png'; }],
  ['clip gap', t => { t.clips[1].start_ms++; }],
  ['first crossfade', t => { t.clips[0].transition_in = 'crossfade'; }],
  ['zero crossfade', t => { t.transition_ms = 0; }],
  ['overlapping words', t => { t.words[1].start_ms = 999; }],
  ['overlay overlap', t => { t.overlays[1].start_ms = 2000; }],
  ['item outside card', t => { t.overlays[0].item_end_ms = 2400; }],
  ['inconsistent list', t => { t.overlays[1].items[0] = 'Outra'; }],
  ['missing list item', t => { t.overlays.splice(1, 1); }],
  ['closing outside words', t => { t.overlays.at(-1).highlight_from = 12; }],
  ['closing starts after word', t => { t.overlays.at(-1).start_ms = 9200; }],
  ['wrong dimensions', t => { t.width = 1920; }],
]) test(`invalid props fail clearly: ${name}`, () => {
  const t = fresh(); mutate(t);
  assert.throws(() => parseTimeline(t), /TimelineV2 inválido/);
});

test('empty overlays and words are valid; phrase_blocks needs no extra fields', () => {
  const t = fresh(); t.words = []; t.overlays = []; t.caption_template = 'phrase_blocks';
  assert.equal(parseTimeline(t), t);
  assert.equal(badgeFlight(t, 3000), null);
});
