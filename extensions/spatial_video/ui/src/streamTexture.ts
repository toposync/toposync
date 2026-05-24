import Hls from "hls.js";
import JSMpeg from "@cycjimmy/jsmpeg-player";
import * as THREE from "three";

import {
  fetchLiveViewPlayback,
  heartbeatTransmissionDemand,
  primeTransmissionDemand,
} from "./api";
import type { ProjectionCandidate, StreamingOutputUrl, StreamingPlaybackResponse, StreamingTransport } from "./types";

type StreamTextureStatus = "idle" | "loading" | "playing" | "error";

export type StreamTextureSnapshot = {
  status: StreamTextureStatus;
  message: string;
  transport: StreamingTransport | null;
  texture: THREE.Texture | null;
};

type Listener = () => void;

const HEARTBEAT_TTL_SECONDS = 45;
const MSE_CODEC_REQUEST = "avc1.640029,avc1.64002A,avc1.640033,avc1.42E01E,mp4a.40.2,opus";
const MSE_INIT_TIMEOUT_MS = 6000;
const MSE_FIRST_FRAME_TIMEOUT_MS = 7000;
const FIRST_FRAME_TIMEOUT_MS = 14000;

function asMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function websocketUrl(url: string): string {
  if (url.startsWith("ws://") || url.startsWith("wss://")) return url;
  if (url.startsWith("http://")) return `ws://${url.slice("http://".length)}`;
  if (url.startsWith("https://")) return `wss://${url.slice("https://".length)}`;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${url.startsWith("/") ? url : `/${url}`}`;
}

function chooseTransports(playback: StreamingPlaybackResponse): Array<{
  transport: StreamingTransport;
  output: StreamingOutputUrl;
}> {
  const outputs = playback.urls.outputs ?? [];
  const byTransport = new Map<string, StreamingOutputUrl>();
  for (const output of outputs) {
    if (!byTransport.has(output.protocol)) byTransport.set(output.protocol, output);
  }
  const seen = new Set<string>();
  const out: Array<{ transport: StreamingTransport; output: StreamingOutputUrl }> = [];
  const push = (transport: StreamingTransport, output: StreamingOutputUrl | null | undefined) => {
    if (!output || transport === "webrtc") return;
    const key = `${transport}:${output.output_id}:${output.url}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ transport, output });
  };

  const planned = [...(playback.playback_plan?.transports ?? [])]
    .filter((item) => item.available && item.transport !== "webrtc")
    .sort((a, b) => a.rank - b.rank);

  for (const item of planned) {
    const output =
      (item.url ? ({ output_id: item.output_id || item.transport, protocol: item.transport, url: item.url } as StreamingOutputUrl) : null) ??
      (item.output_id ? outputs.find((candidate) => candidate.output_id === item.output_id && candidate.protocol === item.transport) : null) ??
      byTransport.get(item.transport);
    if (output && (item.transport === "mse" || item.transport === "hls" || item.transport === "jsmpeg")) {
      push(item.transport, output);
    }
  }

  for (const transport of ["mse", "hls", "jsmpeg"] as const) {
    push(transport, byTransport.get(transport));
  }
  return out;
}

function createMediaSourceMime(raw: string): string | null {
  const value = raw.trim();
  if (!value) return null;
  if (value.includes("video/mp4")) return value;
  if (/(avc1|hvc1|hev1|mp4a|opus)/i.test(value)) return `video/mp4; codecs="${value.replace(/^codecs=/i, "").replace(/^"|"$/g, "")}"`;
  return null;
}

export class StreamTextureSource {
  private readonly candidate: ProjectionCandidate;
  private readonly listeners = new Set<Listener>();
  private readonly playbackSessionId: string;
  private abortController: AbortController | null = null;
  private video: HTMLVideoElement | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private hls: Hls | null = null;
  private jsmpegPlayer: { destroy?: () => void } | null = null;
  private mseSocket: WebSocket | null = null;
  private mediaSourceUrl: string | null = null;
  private heartbeatTimer: number | null = null;
  private frameTimer: number | null = null;
  private texture: THREE.Texture | null = null;
  private selectedTransport: StreamingTransport | null = null;
  private selectedOutput: StreamingOutputUrl | null = null;
  private qualityProfileId: string | null = null;
  private destroyed = false;
  private snapshot: StreamTextureSnapshot = {
    status: "idle",
    message: "Aguardando transmissão.",
    transport: null,
    texture: null,
  };

  constructor(candidate: ProjectionCandidate) {
    this.candidate = candidate;
    const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    this.playbackSessionId = `spatial-video:${candidate.id}:${random}`;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  getSnapshot(): StreamTextureSnapshot {
    return this.snapshot;
  }

  start(): void {
    if (this.abortController || this.destroyed) return;
    this.abortController = new AbortController();
    this.setSnapshot({ status: "loading", message: "Preparando transmissão espacial.", transport: null, texture: null });
    void this.startAsync(this.abortController.signal);
  }

  destroy(): void {
    this.destroyed = true;
    this.stopHeartbeat();
    this.clearFrameTimer();
    this.abortController?.abort();
    this.abortController = null;
    this.hls?.destroy();
    this.hls = null;
    this.mseSocket?.close();
    this.mseSocket = null;
    this.jsmpegPlayer?.destroy?.();
    this.jsmpegPlayer = null;
    if (this.mediaSourceUrl) URL.revokeObjectURL(this.mediaSourceUrl);
    this.mediaSourceUrl = null;
    this.video?.pause();
    this.video?.removeAttribute("src");
    this.video?.load();
    this.video = null;
    this.canvas = null;
    this.texture?.dispose();
    this.texture = null;
    this.setSnapshot({ status: "idle", message: "Transmissão encerrada.", transport: null, texture: null });
  }

  private resetPlaybackArtifacts(): void {
    this.stopHeartbeat();
    this.clearFrameTimer();
    this.hls?.destroy();
    this.hls = null;
    this.mseSocket?.close();
    this.mseSocket = null;
    this.jsmpegPlayer?.destroy?.();
    this.jsmpegPlayer = null;
    if (this.mediaSourceUrl) URL.revokeObjectURL(this.mediaSourceUrl);
    this.mediaSourceUrl = null;
    this.video?.pause();
    this.video?.removeAttribute("src");
    this.video?.load();
    this.video = null;
    this.canvas = null;
    this.texture?.dispose();
    this.texture = null;
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  private setSnapshot(next: StreamTextureSnapshot): void {
    this.snapshot = next;
    this.emit();
  }

  private async startAsync(signal: AbortSignal): Promise<void> {
    try {
      const playback = await fetchLiveViewPlayback(this.candidate.liveViewId, this.candidate.variantId, signal);
      if (signal.aborted || this.destroyed) return;
      const options = chooseTransports(playback);
      if (options.length === 0) throw new Error("Nenhum transporte MSE/HLS/JSMpeg disponível para esta transmissão.");

      const errors: string[] = [];
      for (const selection of options) {
        if (signal.aborted || this.destroyed) return;
        this.resetPlaybackArtifacts();
        this.selectedTransport = selection.transport;
        this.selectedOutput = selection.output;
        this.qualityProfileId = selection.output.quality_profile_id ?? playback.variant?.quality_profile_id ?? null;
        this.setSnapshot({
          status: "loading",
          message: `Tentando ${selection.transport.toUpperCase()}.`,
          transport: selection.transport,
          texture: null,
        });

        try {
          await primeTransmissionDemand(playback.transmission.id, selection.output.output_id, this.qualityProfileId, signal);
          this.startHeartbeat(playback.transmission.id, selection.transport, selection.output.output_id, this.qualityProfileId);
          if (selection.transport === "mse") await this.startMse(selection.output.url, signal);
          else if (selection.transport === "hls") await this.startHls(selection.output.url, signal);
          else if (selection.transport === "jsmpeg") await this.startJsmpeg(selection.output.url, signal);
          return;
        } catch (error) {
          errors.push(`${selection.transport.toUpperCase()}: ${asMessage(error)}`);
        }
      }

      throw new Error(errors.join(" · "));
    } catch (error) {
      if (signal.aborted || this.destroyed) return;
      this.resetPlaybackArtifacts();
      this.setSnapshot({
        status: "error",
        message: asMessage(error),
        transport: this.selectedTransport,
        texture: null,
      });
    }
  }

  private startHeartbeat(transmissionId: string, transport: StreamingTransport, outputId: string | null, qualityProfileId: string | null): void {
    const run = () => {
      const controller = new AbortController();
      void heartbeatTransmissionDemand({
        transmissionId,
        playbackSessionId: this.playbackSessionId,
        transport,
        outputId,
        qualityProfileId,
        ttlSeconds: HEARTBEAT_TTL_SECONDS,
        signal: controller.signal,
      }).catch(() => undefined);
    };
    run();
    this.heartbeatTimer = window.setInterval(run, 10000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer != null) window.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  private clearFrameTimer(): void {
    if (this.frameTimer != null) window.clearTimeout(this.frameTimer);
    this.frameTimer = null;
  }

  private waitForVideoFrame(video: HTMLVideoElement, signal: AbortSignal, timeoutMs = FIRST_FRAME_TIMEOUT_MS): Promise<void> {
    return new Promise((resolve, reject) => {
      let done = false;
      const cleanup = () => {
        video.removeEventListener("loadeddata", onFrame);
        video.removeEventListener("timeupdate", onFrame);
        video.removeEventListener("error", onError);
        signal.removeEventListener("abort", onAbort);
        this.clearFrameTimer();
      };
      const finish = (callback: () => void) => {
        if (done) return;
        done = true;
        cleanup();
        callback();
      };
      const onFrame = () => {
        if (video.readyState >= 2) finish(resolve);
      };
      const onError = () => finish(() => reject(new Error("Erro no elemento de vídeo.")));
      const onAbort = () => finish(() => reject(new DOMException("Aborted", "AbortError")));
      video.addEventListener("loadeddata", onFrame);
      video.addEventListener("timeupdate", onFrame);
      video.addEventListener("error", onError);
      signal.addEventListener("abort", onAbort);
      this.frameTimer = window.setTimeout(() => finish(() => reject(new Error("Timeout aguardando primeiro frame."))), timeoutMs);
      onFrame();
    });
  }

  private createVideoTexture(video: HTMLVideoElement): THREE.VideoTexture {
    const texture = new THREE.VideoTexture(video);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.generateMipmaps = false;
    texture.flipY = false;
    this.texture?.dispose();
    this.texture = texture;
    return texture;
  }

  private async startHls(url: string, signal: AbortSignal): Promise<void> {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.crossOrigin = "anonymous";
    this.video = video;

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
    } else if (Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: false, backBufferLength: 10, maxBufferLength: 12 });
      this.hls = hls;
      hls.loadSource(url);
      hls.attachMedia(video);
    } else {
      throw new Error("Este navegador não suporta HLS nesta visualização.");
    }

    const texture = this.createVideoTexture(video);
    await video.play().catch(() => undefined);
    await this.waitForVideoFrame(video, signal);
    this.setSnapshot({ status: "playing", message: "Vídeo HLS projetado.", transport: "hls", texture });
  }

  private async startMse(url: string, signal: AbortSignal): Promise<void> {
    if (!("MediaSource" in window)) throw new Error("MSE não está disponível neste navegador.");
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    this.video = video;
    const texture = this.createVideoTexture(video);

    const mediaSource = new MediaSource();
    this.mediaSourceUrl = URL.createObjectURL(mediaSource);
    video.src = this.mediaSourceUrl;

    await new Promise<void>((resolve, reject) => {
      if (mediaSource.readyState === "open") {
        resolve();
        return;
      }
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onAbort = () => {
        cleanup();
        reject(new DOMException("Aborted", "AbortError"));
      };
      const cleanup = () => {
        mediaSource.removeEventListener("sourceopen", onOpen);
        signal.removeEventListener("abort", onAbort);
      };
      mediaSource.addEventListener("sourceopen", onOpen);
      signal.addEventListener("abort", onAbort);
    });

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      let sourceBuffer: SourceBuffer | null = null;
      const queue: ArrayBuffer[] = [];
      const fail = (error: Error) => {
        if (settled) return;
        settled = true;
        reject(error);
      };
      const maybeAppend = () => {
        if (!sourceBuffer || sourceBuffer.updating || queue.length === 0) return;
        const chunk = queue.shift();
        if (!chunk) return;
        try {
          sourceBuffer.appendBuffer(chunk);
        } catch (error) {
          fail(error instanceof Error ? error : new Error(String(error)));
        }
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
      const initTimer = window.setTimeout(() => fail(new Error("Timeout aguardando inicialização MSE.")), MSE_INIT_TIMEOUT_MS);
      const socket = new WebSocket(websocketUrl(url));
      socket.binaryType = "arraybuffer";
      this.mseSocket = socket;
      const cleanup = () => {
        window.clearTimeout(initTimer);
        signal.removeEventListener("abort", onAbort);
      };
      const onAbort = () => {
        socket.close();
        cleanup();
        fail(new DOMException("Aborted", "AbortError") as unknown as Error);
      };
      signal.addEventListener("abort", onAbort);
      socket.onopen = () => socket.send(JSON.stringify({ type: "mse", value: MSE_CODEC_REQUEST }));
      socket.onerror = () => fail(new Error("WebSocket MSE falhou."));
      socket.onclose = () => {
        if (!settled && !sourceBuffer) fail(new Error("WebSocket MSE fechou antes de iniciar."));
      };
      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          try {
            const parsed = JSON.parse(event.data) as { type?: string; value?: string };
            if (parsed.type === "error" && parsed.value) {
              fail(new Error(parsed.value));
              return;
            }
            const mime = parsed.type === "mse" && parsed.value ? createMediaSourceMime(parsed.value) : null;
            if (!mime || sourceBuffer) return;
            if (!MediaSource.isTypeSupported(mime)) {
              fail(new Error(`Mime MSE não suportado: ${mime}`));
              return;
            }
            sourceBuffer = mediaSource.addSourceBuffer(mime);
            sourceBuffer.mode = "segments";
            sourceBuffer.addEventListener("updateend", maybeAppend);
            cleanup();
            finish();
          } catch (error) {
            fail(error instanceof Error ? error : new Error(String(error)));
          }
          return;
        }
        if (event.data instanceof ArrayBuffer) {
          queue.push(event.data);
          maybeAppend();
        }
      };
    });

    await video.play().catch(() => undefined);
    await this.waitForVideoFrame(video, signal, MSE_FIRST_FRAME_TIMEOUT_MS);
    this.setSnapshot({ status: "playing", message: "Vídeo MSE projetado.", transport: "mse", texture });
  }

  private async startJsmpeg(url: string, signal: AbortSignal): Promise<void> {
    const canvas = document.createElement("canvas");
    canvas.width = 854;
    canvas.height = 480;
    this.canvas = canvas;
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.generateMipmaps = false;
    texture.flipY = false;
    this.texture?.dispose();
    this.texture = texture;

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      let decoded = false;
      const timer = window.setTimeout(() => {
        if (!decoded && !settled) reject(new Error("Timeout aguardando primeiro frame JSMpeg."));
      }, FIRST_FRAME_TIMEOUT_MS);
      const finish = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve();
      };
      const onAbort = () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      };
      signal.addEventListener("abort", onAbort, { once: true });
      this.jsmpegPlayer = new JSMpeg.Player(websocketUrl(url), {
        canvas,
        autoplay: true,
        audio: false,
        videoBufferSize: 512 * 1024,
        onVideoDecode: () => {
          decoded = true;
          texture.needsUpdate = true;
          finish();
        },
        onError: (error) => {
          if (!settled) reject(error instanceof Error ? error : new Error(String(error)));
        },
      });
    });

    this.setSnapshot({ status: "playing", message: "Vídeo JSMpeg projetado.", transport: "jsmpeg", texture });
  }
}
