#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from rtsp_sample_server import REPO_ROOT, add_common_arguments, serve_rtsp_sample


DEFAULT_IMAGE = REPO_ROOT / "docs-site" / "static" / "img" / "rtsp-placeholder-camera.jpg"
DEFAULT_DATA_DIR = REPO_ROOT / ".toposync-data" / "rtsp-sample-image"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a still image as an RTSP camera for Toposync onboarding and development.",
        epilog=(
            "Examples:\n"
            "  npm run dev:rtsp-image\n"
            "  npm run dev:rtsp-image -- --expose docker\n"
            "  npm run dev:rtsp-image -- /path/to/image.jpg --expose network --host 192.168.1.10\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        default=DEFAULT_IMAGE,
        help=f"Image file to publish. Defaults to {DEFAULT_IMAGE.relative_to(REPO_ROOT)}.",
    )
    add_common_arguments(parser, default_path="toposync-sample-image", default_data_dir=DEFAULT_DATA_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists() or not image_path.is_file():
        print(f"Image file not found: {image_path}")
        return 2

    input_args = ["-re", "-loop", "1", "-framerate", str(args.fps), "-i", str(image_path)]
    return serve_rtsp_sample(input_label=f"image {image_path}", input_args=input_args, args=args)


if __name__ == "__main__":
    raise SystemExit(main())
