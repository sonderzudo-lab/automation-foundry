import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import React from 'react';
import * as remotion from 'remotion';
import ts from 'typescript';
import * as timeline from '../src/timeline.ts';
import * as timing from '../src/timing.ts';

const example = JSON.parse(readFileSync(new URL('../../../docs/reference/schemas/timeline-v3.example.json', import.meta.url), 'utf8'));
const source = readFileSync(new URL('../src/ExplainerV1.tsx', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React, esModuleInterop: true },
});

// Inspect the real composition's element tree without a browser or media decoding.
// Only hooks and staticFile are stubbed; Sequence/OffthreadVideo keep their actual identities.
function composition(props, frame) {
  const exports = {};
  const dependencies = {
    react: { ...React, useMemo: fn => fn() },
    remotion: { ...remotion, useCurrentFrame: () => frame, staticFile: name => `/static/${name}` },
    './timeline': timeline,
    './timing': timing,
  };
  runInNewContext(outputText, { exports, require: name => {
    assert.ok(Object.hasOwn(dependencies, name), `Unexpected dependency ${name}`);
    return dependencies[name];
  } });
  return exports.ExplainerV1(props);
}
function nodes(element, type) {
  if (!React.isValidElement(element)) return [];
  return [
    ...(element.type === type ? [element] : []),
    ...React.Children.toArray(element.props.children).flatMap(child => nodes(child, type)),
  ];
}

test('ExplainerV1 replaces only video clips, mutes them, and keeps narration and fallback zoom', () => {
  const tree = composition(example, 180);
  const videos = nodes(tree, remotion.OffthreadVideo);
  assert.equal(videos.length, 1);
  assert.equal(videos[0].props.src, '/static/scene-001.mp4');
  assert.equal(videos[0].props.muted, true);
  assert.equal(videos[0].props.playbackRate, 1);
  assert.equal(videos[0].props.style.objectFit, 'cover');
  assert.equal(videos[0].props.style.transform, undefined);
  const images = nodes(tree, remotion.Img);
  assert.equal(images.length, 1);
  assert.equal(images[0].props.src, '/static/visual-002.png');
  assert.match(images[0].props.style.transform, /scale\(1.12\)/);
  const audio = nodes(tree, remotion.Audio);
  assert.equal(audio.length, 1);
  assert.equal(audio[0].props.src, '/static/narration.wav');
});

test('video sequences offset source time from window start and cut at the exclusive window end', () => {
  const t = structuredClone(example);
  Object.assign(t.clips[1], { video: 'scene-002.mp4', video_sha256: 'e'.repeat(64), video_ms: 4960, effect: 'none' });
  const tree = composition(t, 180);
  const sequences = nodes(tree, remotion.Sequence);
  assert.equal(sequences.length, 2);
  const [first, second] = sequences.map(node => node.props);
  // First video is 6440 ms long, but its sequence excludes frame 186 (6200 ms).
  assert.equal(first.from, 0);
  assert.equal(first.durationInFrames, 186);
  assert.equal(second.from, 174); // 5800 ms, including incoming crossfade.
  assert.equal(second.from + second.durationInFrames, 360);
  assert.equal(second.layout, 'none');
  const video = nodes(sequences[1], remotion.OffthreadVideo)[0];
  assert.equal(video.props.playbackRate, .8);
  assert.equal(video.props.trimBefore, undefined); // First source frame; no seek into the file.
  assert.equal(video.props.startFrom, undefined);
  assert.equal(video.props.loop, undefined);
  assert.equal(first.children.props.style.opacity, 1);
  assert.equal(second.children.props.style.opacity, .5);
  assert.equal(nodes(composition(t, 174), remotion.Sequence)[1].props.children.props.style.opacity, 0);
  assert.equal(nodes(composition(t, 186), remotion.Sequence)[1].props.children.props.style.opacity, 1);
});

test('v2 and all-null v3 clips keep the image-only composition path', () => {
  const t = structuredClone(example);
  Object.assign(t.clips[0], { video: null, video_sha256: null, video_ms: null, effect: 'zoom_in' });
  for (const version of [3, 2]) {
    t.schema_version = version;
    if (version === 2) t.clips.forEach(clip => {
      delete clip.video; delete clip.video_sha256; delete clip.video_ms;
    });
    const tree = composition(t, 180);
    assert.equal(nodes(tree, remotion.OffthreadVideo).length, 0);
    assert.equal(nodes(tree, remotion.Sequence).length, 0);
    assert.equal(nodes(tree, remotion.Img).length, 2);
    assert.equal(timing.clipVideoTiming(t, 0), null);
  }
});
