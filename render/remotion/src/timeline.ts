/** Mirrors src/pipeline/timeline.py. Times and closing indices are absolute. */
export type Word = { text: string; start_ms: number; end_ms: number };
export type Clip = {
  index: number; image: string; image_sha256: string;
  start_ms: number; end_ms: number;
  effect: 'zoom_in' | 'zoom_out' | 'pan_left' | 'pan_right' | 'none';
  transition_in: 'cut' | 'crossfade';
};
export type TimelineClipV3 = Clip & {
  video: string | null; video_sha256: string | null; video_ms: number | null;
};
type Interval = { start_ms: number; end_ms: number };
export type ListOverlay = Interval & {
  kind: 'list'; title: string; items: string[]; active: number;
  item_start_ms: number; item_end_ms: number;
};
export type HighlightOverlay = Interval & { kind: 'highlight'; text: string };
export type ClosingOverlay = Interval & {
  kind: 'closing_sentence'; word_from: number; highlight_from: number;
};
export type Overlay = ListOverlay | HighlightOverlay | ClosingOverlay;
export type TimelineV2 = {
  schema_version: 2; format: 'short' | 'long'; width: number; height: number;
  fps: 30; duration_ms: number; audio_sha256: string; caption_ass_sha256: string;
  caption_template: 'word_pop' | 'phrase_blocks'; transition_ms: number;
  clips: Clip[]; words: Word[]; animation_style: 'explainer_v1'; overlays: Overlay[];
};
export type TimelineV3 = Omit<TimelineV2, 'schema_version' | 'clips'> & {
  schema_version: 3; clips: TimelineClipV3[];
};
export type Timeline = TimelineV2 | TimelineV3;

/** Mirrors clip_window, including ceil-half padding at cuts and audio bounds. */
export function clipWindow(t: Timeline, clip: Clip): { start_ms: number; end_ms: number } {
  const half = Math.ceil(t.transition_ms / 2);
  return { start_ms: Math.max(0, clip.start_ms - half), end_ms: Math.min(t.duration_ms, clip.end_ms + half) };
}

function requireValue(ok: unknown, field: string): asserts ok {
  if (!ok) throw new Error(field);
}
function object(value: unknown, keys: string, path: string): Record<string, unknown> {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value), path);
  const result = value as Record<string, unknown>;
  const expected = keys.split(' ');
  requireValue(Object.keys(result).length === expected.length && expected.every(k => Object.hasOwn(result, k)), `${path}: campos ausentes ou extras`);
  return result;
}
function integer(value: unknown, min: number, max: number, path: string): void {
  requireValue(typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max, path);
}
function string(value: unknown, min: number, max: number, path: string): void {
  requireValue(typeof value === 'string' && [...value].length >= min && [...value].length <= max, path);
}
function array(value: unknown, min: number, max: number, path: string): asserts value is unknown[] {
  requireValue(Array.isArray(value) && value.length >= min && value.length <= max, path);
}
function choice(value: unknown, choices: readonly unknown[], path: string): void {
  requireValue(choices.includes(value), path);
}
const MAX_MS = 14_400_000;
const HASH = /^[0-9a-f]{64}$/;
function hash(value: unknown, path: string): void {
  requireValue(typeof value === 'string' && HASH.test(value), path);
}
function interval(value: Record<string, unknown>, path: string, endMin = 1): void {
  integer(value.start_ms, 0, MAX_MS, `${path}.start_ms`);
  integer(value.end_ms, endMin, MAX_MS, `${path}.end_ms`);
}

/** Reject invalid inputs before loading media. Asset hashes remain lane D's job. */
export function parseTimeline(value: unknown): Timeline {
  try {
    return validateTimeline(value);
  } catch (error) {
    const version = value !== null && typeof value === 'object' && 'schema_version' in value && value.schema_version === 3 ? 3 : 2;
    throw new Error(`TimelineV${version} inválido: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function validateTimeline(value: unknown): Timeline {
  const t = object(value, 'schema_version format width height fps duration_ms audio_sha256 caption_ass_sha256 caption_template transition_ms clips words animation_style overlays', 'props');
  choice(t.schema_version, [2, 3], 'schema_version: expected 2 or 3');
  choice(t.format, ['short', 'long'], 'format');
  requireValue(t.width === (t.format === 'short' ? 1080 : 1920) && t.height === (t.format === 'short' ? 1920 : 1080), 'dimensões do formato');
  choice(t.fps, [30], 'fps');
  integer(t.duration_ms, 1000, MAX_MS, 'duration_ms');
  integer(t.transition_ms, 0, 1000, 'transition_ms');
  hash(t.audio_sha256, 'audio_sha256');
  hash(t.caption_ass_sha256, 'caption_ass_sha256');
  choice(t.caption_template, ['word_pop', 'phrase_blocks'], 'caption_template');
  choice(t.animation_style, ['explainer_v1'], 'animation_style');
  array(t.clips, 1, 600, 'clips');
  array(t.words, 0, 60000, 'words');
  array(t.overlays, 0, 20, 'overlays');
  t.clips.forEach((value, i) => {
    const p = `clips[${i}]`;
    const c = object(value, 'index image image_sha256 start_ms end_ms effect transition_in' + (t.schema_version === 3 ? ' video video_sha256 video_ms' : ''), p);
    integer(c.index, 1, 600, `${p}.index`);
    requireValue(typeof c.image === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png$/.test(c.image), `${p}.image`);
    hash(c.image_sha256, `${p}.image_sha256`);
    interval(c, p);
    choice(c.effect, ['none', 'zoom_in', 'zoom_out', 'pan_left', 'pan_right'], `${p}.effect`);
    choice(c.transition_in, ['cut', 'crossfade'], `${p}.transition_in`);
    if (t.schema_version === 3) {
      if (c.video !== null) requireValue(typeof c.video === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4$/.test(c.video), `${p}.video: expected a local MP4 filename`);
      if (c.video_sha256 !== null) hash(c.video_sha256, `${p}.video_sha256`);
      if (c.video_ms !== null) integer(c.video_ms, 1, MAX_MS, `${p}.video_ms`);
    }
  });
  t.words.forEach((value, i) => {
    const p = `words[${i}]`;
    const w = object(value, 'text start_ms end_ms', p);
    string(w.text, 1, 64, `${p}.text`);
    interval(w, p, 0);
  });
  t.overlays.forEach((value, i) => {
    const p = `overlays[${i}]`;
    requireValue(value !== null && typeof value === 'object', p);
    const kind = (value as Record<string, unknown>).kind;
    choice(kind, ['list', 'highlight', 'closing_sentence'], `${p}.kind`);
    const suffix = kind === 'list' ? 'title items active item_start_ms item_end_ms' : kind === 'highlight' ? 'text' : 'word_from highlight_from';
    const o = object(value, `kind start_ms end_ms ${suffix}`, p);
    interval(o, p);
    if (kind === 'list') {
      string(o.title, 1, 40, `${p}.title`);
      array(o.items, 2, 6, `${p}.items`);
      o.items.forEach(v => requireValue(typeof v === 'string', `${p}.items`));
      integer(o.active, 0, 5, `${p}.active`);
      integer(o.item_start_ms, 0, MAX_MS, `${p}.item_start_ms`);
      integer(o.item_end_ms, 0, MAX_MS, `${p}.item_end_ms`);
    } else if (kind === 'highlight') {
      string(o.text, 1, 80, `${p}.text`);
      requireValue((o.text as string).trim().split(/\s+/u).length <= 8, `${p}.text: máximo 8 palavras`);
    } else {
      integer(o.word_from, 0, 59999, `${p}.word_from`);
      integer(o.highlight_from, 0, 59999, `${p}.highlight_from`);
    }
  });
  const timeline = t as Timeline;
  let previousEnd = 0;
  timeline.clips.forEach((c, i, clips) => {
    requireValue(c.index === i + 1 && c.start_ms === previousEnd && c.end_ms - c.start_ms >= 1000, `clips[${i}]: ordem, continuidade ou duração`);
    requireValue(i > 0 || c.transition_in === 'cut', 'primeiro clip precisa de cut');
    if (c.transition_in === 'crossfade') {
      requireValue(timeline.transition_ms > 0 && timeline.transition_ms * 2 <= Math.min(c.end_ms - c.start_ms, clips[i - 1].end_ms - clips[i - 1].start_ms), `clips[${i}]: crossfade longo demais`);
    }
    previousEnd = c.end_ms;
  });
  requireValue(previousEnd === timeline.duration_ms, 'clips devem terminar com o áudio');
  previousEnd = 0;
  timeline.words.forEach((w, i) => {
    requireValue(w.start_ms >= previousEnd && w.end_ms >= w.start_ms && w.end_ms <= timeline.duration_ms, `words[${i}]: tempos fora de ordem ou do áudio`);
    previousEnd = w.end_ms;
  });
  previousEnd = 0;
  timeline.overlays.forEach((o, i) => {
    requireValue(o.start_ms >= previousEnd && o.end_ms - o.start_ms >= 1000 && o.end_ms <= timeline.duration_ms, `overlays[${i}]: sobreposição ou duração inválida`);
    previousEnd = o.end_ms;
    if (o.kind === 'list') requireValue(o.start_ms <= o.item_start_ms && o.item_start_ms <= o.item_end_ms && o.item_end_ms <= o.end_ms, `overlays[${i}]: item fora do cartão`);
    if (o.kind === 'closing_sentence') {
      requireValue(i === timeline.overlays.length - 1 && o.end_ms === timeline.duration_ms, 'closing_sentence deve encerrar o vídeo');
      requireValue(o.word_from <= o.highlight_from && o.highlight_from < timeline.words.length, 'closing_sentence: índices fora de words');
      requireValue(timeline.words[o.word_from].start_ms >= o.start_ms, 'closing_sentence começa depois da primeira palavra');
    }
  });
  const lists = timeline.overlays.filter((o): o is ListOverlay => o.kind === 'list');
  if (lists.length) {
    requireValue(lists.length === lists[0].items.length && lists.every((o, i) => o.active === i && o.title === lists[0].title && JSON.stringify(o.items) === JSON.stringify(lists[0].items)), 'list: um cartão por item, em ordem, com o mesmo título e itens');
  }
  if (timeline.schema_version === 3) {
    const videos = new Set<string>();
    for (const clip of timeline.clips) {
      if (clip.video === null && clip.video_sha256 === null && clip.video_ms === null) continue;
      requireValue(clip.video !== null && clip.video_sha256 !== null && clip.video_ms !== null, `clip ${clip.index} must set video, video_sha256 and video_ms together`);
      requireValue(clip.effect === 'none', `clip ${clip.index} plays a video and must have the effect none`);
      const window = clipWindow(timeline, clip);
      requireValue(clip.video_ms * 5 >= (window.end_ms - window.start_ms) * 4, `clip ${clip.index} has a video too short for its window (maximum stretch 1.25x)`);
      requireValue(!videos.has(clip.video), 'two clips play the same video');
      videos.add(clip.video);
    }
  }
  return timeline;
}

export function videoMetadata(value: unknown): { width: number; height: number; fps: 30; durationInFrames: number } {
  const t = parseTimeline(value);
  return { width: t.width, height: t.height, fps: 30, durationInFrames: Math.ceil(t.duration_ms * 30 / 1000) };
}
