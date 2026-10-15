import React from 'react';
import { Composition } from 'remotion';
import { ExplainerV1 } from './ExplainerV1';
import { videoMetadata } from './timeline';

export const Root: React.FC = () => (
  <Composition id="ExplainerV1" component={ExplainerV1} defaultProps={{}}
    calculateMetadata={({ props }) => ({ ...videoMetadata(props), defaultCodec: 'h264', defaultPixelFormat: 'yuv420p' })} />
);
