import React, { useMemo } from 'react';
import { AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, spring, staticFile, useCurrentFrame, useVideoConfig } from 'remotion';
import { parseTimeline } from './timeline';
import type { ClosingOverlay, ListOverlay, Overlay, Timeline } from './timeline';
import { activeWord, badgeFlight, clamp01, clipState, clipVideoTiming, closingWords, ease, listLayout, overlayOpacity, phraseBlocks, progress, returnEnd } from './timing';
import type { BadgeFlight } from './timing';

const ACCENT = '#FFC83D';
const DONE = '#3DDC97';
const TEXT_FONT = "'Segoe UI', Arial, sans-serif";
const POP_FONT = "'Arial Black', 'Segoe UI Black', Arial, sans-serif";

export const ExplainerV1: React.FC<Record<string, unknown>> = (props) => {
  const timeline = useMemo(() => parseTimeline(props), [props]);
  const frame = useCurrentFrame();
  const now = frame * 1000 / 30;
  const overlay = timeline.overlays.find(o => now >= o.start_ms && now < o.end_ms);
  const flight = badgeFlight(timeline, now);
  return (
    <AbsoluteFill style={{ backgroundColor: '#0b0d12', overflow: 'hidden' }}>
      {timeline.clips.map((clip, index) => {
        const state = clipState(timeline, index, now);
        const videoTiming = clipVideoTiming(timeline, index);
        if (videoTiming && 'video' in clip && clip.video) return (
          <Sequence key={clip.index} from={videoTiming.from} durationInFrames={videoTiming.durationInFrames} layout="none">
            <AbsoluteFill style={{ opacity: state.visible ? state.opacity : 0 }}>
              <OffthreadVideo src={staticFile(clip.video)} muted playbackRate={videoTiming.playbackRate}
                style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            </AbsoluteFill>
          </Sequence>
        );
        if (!state.visible) return null;
        return <AbsoluteFill key={clip.index} style={{ opacity: state.opacity }}>
          <Img src={staticFile(clip.image)} style={{
            width: '100%', height: '100%', objectFit: 'cover',
            transform: `translateX(${state.xPercent}%) scale(${state.scale})`,
          }} />
        </AbsoluteFill>;
      })}
      {overlay && <OverlayCard timeline={timeline} overlay={overlay} now={now} flight={flight} />}
      {flight && <TravelBadge timeline={timeline} flight={flight} />}
      {overlay?.kind !== 'closing_sentence' && <Captions timeline={timeline} now={now} lowered={Boolean(overlay)} />}
      <div style={{ position: 'absolute', top: 0, left: 0, height: timeline.height > timeline.width ? 12 : 8,
        width: `${100 * clamp01(frame / Math.max(1, Math.ceil(timeline.duration_ms * .03) - 1))}%`, background: ACCENT }} />
      <Audio src={staticFile('narration.wav')} />
    </AbsoluteFill>
  );
};

const OverlayCard: React.FC<{ timeline: Timeline; overlay: Overlay; now: number; flight: BadgeFlight | null }> = ({ timeline, overlay, now, flight }) => (
  <AbsoluteFill style={{
    opacity: overlayOpacity(now, overlay.start_ms, overlay.end_ms),
    background: 'linear-gradient(180deg, rgba(8,10,16,0.78), rgba(8,10,16,0.92))',
    justifyContent: 'center', alignItems: 'center', color: 'white',
  }}>
    {overlay.kind === 'list' ? <ListPanel timeline={timeline} card={overlay} now={now} flight={flight} />
      : overlay.kind === 'closing_sentence' ? <Closing timeline={timeline} card={overlay} now={now} />
        : <div style={{ width: '84%', textAlign: 'center', fontFamily: POP_FONT, color: ACCENT,
          fontSize: timeline.format === 'short' ? 96 : 86, lineHeight: 1.15, overflowWrap: 'anywhere',
          transform: `translateY(${(1 - ease(progress(now, overlay.start_ms, overlay.start_ms + 330))) * 40}px)` }}>
          {overlay.text}
        </div>}
  </AbsoluteFill>
);

const ListPanel: React.FC<{ timeline: Timeline; card: ListOverlay; now: number; flight: BadgeFlight | null }> = ({ timeline, card, now, flight }) => {
  const l = listLayout(timeline.width, timeline.height, card.items.length);
  const portrait = timeline.format === 'short';
  const entered = ease(progress(now, card.item_start_ms, card.item_end_ms));
  return <AbsoluteFill style={{ fontFamily: TEXT_FONT }}>
    <div style={{ position: 'absolute', left: l.left, top: l.titleY, width: '84%', fontSize: portrait ? 46 : 38,
      letterSpacing: 6, textTransform: 'uppercase', color: ACCENT, fontWeight: 700, overflowWrap: 'anywhere' }}>{card.title}</div>
    {card.items.map((item, index) => {
      const active = index === card.active;
      const spoken = now >= card.item_start_ms;
      const hidden = index > card.active || (active && !spoken);
      const travelling = flight?.card.active === index;
      // The previous number becomes a check only when its returning badge lands.
      const check = index < card.active && (index < card.active - 1 || now >= returnEnd(card)) ? 1 : 0;
      return <div key={index} style={{ position: 'absolute', left: l.left, top: l.firstY + index * l.gap - l.badge / 2,
        width: timeline.width - 2 * l.left, minHeight: l.badge, display: 'flex', alignItems: 'center', gap: portrait ? 36 : 28,
        opacity: hidden ? .28 : 1, transform: active && spoken ? `translateX(${(1 - entered) * -120}px)` : undefined }}>
        <div style={{ visibility: travelling ? 'hidden' : 'visible' }}>
          <Badge number={index + 1} check={check} highlight={(active && spoken) || index < card.active} size={l.badge} />
        </div>
        <div style={{ fontSize: portrait ? (active ? 70 : 60) : (active ? 56 : 48), fontWeight: 800,
          lineHeight: 1.05, overflowWrap: 'anywhere', flex: 1, minWidth: 0 }}>
          {hidden ? '• • •' : item}
        </div>
      </div>;
    })}
  </AbsoluteFill>;
};

const Badge: React.FC<{ number: number; check: number; highlight: boolean; size: number }> = ({ number, check, highlight, size }) => (
  <div style={{ width: size, height: size, flex: '0 0 auto', borderRadius: '50%',
    background: check > .5 ? DONE : highlight ? ACCENT : 'rgba(255,255,255,0.15)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#0b0d12',
    fontSize: size * .545, fontWeight: 900, fontFamily: POP_FONT }}>
    {check > .5 ? <svg width={size * .58} height={size * .58} viewBox="0 0 24 24">
      <path d="M4 12.5l5 5L20 6.5" fill="none" stroke="#0b0d12" strokeWidth={3.2} strokeLinecap="round"
        strokeLinejoin="round" strokeDasharray={30} strokeDashoffset={30 * (1 - clamp01((check - .5) * 2))} />
    </svg> : number}
  </div>
);

const TravelBadge: React.FC<{ timeline: Timeline; flight: BadgeFlight }> = ({ timeline, flight }) => {
  const { card, corner: t } = flight;
  const l = listLayout(timeline.width, timeline.height, card.items.length);
  const rowX = l.left + l.badge / 2;
  const rowY = l.firstY + card.active * l.gap;
  return <div style={{ position: 'absolute', left: rowX + (l.cornerX - rowX) * t - l.badge / 2,
    top: rowY + (l.cornerY - rowY) * t - l.badge / 2, display: 'flex', alignItems: 'center', gap: 22,
    opacity: flight.opacity, transform: `scale(${1 - .3 * t})`, transformOrigin: `${l.badge / 2}px ${l.badge / 2}px`,
    maxWidth: timeline.width * .86, filter: 'drop-shadow(0 8px 18px rgba(0,0,0,0.5))' }}>
    <Badge number={card.active + 1} check={flight.check} highlight size={l.badge} />
    <div style={{ opacity: progress(t, .85, 1), fontFamily: TEXT_FONT, fontWeight: 800,
      fontSize: timeline.format === 'short' ? 48 : 40, color: 'white', background: 'rgba(8,10,16,0.7)',
      padding: '14px 30px', borderRadius: 40, overflowWrap: 'anywhere' }}>{card.items[card.active]}</div>
  </div>;
};

const Closing: React.FC<{ timeline: Timeline; card: ClosingOverlay; now: number }> = ({ timeline, card, now }) => {
  const { fps } = useVideoConfig();
  const sentence = useMemo(() => closingWords(timeline.words, card), [timeline.words, card]);
  // Bound long closing sentences to the safe central area; font remains a system font.
  const characters = sentence.reduce((sum, w) => sum + w.text.length + 1, 0);
  const size = Math.min(timeline.format === 'short' ? 92 : 84, Math.sqrt(timeline.width * .82 * timeline.height * .55 / Math.max(1, characters) / .7));
  return <div style={{ width: '85%', display: 'flex', flexWrap: 'wrap', gap: `${size * .11}px ${size * .24}px`,
    justifyContent: 'center', fontFamily: POP_FONT }}>
    {sentence.map((word, index) => {
      const appear = spring({ frame: (now - word.start_ms) * fps / 1000, fps, config: { damping: 12 } });
      return <span key={index} style={{ fontSize: size, lineHeight: 1.15, maxWidth: '100%', overflowWrap: 'anywhere',
        color: word.highlighted ? ACCENT : 'white',
        opacity: now >= word.start_ms ? 1 : 0,
        transform: `translateY(${(1 - appear) * 40}px) scale(${.85 + .15 * appear})`, display: 'inline-block' }}>{word.text}</span>;
    })}
  </div>;
};

const Captions: React.FC<{ timeline: Timeline; now: number; lowered: boolean }> = ({ timeline, now, lowered }) => {
  const { fps } = useVideoConfig();
  const phrases = useMemo(() => phraseBlocks(timeline.words), [timeline.words]);
  const index = activeWord(timeline.words, now);
  if (index < 0) return null;
  const word = timeline.words[index];
  const pop = spring({ frame: (now - word.start_ms) * fps / 1000, fps, config: { damping: 11, stiffness: 180 } });
  const phrase = phrases.find(p => index >= p.from && index < p.to);
  const base: React.CSSProperties = { position: 'absolute', left: '7%', width: '86%',
    top: `${lowered ? 76 : 63}%`, textAlign: 'center', fontFamily: POP_FONT,
    textShadow: '0 10px 30px rgba(0,0,0,0.45)', overflowWrap: 'anywhere', lineHeight: 1.15 };
  if (timeline.caption_template === 'phrase_blocks' && phrase) return <div style={{ ...base,
    fontSize: timeline.format === 'short' ? 62 : 56, color: 'white', background: 'rgba(8,10,16,0.8)',
    padding: '20px 26px', borderRadius: 24, boxSizing: 'border-box' }}>
    {timeline.words.slice(phrase.from, phrase.to).map((w, offset) => <React.Fragment key={offset}>
      {offset > 0 ? ' ' : ''}<span style={{ color: phrase.from + offset <= index ? ACCENT : 'white' }}>{w.text}</span>
    </React.Fragment>)}
  </div>;
  const text = word.text.replace(/[.,!?:;]+$/u, '') || word.text;
  const fontSize = Math.min(timeline.format === 'short' ? 124 : 110, timeline.width * .8 / Math.max(1, [...text].length) / .78);
  return <div style={{ ...base, fontSize, color: index % 4 === 3 ? ACCENT : 'white',
    WebkitTextStroke: `${Math.min(14, fontSize * .11)}px #000`, paintOrder: 'stroke', textTransform: 'uppercase',
    transform: `scale(${.6 + .4 * pop}) rotate(${(1 - pop) * -4}deg)` }}>{text}</div>;
};
