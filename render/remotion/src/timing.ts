import { clipWindow } from './timeline.ts';
import type { Clip, ClosingOverlay, ListOverlay, Timeline, Word } from './timeline.ts';

/** Sequence offsets stay fractional: the source clock starts at the exact window start. */
export function clipVideoTiming(t: Timeline, i: number): { from: number; durationInFrames: number; playbackRate: number } | null {
  if (t.schema_version !== 3) return null;
  const clip = t.clips[i];
  if (clip.video === null || clip.video_ms === null) return null;
  const window = clipWindow(t, clip);
  const length = window.end_ms - window.start_ms;
  const from = window.start_ms * t.fps / 1000;
  return {
    from,
    durationInFrames: window.end_ms * t.fps / 1000 - from,
    playbackRate: Math.min(1, clip.video_ms / length),
  };
}

export const clamp01 = (n: number): number => Math.max(0, Math.min(1, n));
export function progress(now: number, start: number, end: number): number {
  return end <= start ? (now >= start ? 1 : 0) : clamp01((now - start) / (end - start));
}
export function ease(n: number): number {
  const t = clamp01(n);
  return t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
}

/** Incoming image is composited over an opaque outgoing image: no black dip. */
export function clipState(t: Timeline, i: number, now: number): { visible: boolean; opacity: number; scale: number; xPercent: number } {
  const c = t.clips[i];
  const next = t.clips[i + 1];
  const start = c.start_ms - (c.transition_in === 'crossfade' ? t.transition_ms / 2 : 0);
  const end = c.end_ms + (next?.transition_in === 'crossfade' ? t.transition_ms / 2 : 0);
  const p = progress(now, c.start_ms, c.end_ms);
  return {
    visible: now >= start && now < end,
    opacity: c.transition_in === 'crossfade' ? progress(now, start, start + t.transition_ms) : 1,
    ...clipMotion(c.effect, p),
  };
}
export function clipMotion(effect: Clip['effect'], p: number): { scale: number; xPercent: number } {
  const t = clamp01(p);
  switch (effect) {
    case 'zoom_in': return { scale: 1 + .12 * t, xPercent: 0 };
    case 'zoom_out': return { scale: 1.12 - .12 * t, xPercent: 0 };
    case 'pan_left': return { scale: 1.12, xPercent: 5 - 10 * t };
    case 'pan_right': return { scale: 1.12, xPercent: -5 + 10 * t };
    case 'none': return { scale: 1, xPercent: 0 };
  }
}

export function overlayOpacity(now: number, start: number, end: number): number {
  const fade = Math.min(330, (end - start) / 3);
  return progress(now, start, start + fade) * (1 - progress(now, end - fade, end));
}

/** Captions follow [start, end); silence never holds the previous word. */
export function activeWord(words: Word[], now: number): number {
  let low = 0;
  let high = words.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >>> 1;
    if (words[mid].start_ms <= now) { found = mid; low = mid + 1; }
    else high = mid - 1;
  }
  return found >= 0 && now < words[found].end_ms ? found : -1;
}
export type Phrase = { from: number; to: number };
export function closingWords(words: Word[], card: ClosingOverlay): (Word & { highlighted: boolean })[] {
  return words.slice(card.word_from).map((word, offset) => ({
    ...word, highlighted: card.word_from + offset >= card.highlight_from,
  }));
}

/** Words carry no ASS event boundary; group at punctuation, silence, or 6 words. */
export function phraseBlocks(words: Word[]): Phrase[] {
  const phrases: Phrase[] = [];
  let from = 0;
  let characters = 0;
  words.forEach((w, i) => {
    characters += w.text.length + 1;
    const next = words[i + 1];
    if (!next || i - from >= 5 || characters + next.text.length > 42 || /[.!?;:]$/u.test(w.text) || next.start_ms - w.end_ms >= 350) {
      phrases.push({ from, to: i + 1 });
      from = i + 1;
      characters = 0;
    }
  });
  return phrases;
}

export type ListLayout = { left: number; badge: number; firstY: number; gap: number; titleY: number; cornerX: number; cornerY: number };
export function listLayout(width: number, height: number, count: number): ListLayout {
  const portrait = height > width;
  const gap = Math.min(portrait ? 180 : 120, height * .42 / Math.max(1, count - 1));
  const firstY = height * .46 - (count - 1) * gap / 2;
  return { left: width * .0833, badge: portrait ? 110 : 80, firstY, gap, titleY: firstY - (portrait ? 200 : 130), cornerX: width * .111, cornerY: height * .078 };
}
export type BadgeFlight = { card: ListOverlay; corner: number; check: number; opacity: number };
export function departure(card: ListOverlay): number {
  return Math.max(card.item_end_ms, card.end_ms - 330);
}
export function returnEnd(card: ListOverlay): number {
  return Math.min(card.start_ms + 330, card.item_start_ms);
}
/** At most one badge travels. A zero-gap handoff goes directly to the next row. */
export function badgeFlight(t: Timeline, now: number): BadgeFlight | null {
  const cards = t.overlays.filter((o): o is ListOverlay => o.kind === 'list');
  const closing = t.overlays.find(o => o.kind === 'closing_sentence');
  for (let i = 0; i < cards.length; i++) {
    const card = cards[i];
    const next = cards[i + 1];
    const depart = departure(card);
    const end = next ? returnEnd(next) : closing?.start_ms ?? t.duration_ms;
    if (now < depart || now >= end) continue;
    const cornerAt = Math.min(depart + 700, next?.start_ms ?? end);
    if (cornerAt <= depart) return null;
    const out = ease(progress(now, depart, cornerAt));
    const home = next ? ease(progress(now, next.start_ms, end)) : 0;
    return { card, corner: out * (1 - home), check: home, opacity: next ? 1 : 1 - progress(now, end - Math.min(400, (end - depart) / 2), end) };
  }
  return null;
}
