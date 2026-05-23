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
    loop?: boolean;
    disableGl?: boolean;
    preserveDrawingBuffer?: boolean;
    reconnectInterval?: number;
    onSourceEstablished?: () => void;
    onSourceCompleted?: () => void;
    onStalled?: () => void;
    onVideoDecode?: () => void;
    onError?: (error: unknown) => void;
  };

  type JSMpegModule = {
    Player: new (url: string, options?: JSMpegPlayerOptions) => JSMpegPlayerInstance;
    VideoElement: new (
      wrapper: string | Element,
      url: string,
      options?: JSMpegPlayerOptions & Record<string, unknown>,
      overlayOptions?: Record<string, unknown>,
    ) => JSMpegPlayerInstance & { player?: JSMpegPlayerInstance };
  };

  const JSMpeg: JSMpegModule;
  export default JSMpeg;
  export const Player: JSMpegModule["Player"];
  export const VideoElement: new (
    wrapper: string | Element,
    url: string,
    options?: JSMpegPlayerOptions & Record<string, unknown>,
    overlayOptions?: Record<string, unknown>,
  ) => JSMpegPlayerInstance & { player?: JSMpegPlayerInstance };
}
