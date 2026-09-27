"""Bounded PCM decoding for an already resolved media source."""

from threading import Lock


class AudioCapture:
    def __init__(self, location: str):
        self.location = location
        self._lock = Lock()
        self._container = None
        self._decoder = None
        self._resampler = None
        self._fifo = None
        self._finished = False
        self._offset = 0
        self._terminal_status = "stream_lost" if location.startswith(("rtsp://", "rtsps://", "http://", "https://")) else "ended"

    def read(self):
        """Return status, PCM16, and relative sample time; never a capture clock."""
        with self._lock:
            if self._finished:
                return self._terminal_status, None, self._offset / 16000
            try:
                import av
                if self._container is None:
                    options = {"rtsp_transport": "tcp"} if self.location.startswith("rtsp://") else {}
                    self._container = av.open(self.location, timeout=(5, 5), options=options)
                    streams = self._container.streams.audio
                    if not streams:
                        self._finished = True
                        return "no_audio", None, 0.0
                    stream = streams[0]
                    channels = stream.codec_context.channels
                    if channels not in (1, 2):
                        self._finished = True
                        return "unsupported_channels", None, 0.0
                    self._decoder = self._container.decode(stream)
                    self._resampler = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=16000)
                    self._fifo = av.AudioFifo()
                while self._fifo.samples < 16000:
                    frame = next(self._decoder, None)
                    for converted in self._resampler.resample(frame):
                        # FIFO describes decoded sample order, not synchronized PTS.
                        converted.pts = None
                        self._fifo.write(converted)
                    if frame is None:
                        self._finished = True
                        break
                frame = self._fifo.read(16000, partial=True)
                offset = self._offset / 16000
                if frame is None:
                    return self._terminal_status, None, offset
                self._offset += frame.samples
                channels = len(frame.layout.channels)
                samples = frame.to_ndarray().reshape(-1, channels).copy()
                return "available", samples[:, 0] if channels == 1 else samples, offset
            except ImportError:
                self._finished = True
                return "dependency_unavailable", None, self._offset / 16000
            except Exception:
                # Do not leak resolved URLs, which can contain credentials.
                self._finished = True
                return "stream_lost", None, self._offset / 16000

    def close(self):
        # Serialize with an outstanding timed network read during cancellation.
        with self._lock:
            self._finished = True
            if self._container is not None:
                self._container.close()
                self._container = None
            self._decoder = self._resampler = self._fifo = None
