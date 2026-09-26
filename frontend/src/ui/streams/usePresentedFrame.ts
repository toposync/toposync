import { useEffect, useRef } from 'react';
import type { LiveViewFrame } from '@toposync/plugin-api';

/** Create a decoder epoch even in browsers that do not implement Crypto.randomUUID. */
export function createPresentedFrameEpoch(): string {
    const browserCrypto = globalThis.crypto;
    if (typeof browserCrypto?.randomUUID === 'function')
        return browserCrypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Observe the existing decoder; this hook never opens a transport. */
export function usePresentedFrame(video: React.RefObject<HTMLVideoElement>, enabled: boolean, identity: string, onFrame?: (frame: LiveViewFrame | null) => void) {
    const callback = useRef(onFrame);
    callback.current = onFrame;
    useEffect(() => {
        const element = video.current;
        if (!element || !enabled || !onFrame) {
            callback.current?.(null);
            return;
        }
        let disposed = false, handle = 0, sequence = 0, lastTime = -1;
        const epoch = createPresentedFrameEpoch();
        const frameCallback = 'requestVideoFrameCallback' in element;
        function deliver(_now: number, metadata?: VideoFrameCallbackMetadata) {
            if (disposed || !element)
                return;
            const mediaTime = metadata?.mediaTime ?? element.currentTime;
            if (element.readyState >= 2 && !element.paused && mediaTime !== lastTime) {
                lastTime = mediaTime;
                callback.current?.({ image: element, width: element.videoWidth, height: element.videoHeight,
                    epoch, sequence: ++sequence, mediaTime, presentedAt: performance.now(), timingBasis: 'browser_presented_frame' });
            }
            handle = frameCallback ? element.requestVideoFrameCallback(deliver) : requestAnimationFrame(deliver);
        }
        // `waiting` and `stalled` only mean that playback is temporarily short
        // of data. The element still owns its last presented frame and keeps the
        // same decoder identity, so consumers use their own freshness budget.
        // Null is reserved for terminal media loss, suspension, or replacement.
        const invalid = () => callback.current?.(null);
        for (const event of ['emptied', 'error', 'ended'])
            element.addEventListener(event, invalid);
        handle = frameCallback ? element.requestVideoFrameCallback(deliver) : requestAnimationFrame(deliver);
        return () => {
            disposed = true;
            if (frameCallback)
                element.cancelVideoFrameCallback(handle);
            else
                cancelAnimationFrame(handle);
            for (const event of ['emptied', 'error', 'ended'])
                element.removeEventListener(event, invalid);
            invalid();
        };
    }, [video, enabled, identity, Boolean(onFrame)]);
}
