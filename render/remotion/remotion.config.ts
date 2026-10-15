import { Config } from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setCodec('h264');
Config.setAudioCodec('aac');
Config.setPixelFormat('yuv420p');
Config.setConcurrency(1);
