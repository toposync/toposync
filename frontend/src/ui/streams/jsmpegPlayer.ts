import JSMpeg, { type JSMpegPlayerInstance } from "@cycjimmy/jsmpeg-player";

export type ToposyncJsmpegPlayer = {
  destroy: () => void;
};

export type ToposyncJsmpegPlayerOptions = {
  canvas: HTMLCanvasElement;
  onSourceEstablished?: () => void;
  onSourceCompleted?: () => void;
  onStalled?: () => void;
  onVideoDecode?: () => void;
  onError?: (error: unknown) => void;
};

export function createJsmpegPlayer(url: string, options: ToposyncJsmpegPlayerOptions): ToposyncJsmpegPlayer {
  const player = new JSMpeg.Player(url, {
    canvas: options.canvas,
    autoplay: true,
    audio: false,
    loop: false,
    preserveDrawingBuffer: true,
    reconnectInterval: 0,
    onSourceEstablished: options.onSourceEstablished,
    onSourceCompleted: options.onSourceCompleted,
    onStalled: options.onStalled,
    onVideoDecode: options.onVideoDecode,
    onError: options.onError,
  }) as JSMpegPlayerInstance;

  return {
    destroy: () => {
      try {
        player.destroy?.();
      } catch {
        player.stop?.();
      }
    },
  };
}
