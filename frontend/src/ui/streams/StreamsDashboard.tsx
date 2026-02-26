import React, { useEffect, useMemo, useRef, useState } from "react";
import type Hls from "hls.js";

import {
  getStreamingTransmissionUrls,
  listStreamingTransmissions,
  primeStreamingTransmissionDemand,
  type StreamingTransmission,
  type StreamingTransmissionUrlsResponse,
} from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";

type GridMode = "1x1" | "2x2";

type Props = {
  uiVisible: boolean;
  isActive: boolean;
};

type TilePlaybackStatus = "idle" | "loading" | "playing" | "error" | "unsupported";
type TilePlaybackTransport = "none" | "webrtc" | "hls";
type StreamProtocol = "hls" | "rtsp" | "webrtc";

type BasicAuthCredentials = {
  username: string;
  password: string;
};

type PlaybackOutputSelection = {
  outputId: string;
  url: string;
  auth: BasicAuthCredentials | null;
};

const GRID_MODE_STORAGE_KEY = "toposync.streams.grid_mode.v1";
const TRANSMISSIONS_REFRESH_MS = 15000;
const RETRY_BASE_MS = 900;
const RETRY_MAX_MS = 8000;
const WEBRTC_SIGNAL_TIMEOUT_MS = 5000;
const WEBRTC_CONNECT_TIMEOUT_MS = 5000;

function readGridMode(): GridMode {
  if (typeof window === "undefined") return "2x2";
  try {
    const saved = String(localStorage.getItem(GRID_MODE_STORAGE_KEY) || "").trim();
    return saved === "1x1" ? "1x1" : "2x2";
  } catch {
    return "2x2";
  }
}

function normalizeText(value: unknown, fallback: string): string {
  const normalized = typeof value === "string" ? value.trim() : "";
  return normalized || fallback;
}

function selectOutputByProtocol(
  transmission: StreamingTransmission,
  urls: StreamingTransmissionUrlsResponse | undefined,
  protocol: StreamProtocol,
): PlaybackOutputSelection | null {
  if (!urls || !Array.isArray(urls.outputs)) return null;
  for (const output of urls.outputs) {
    if (!output || output.protocol !== protocol) continue;
    const url = String(output.url || "").trim();
    if (!url) continue;
    const outputId = String(output.output_id || "").trim();
    return {
      outputId,
      url,
      auth: resolveOutputBasicAuth(transmission, outputId),
    };
  }
  return null;
}

function resolveOutputBasicAuth(transmission: StreamingTransmission, outputId: string): BasicAuthCredentials | null {
  const outputs = Array.isArray(transmission.outputs) ? transmission.outputs : [];
  const output = outputs.find((item) => String(item?.id || "").trim() === String(outputId || "").trim()) ?? null;
  if (!output) return null;
  const auth = output.authentication;
  if (!auth || auth.enabled !== true) return null;
  const username = String(auth.username || "").trim();
  const password = String(auth.password || "").trim();
  if (!username || !password) return null;
  return { username, password };
}

function buildBasicAuthHeader(auth: BasicAuthCredentials | null): string | null {
  if (!auth) return null;
  try {
    return `Basic ${btoa(`${auth.username}:${auth.password}`)}`;
  } catch {
    return null;
  }
}

function withBasicAuthInUrl(url: string, auth: BasicAuthCredentials | null): string {
  if (!auth) return url;
  try {
    const parsed = new URL(url, window.location.href);
    parsed.username = auth.username;
    parsed.password = auth.password;
    return parsed.toString();
  } catch {
    return url;
  }
}

function asErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || i18n.t("core.ui.streams.error_unknown", {}, "unknown error"));
}

function canPlayNativeHls(video: HTMLVideoElement): boolean {
  const check = video.canPlayType("application/vnd.apple.mpegurl");
  return check === "probably" || check === "maybe";
}

function canUseWebRtc(): boolean {
  return typeof RTCPeerConnection !== "undefined";
}

function waitForIceGatheringComplete(peerConnection: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (peerConnection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      peerConnection.removeEventListener("icegatheringstatechange", onStateChange);
      reject(new Error(i18n.t("core.ui.streams.errors.webrtc_ice_timeout", {}, "Timed out waiting for ICE gathering.")));
    }, Math.max(500, timeoutMs));
    const onStateChange = () => {
      if (peerConnection.iceGatheringState !== "complete") return;
      window.clearTimeout(timeoutId);
      peerConnection.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    };
    peerConnection.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function waitForPeerConnectionReady(peerConnection: RTCPeerConnection, timeoutMs: number): Promise<void> {
  const isConnected = () =>
    peerConnection.connectionState === "connected" ||
    peerConnection.iceConnectionState === "connected" ||
    peerConnection.iceConnectionState === "completed";

  const isFailed = () =>
    peerConnection.connectionState === "failed" ||
    peerConnection.connectionState === "disconnected" ||
    peerConnection.connectionState === "closed" ||
    peerConnection.iceConnectionState === "failed" ||
    peerConnection.iceConnectionState === "disconnected" ||
    peerConnection.iceConnectionState === "closed";

  if (isConnected()) return Promise.resolve();
  if (isFailed()) return Promise.reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_failed", {}, "WebRTC connection failed.")));

  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      cleanup();
      reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_timeout", {}, "WebRTC connection timed out.")));
    }, Math.max(1000, timeoutMs));

    const onStateChange = () => {
      if (isConnected()) {
        cleanup();
        resolve();
        return;
      }
      if (isFailed()) {
        cleanup();
        reject(new Error(i18n.t("core.ui.streams.errors.webrtc_connection_failed", {}, "WebRTC connection failed.")));
      }
    };

    const cleanup = () => {
      window.clearTimeout(timeoutId);
      peerConnection.removeEventListener("connectionstatechange", onStateChange);
      peerConnection.removeEventListener("iceconnectionstatechange", onStateChange);
    };

    peerConnection.addEventListener("connectionstatechange", onStateChange);
    peerConnection.addEventListener("iceconnectionstatechange", onStateChange);
  });
}

function StreamTilePlayer({
  transmissionId,
  label,
  overlayVisible,
  sourceHint,
  sourceHintTone,
  webrtcUrl,
  webrtcAuthHeader,
  hlsUrl,
  hlsAuthHeader,
  hlsNativeUrl,
  active,
}: {
  transmissionId: string;
  label: string;
  overlayVisible: boolean;
  sourceHint: string | null;
  sourceHintTone: "muted" | "warn" | "error";
  webrtcUrl: string | null;
  webrtcAuthHeader: string | null;
  hlsUrl: string | null;
  hlsAuthHeader: string | null;
  hlsNativeUrl: string | null;
  active: boolean;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);

  const [status, setStatus] = useState<TilePlaybackStatus>("idle");
  const [transport, setTransport] = useState<TilePlaybackTransport>("none");
  const [errorText, setErrorText] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    let cancelled = false;
    let nativeCleanup: (() => void) | null = null;
    let retryTimerId: number | null = null;
    let attempt = 0;
    let hlsPlayer: Hls | null = null;
    let peerConnection: RTCPeerConnection | null = null;
    let whepSessionUrl: string | null = null;
    let webrtcAbortController: AbortController | null = null;

    const clearRetryTimer = () => {
      if (retryTimerId == null) return;
      window.clearTimeout(retryTimerId);
      retryTimerId = null;
    };

    const clearNative = () => {
      if (!nativeCleanup) return;
      nativeCleanup();
      nativeCleanup = null;
    };

    const destroyHls = () => {
      try {
        hlsPlayer?.destroy();
      } catch {
        // ignore
      }
      hlsPlayer = null;
    };

    const destroyWebRtc = () => {
      const abortController = webrtcAbortController;
      webrtcAbortController = null;
      if (abortController) abortController.abort();

      const sessionUrl = whepSessionUrl;
      whepSessionUrl = null;
      if (sessionUrl) {
        void fetch(sessionUrl, {
          method: "DELETE",
          mode: "cors",
          headers: webrtcAuthHeader ? { authorization: webrtcAuthHeader } : undefined,
        }).catch(() => {});
      }

      const currentPeerConnection = peerConnection;
      peerConnection = null;
      if (currentPeerConnection) {
        try {
          currentPeerConnection.ontrack = null;
          currentPeerConnection.close();
        } catch {
          // ignore
        }
      }
    };

    const clearVideoSource = () => {
      const video = videoRef.current;
      if (!video) return;
      try {
        video.pause();
      } catch {
        // ignore
      }
      try {
        video.srcObject = null;
      } catch {
        // ignore
      }
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        // ignore
      }
    };

    const destroyPlayback = () => {
      clearRetryTimer();
      clearNative();
      destroyHls();
      destroyWebRtc();
      clearVideoSource();
    };

    const scheduleRetry = (reason: string) => {
      if (cancelled || !active || (!hlsUrl && !webrtcUrl) || retryTimerId != null) return;
      const delayMs = Math.min(RETRY_BASE_MS * Math.max(1, 2 ** attempt), RETRY_MAX_MS);
      retryTimerId = window.setTimeout(() => {
        retryTimerId = null;
        attempt += 1;
        setStatus("loading");
        setErrorText(reason);
        void startPlayback();
      }, delayMs);
    };

    const configureVideo = (video: HTMLVideoElement) => {
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      video.controls = false;
    };

    const startNativeHlsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      setTransport("hls");
      const sourceUrl = String(hlsNativeUrl || hlsUrl || "").trim();
      const onPlaying = () => {
        setStatus("playing");
        setErrorText(null);
      };
      const onError = () => {
        setStatus("error");
        const message = i18n.t("core.ui.streams.errors.hls_native_error", {}, "Native HLS playback error.");
        setErrorText(message);
        destroyPlayback();
        scheduleRetry(message);
      };

      video.addEventListener("playing", onPlaying);
      video.addEventListener("loadeddata", onPlaying);
      video.addEventListener("error", onError);
      nativeCleanup = () => {
        video.removeEventListener("playing", onPlaying);
        video.removeEventListener("loadeddata", onPlaying);
        video.removeEventListener("error", onError);
      };

      video.src = sourceUrl;
      try {
        await video.play();
      } catch {
        // autoplay can be blocked; user interaction will retry.
      }
    };

    const startHlsJsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      setTransport("hls");
      const hlsModule = await import("hls.js");
      if (cancelled) return;
      const HlsConstructor = hlsModule.default;
      if (!HlsConstructor.isSupported()) {
        setStatus("unsupported");
        setErrorText(i18n.t("core.ui.streams.errors.hls_unsupported_browser", {}, "HLS playback is not supported in this browser."));
        return;
      }

      const hls = new HlsConstructor({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        xhrSetup: (xhr: XMLHttpRequest) => {
          if (hlsAuthHeader) xhr.setRequestHeader("Authorization", hlsAuthHeader);
        },
        fetchSetup: (context: { url: string }, initParams: RequestInit) => {
          const requestUrl = String(context?.url || "");
          if (!hlsAuthHeader) return new Request(requestUrl, initParams);
          const headers = new Headers(initParams?.headers || {});
          headers.set("Authorization", hlsAuthHeader);
          return new Request(requestUrl, { ...initParams, headers });
        },
      });
      hlsPlayer = hls;

      hls.on(HlsConstructor.Events.MANIFEST_PARSED, () => {
        setStatus("playing");
        setErrorText(null);
        void video.play().catch(() => {});
      });

      hls.on(HlsConstructor.Events.ERROR, (_event, data) => {
        if (!data?.fatal) return;
        setStatus("error");
        const details = String(
          data.details || data.type || i18n.t("core.ui.streams.errors.hlsjs_fatal", {}, "hls.js fatal error"),
        );
        setErrorText(details);
        destroyPlayback();
        scheduleRetry(details);
      });

      hls.attachMedia(video);
      hls.on(HlsConstructor.Events.MEDIA_ATTACHED, () => {
        hls.loadSource(hlsUrl ?? "");
      });
    };

    const normalizeWhepSdp = (sdp: string): string => {
      // MediaMTX expects SDP with CRLF terminators; using `trim()` can remove the trailing CRLF and break parsing (EOF).
      const normalized = sdp.replace(/\r?\n/g, "\r\n");
      return normalized.endsWith("\r\n") ? normalized : `${normalized}\r\n`;
    };

    const startWebRtcPlayback = async (video: HTMLVideoElement): Promise<void> => {
      if (!webrtcUrl) throw new Error(i18n.t("core.ui.streams.errors.webrtc_url_missing", {}, "WebRTC URL is not available."));
      if (!canUseWebRtc()) throw new Error(i18n.t("core.ui.streams.errors.webrtc_unsupported_browser", {}, "WebRTC is not supported in this browser."));

      setTransport("webrtc");
      const nextPeerConnection = new RTCPeerConnection();
      peerConnection = nextPeerConnection;

      const remoteStream = new MediaStream();
      video.srcObject = remoteStream;

      nextPeerConnection.ontrack = (event) => {
        const sourceStream = event.streams[0] ?? null;
        if (sourceStream) {
          for (const track of sourceStream.getTracks()) {
            const exists = remoteStream.getTracks().some((item) => item.id === track.id);
            if (!exists) remoteStream.addTrack(track);
          }
          return;
        }
        remoteStream.addTrack(event.track);
      };

      nextPeerConnection.addTransceiver("video", { direction: "recvonly" });
      nextPeerConnection.addTransceiver("audio", { direction: "recvonly" });

      try {
        const offer = await nextPeerConnection.createOffer();
        await nextPeerConnection.setLocalDescription(offer);
        await waitForIceGatheringComplete(nextPeerConnection, WEBRTC_SIGNAL_TIMEOUT_MS);

        const localDescription = nextPeerConnection.localDescription;
        const offerSdpRaw = String(localDescription?.sdp || "");
        if (!offerSdpRaw.trim()) {
          throw new Error(i18n.t("core.ui.streams.errors.webrtc_sdp_offer_failed", {}, "Failed to create WebRTC SDP offer."));
        }
        const offerSdp = normalizeWhepSdp(offerSdpRaw);

        const abortController = new AbortController();
        webrtcAbortController = abortController;
        const response = await fetch(webrtcUrl, {
          method: "POST",
          mode: "cors",
          headers: {
            accept: "application/sdp",
            "content-type": "application/sdp",
            ...(webrtcAuthHeader ? { authorization: webrtcAuthHeader } : {}),
          },
          body: offerSdp,
          signal: abortController.signal,
        });
        if (!response.ok) {
          let detail = "";
          try {
            detail = String(await response.text()).trim();
          } catch {
            detail = "";
          }
          const base = i18n.t(
            "core.ui.streams.errors.whep_negotiation_failed",
            { status: response.status },
            "WHEP negotiation failed ({{status}}).",
          );
          const snippet = detail ? detail.slice(0, 280) : "";
          const responseHint = snippet
            ? i18n.t("core.ui.streams.errors.http_response_snippet", { detail: snippet }, "Response: {{detail}}")
            : "";
          throw new Error(responseHint ? `${base} ${responseHint}` : base);
        }

        const answerSdpRaw = String(await response.text());
        if (!answerSdpRaw.trim()) {
          throw new Error(i18n.t("core.ui.streams.errors.whep_answer_empty", {}, "WHEP answer is empty."));
        }
        const answerSdp = normalizeWhepSdp(answerSdpRaw);

        const locationHeader = response.headers.get("location");
        if (locationHeader) {
          try {
            whepSessionUrl = new URL(locationHeader, webrtcUrl).toString();
          } catch {
            whepSessionUrl = locationHeader;
          }
        }

        await nextPeerConnection.setRemoteDescription({ type: "answer", sdp: answerSdp });
        await waitForPeerConnectionReady(nextPeerConnection, WEBRTC_CONNECT_TIMEOUT_MS);
      } catch (error) {
        destroyWebRtc();
        throw error;
      }

      setStatus("playing");
      setErrorText(null);
      try {
        await video.play();
      } catch {
        // autoplay can be blocked; user interaction will retry.
      }
    };

    const startHlsPlayback = async (video: HTMLVideoElement): Promise<void> => {
      if (!hlsUrl) throw new Error(i18n.t("core.ui.streams.errors.hls_url_missing", {}, "HLS URL is not available."));
      if (canPlayNativeHls(video)) {
        await startNativeHlsPlayback(video);
        return;
      }
      await startHlsJsPlayback(video);
    };

    const startPlayback = async () => {
      if (cancelled || !active || (!hlsUrl && !webrtcUrl)) return;
      const video = videoRef.current;
      if (!video) return;

      destroyPlayback();
      configureVideo(video);
      setStatus("loading");
      setErrorText(null);
      setTransport("none");

      if (transmissionId) {
        try {
          await primeStreamingTransmissionDemand(transmissionId);
        } catch (primeError) {
          // Priming is best-effort; if it fails, fallback/retry will handle it.
          setErrorText(
            i18n.t(
              "core.ui.streams.errors.prime_failed",
              { error: asErrorMessage(primeError) },
              "Failed to prime stream: {{error}}",
            ),
          );
        }
      }
      if (cancelled) return;

      let webRtcError: string | null = null;
      if (webrtcUrl) {
        const maxAttempts = 3;
        for (let attemptIndex = 0; attemptIndex < maxAttempts; attemptIndex += 1) {
          try {
            await startWebRtcPlayback(video);
            return;
          } catch (error) {
            const message = asErrorMessage(error);
            webRtcError = message;

            const shouldRetry =
              attemptIndex < maxAttempts - 1 &&
              (message.includes("(404)") || message.includes("no stream is available"));
            if (!shouldRetry) break;

            await new Promise((resolve) => window.setTimeout(resolve, 250));
            if (cancelled) return;
          }
        }
      }

      if (hlsUrl) {
        try {
          await startHlsPlayback(video);
          return;
        } catch (error) {
          const hlsError = asErrorMessage(error);
          const combinedError = webRtcError
            ? i18n.t(
                "core.ui.streams.errors.webrtc_hls_fallback_failed",
                { webrtcError: webRtcError, hlsError },
                "WebRTC failed: {{webrtcError}}. HLS fallback failed: {{hlsError}}",
              )
            : hlsError;
          setStatus("error");
          setErrorText(combinedError);
          destroyPlayback();
          scheduleRetry(combinedError);
          return;
        }
      }

      const message =
        webRtcError || i18n.t("core.ui.streams.errors.no_supported_playback", {}, "No supported playback output available.");
      setStatus("error");
      setErrorText(message);
      destroyPlayback();
      scheduleRetry(message);
    };

    if (!active || (!hlsUrl && !webrtcUrl)) {
      attempt = 0;
      destroyPlayback();
      setStatus("idle");
      setTransport("none");
      setErrorText(null);
      return () => {
        cancelled = true;
        destroyPlayback();
      };
    }

    attempt = 0;
    void startPlayback();

    return () => {
      cancelled = true;
      destroyPlayback();
    };
  }, [active, hlsAuthHeader, hlsNativeUrl, hlsUrl, transmissionId, webrtcAuthHeader, webrtcUrl]);

  const playbackStatusLabel = useMemo(() => {
    const onlineLabel = t("core.ui.streams.status.online", {}, "online");
    const waitingLabel = t("core.ui.streams.status.waiting", {}, "waiting");
    const statusLabel =
      status === "idle"
        ? waitingLabel
        : status === "loading"
          ? t("core.ui.streams.status.loading", {}, "loading")
          : status === "error"
            ? t("core.ui.streams.status.error", {}, "error")
            : status === "unsupported"
              ? t("core.ui.streams.status.unsupported", {}, "unsupported")
              : waitingLabel;

    const transportLabel =
      transport === "webrtc"
        ? t("core.ui.streams.transport.webrtc", {}, "WebRTC")
        : transport === "hls"
          ? t("core.ui.streams.transport.hls", {}, "HLS")
          : onlineLabel;

    if (status === "playing") {
      return transport === "none" ? onlineLabel : transportLabel;
    }
    if (transport === "none") return statusLabel;
    return t(
      "core.ui.streams.status.with_transport",
      { status: statusLabel, transport: transportLabel },
      `${statusLabel} (${transportLabel})`,
    );
  }, [status, t, transport]);

  const toggleFullscreen = async () => {
    const el = frameRef.current;
    if (!el) return;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
      }
      await el.requestFullscreen();
    } catch {
      // ignore
    }
  };

  const togglePip = async () => {
    const video = videoRef.current;
    if (!video) return;

    try {
      const anyDoc = document as unknown as { pictureInPictureElement?: Element | null; pictureInPictureEnabled?: boolean; exitPictureInPicture?: () => Promise<void> };
      if (anyDoc.pictureInPictureElement && typeof anyDoc.exitPictureInPicture === "function") {
        await anyDoc.exitPictureInPicture();
        return;
      }

      const anyVideo = video as unknown as { requestPictureInPicture?: () => Promise<void>; webkitSetPresentationMode?: (mode: string) => void; webkitPresentationMode?: string };
      if (anyDoc.pictureInPictureEnabled && typeof anyVideo.requestPictureInPicture === "function") {
        await anyVideo.requestPictureInPicture();
        return;
      }
      if (typeof anyVideo.webkitSetPresentationMode === "function") {
        const current = String(anyVideo.webkitPresentationMode || "inline");
        anyVideo.webkitSetPresentationMode(current === "picture-in-picture" ? "inline" : "picture-in-picture");
      }
    } catch {
      // ignore
    }
  };

  const pipSupported =
    typeof document !== "undefined" &&
    (Boolean((document as any).pictureInPictureEnabled && (HTMLVideoElement.prototype as any).requestPictureInPicture) ||
      Boolean((HTMLVideoElement.prototype as any).webkitSetPresentationMode));

  const fullscreenSupported = typeof document !== "undefined" && Boolean((document as any).fullscreenEnabled && (HTMLElement.prototype as any).requestFullscreen);

  return (
    <div className="streamsPlayerFrame" ref={frameRef}>
      <video ref={videoRef} className="streamsVideo" muted playsInline autoPlay />

	      <div className={["streamsTileOverlay", overlayVisible ? "isVisible" : "isHidden"].join(" ")}>
        <div className="streamsTileOverlayLeft" title={label}>
          <span className={["streamsPlaybackDot", `is-${status}`].join(" ")} />
          <span className="streamsTileOverlayTitle">{label}</span>
          <span className="streamsTileOverlayMeta">{playbackStatusLabel}</span>
        </div>

	        <div className="streamsTileOverlayActions">
	          <button
	            type="button"
	            className="iconButton streamsTileOverlayButton"
	            aria-label={t("core.ui.streams.actions.pip", {}, "Picture-in-picture")}
	            title={t("core.ui.streams.actions.pip", {}, "Picture-in-picture")}
	            onClick={togglePip}
	            disabled={!pipSupported}
	          >
	            <Icon name="window-restore" />
	          </button>
	          <button
	            type="button"
	            className="iconButton streamsTileOverlayButton"
	            aria-label={t("core.ui.streams.actions.fullscreen", {}, "Fullscreen")}
	            title={t("core.ui.streams.actions.fullscreen", {}, "Fullscreen")}
	            onClick={toggleFullscreen}
	            disabled={!fullscreenSupported}
	          >
	            <Icon name="up-right-and-down-left-from-center" />
	          </button>
        </div>
      </div>

      {overlayVisible && (sourceHint || errorText) ? (
        <div className={["streamsTileOverlayHint", `is-${errorText ? "error" : sourceHintTone}`].join(" ")}>
          {errorText || sourceHint}
        </div>
      ) : null}
    </div>
  );
}

export function StreamsDashboard({ uiVisible, isActive }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [gridMode, setGridMode] = useState<GridMode>(() => readGridMode());
  const [pageIndex, setPageIndex] = useState(0);

  const [transmissions, setTransmissions] = useState<StreamingTransmission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [urlsByTransmissionId, setUrlsByTransmissionId] = useState<Record<string, StreamingTransmissionUrlsResponse>>({});
  const [urlsLoadingByTransmissionId, setUrlsLoadingByTransmissionId] = useState<Record<string, boolean>>({});
  const [urlErrorByTransmissionId, setUrlErrorByTransmissionId] = useState<Record<string, string>>({});

  const [tabVisible, setTabVisible] = useState<boolean>(() => {
    if (typeof document === "undefined") return true;
    return document.visibilityState === "visible";
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      localStorage.setItem(GRID_MODE_STORAGE_KEY, gridMode);
    } catch {
      // ignore
    }
  }, [gridMode]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibilityChange = () => setTabVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadTransmissions = async (isFirstLoad: boolean) => {
      if (isFirstLoad) setLoading(true);
      try {
        const payload = await listStreamingTransmissions();
        if (cancelled) return;
        setTransmissions(Array.isArray(payload) ? payload : []);
        setError(null);
      } catch (loadError) {
        if (cancelled) return;
        setError(asErrorMessage(loadError));
      } finally {
        if (!cancelled && isFirstLoad) setLoading(false);
      }
    };

    void loadTransmissions(true);
    const intervalId = window.setInterval(() => {
      void loadTransmissions(false);
    }, TRANSMISSIONS_REFRESH_MS);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  const enabledTransmissions = useMemo(
    () => transmissions.filter((item) => item && item.enabled !== false),
    [transmissions],
  );

  const pageSize = gridMode === "1x1" ? 1 : 4;
  const pageCount = Math.max(1, Math.ceil(enabledTransmissions.length / pageSize));

  useEffect(() => {
    setPageIndex((previous) => Math.min(previous, Math.max(0, pageCount - 1)));
  }, [pageCount]);

  const currentPageItems = useMemo(() => {
    const start = pageIndex * pageSize;
    return enabledTransmissions.slice(start, start + pageSize);
  }, [enabledTransmissions, pageIndex, pageSize]);

  useEffect(() => {
    for (const transmission of currentPageItems) {
      const transmissionId = String(transmission.id || "").trim();
      if (!transmissionId) continue;
      if (urlsByTransmissionId[transmissionId]) continue;
      if (urlsLoadingByTransmissionId[transmissionId]) continue;

      setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: true }));
      void getStreamingTransmissionUrls(transmissionId)
        .then((payload) => {
          setUrlsByTransmissionId((previous) => ({ ...previous, [transmissionId]: payload }));
          setUrlErrorByTransmissionId((previous) => {
            if (!previous[transmissionId]) return previous;
            const next = { ...previous };
            delete next[transmissionId];
            return next;
          });
        })
        .catch((loadError) => {
          setUrlErrorByTransmissionId((previous) => ({
            ...previous,
            [transmissionId]: asErrorMessage(loadError),
          }));
        })
        .finally(() => {
          setUrlsLoadingByTransmissionId((previous) => ({ ...previous, [transmissionId]: false }));
        });
    }
  }, [currentPageItems, urlsByTransmissionId, urlsLoadingByTransmissionId]);

  const pageTiles = useMemo(() => {
    const out: Array<StreamingTransmission | null> = [...currentPageItems];
    while (out.length < pageSize) out.push(null);
    return out;
  }, [currentPageItems, pageSize]);

  const canGoPrev = pageIndex > 0;
  const canGoNext = pageIndex < pageCount - 1;
  const playersActive = isActive && tabVisible;

  if (loading) {
    return (
      <div className="viewportRoot streamsRoot">
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">{t("core.ui.loading", {}, "Loading...")}</div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="viewportRoot streamsRoot">
      {error ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.unavailable", {}, "Streaming extension unavailable.")} {error}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledTransmissions.length === 0 ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">
              {t("core.ui.streams.empty", {}, "No enabled transmissions. Create one in Settings > Transmissions.")}
            </div>
          </div>
        </div>
      ) : null}

      {!error && enabledTransmissions.length > 0 ? (
        <div className={["streamsGrid", gridMode === "1x1" ? "is1x1" : "is2x2"].join(" ")}>
          {pageTiles.map((transmission, slotIndex) => {
            if (!transmission) {
              return <div key={`slot-empty-${slotIndex}`} className="streamsTile streamsTileEmpty" />;
            }

            const transmissionId = String(transmission.id || "").trim();
            const transmissionName = normalizeText(
              transmission.name,
              normalizeText(transmission.path, transmissionId || `stream-${slotIndex + 1}`),
            );
            const urls = urlsByTransmissionId[transmissionId];
            const webrtcOutput = selectOutputByProtocol(transmission, urls, "webrtc");
            const hlsOutput = selectOutputByProtocol(transmission, urls, "hls");
            const urlError = urlErrorByTransmissionId[transmissionId];
            const urlLoading = Boolean(urlsLoadingByTransmissionId[transmissionId]);
            const webrtcUrl = webrtcOutput?.url ?? null;
            const hlsUrl = hlsOutput?.url ?? null;
            const webrtcAuthHeader = buildBasicAuthHeader(webrtcOutput?.auth ?? null);
            const hlsAuthHeader = buildBasicAuthHeader(hlsOutput?.auth ?? null);
            const hlsNativeUrl = hlsUrl ? withBasicAuthInUrl(hlsUrl, hlsOutput?.auth ?? null) : null;
            const tileActive = playersActive && Boolean(webrtcUrl || hlsUrl);

            let sourceHint: string | null = null;
            let sourceHintTone: "muted" | "warn" | "error" = "muted";
            if (urlLoading) {
              sourceHint = t("core.ui.streams.hint.loading_url", {}, "Loading stream URL…");
              sourceHintTone = "muted";
            } else if (urlError) {
              sourceHint = urlError;
              sourceHintTone = "error";
            } else if (!webrtcUrl && !hlsUrl) {
              sourceHint = t("core.ui.streams.hint.no_outputs", {}, "No WebRTC/HLS output configured for this transmission.");
              sourceHintTone = "warn";
            }

            return (
              <div key={transmissionId} className="streamsTile">
                <StreamTilePlayer
                  transmissionId={transmissionId}
                  label={transmissionName}
                  overlayVisible={uiVisible}
                  sourceHint={sourceHint}
                  sourceHintTone={sourceHintTone}
                  webrtcUrl={webrtcUrl}
                  webrtcAuthHeader={webrtcAuthHeader}
                  hlsUrl={hlsUrl}
                  hlsAuthHeader={hlsAuthHeader}
                  hlsNativeUrl={hlsNativeUrl}
                  active={tileActive}
                />
              </div>
            );
          })}
        </div>
      ) : null}

      <div className={["streamsHud", uiVisible ? "isVisible" : "isHidden"].join(" ")}>
        <div className="streamsHudGroup">
          <button
            type="button"
            className={["chipButton", "streamsHudModeButton", gridMode === "1x1" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-pressed={gridMode === "1x1"}
            onClick={() => {
              setGridMode("1x1");
              setPageIndex(0);
            }}
          >
            1x1
          </button>
          <button
            type="button"
            className={["chipButton", "streamsHudModeButton", gridMode === "2x2" ? "isActive" : ""].filter(Boolean).join(" ")}
            aria-pressed={gridMode === "2x2"}
            onClick={() => {
              setGridMode("2x2");
              setPageIndex(0);
            }}
          >
            2x2
          </button>
        </div>

        <div className="streamsHudGroup streamsHudPager">
          <button
            type="button"
            className="iconButton streamsHudNavButton"
            onClick={() => setPageIndex((previous) => Math.max(0, previous - 1))}
            disabled={!canGoPrev}
            aria-label={t("core.ui.streams.prev_page", {}, "Previous page")}
          >
            <Icon name="chevron-left" />
          </button>
          <div className="streamsHudPageLabel">
            {t("core.ui.streams.page", { current: pageIndex + 1, total: pageCount }, `${pageIndex + 1}/${pageCount}`)}
          </div>
          <button
            type="button"
            className="iconButton streamsHudNavButton"
            onClick={() => setPageIndex((previous) => Math.min(pageCount - 1, previous + 1))}
            disabled={!canGoNext}
            aria-label={t("core.ui.streams.next_page", {}, "Next page")}
          >
            <Icon name="chevron-right" />
          </button>
        </div>
      </div>
    </div>
  );
}
