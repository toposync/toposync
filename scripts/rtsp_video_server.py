#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from rtsp_sample_server import REPO_ROOT, add_common_arguments, serve_rtsp_sample


DEFAULT_DATA_DIR = REPO_ROOT / ".toposync-data" / "rtsp-sample-video"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a video file in a loop as an RTSP camera for Toposync onboarding and development.",
        epilog=(
            "Examples:\n"
            "  npm run dev:rtsp-video -- --video /path/to/video.mp4\n"
            "  npm run dev:rtsp-video -- --video /path/to/video.mp4 --expose docker\n"
            "  npm run dev:rtsp-video -- --video /path/to/video.mp4 --expose network --host 192.168.1.10\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--video",
        required=True,
        type=Path,
        help="Video file to publish in a loop.",
    )
    add_common_arguments(parser, default_path="toposync-sample-video", default_data_dir=DEFAULT_DATA_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists() or not video_path.is_file():
        print(f"Video file not found: {video_path}")
        return 2

    input_args = ["-re", "-stream_loop", "-1", "-i", str(video_path)]
    return serve_rtsp_sample(input_label=f"video {video_path}", input_args=input_args, args=args)


if __name__ == "__main__":
    raise SystemExit(main())
