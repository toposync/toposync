declare module "@cycjimmy/jsmpeg-player" {
export type JSMpegPlayerInstance = {
  destroy?: () => void;
  stop?: () => void;
  play?: () => void;
};

  export type JSMpegPlayerOptions = {
    canvas?: HTMLCanvasElement;
    autoplay?: boolean;
    audio?: boolean;
    videoBufferSize?: number;
    onSourceEstablished?: () => void;
    onSourceCompleted?: () => void;
    onStalled?: () => void;
  onVideoDecode?: () => void;
  onError?: (error: unknown) => void;
};

type JSMpegModule = {
  Player: new (url: string, options?: JSMpegPlayerOptions) => JSMpegPlayerInstance;
};

const JSMpeg: JSMpegModule;
export default JSMpeg;
export const Player: JSMpegModule["Player"];
}
