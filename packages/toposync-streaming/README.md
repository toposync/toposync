# Toposync Streaming bundle

Optional Toposync application bundle that installs the default `toposync` product plus the first-party `streaming` extension.

Notes:

- MediaMTX is downloaded on demand by the extension when the engine starts.
- go2rtc is downloaded automatically on first MSE sidecar start, unless the runtime sets `TOPOSYNC_STREAMING_GO2RTC_PATH` to a bundled binary.
- FFmpeg is expected from `PATH` or `TOPOSYNC_STREAMING_FFMPEG_PATH`.
- A packaged FFmpeg binary is only used when a custom distribution explicitly ships one.
