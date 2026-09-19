"""Stage and explicitly activate reconstructed local reference embeddings.

Uses installed catalog models only. This does not download weights, calibrate
recognition or change pipeline configuration. Original vectors remain intact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from toposync_ext_vision.identity.extraction import IdentityExtractor
from toposync_ext_vision.identity.rebuild import (
    discard_inactive_representations,
    rebuild_reference_batch,
    representation_status,
    set_representation_active,
)
from toposync_ext_vision.identity.store import IdentityStore
from toposync_ext_vision.registry import build_default_model_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument(
        "--model-data-dir",
        type=Path,
        help="Data directory containing vision-models/; defaults to gallery data directory",
    )
    parser.add_argument("--species", required=True, choices=["person", "cat", "dog"])
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--face-detector-model-id", default="opencv_yunet_2023mar")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    stage = commands.add_parser("stage")
    stage.add_argument("--expected-revision", required=True, type=int)
    stage.add_argument("--batch-size", default=16, type=int)
    stage.add_argument("--retry-failed", action="store_true")
    for name in ("activate", "deactivate", "discard"):
        command = commands.add_parser(name)
        command.add_argument("--expected-revision", required=True, type=int)
    args = parser.parse_args()
    location = (
        args.data_dir
        / "identities"
        / hashlib.sha256(b"installation").hexdigest()[:32]
        / "gallery.sqlite3"
    )
    if not location.is_file():
        parser.error("An existing gallery is required")
    os.environ["TOPOSYNC_DATA_DIR"] = str((args.model_data_dir or args.data_dir).resolve())
    registry = build_default_model_registry()
    manifest = registry.get_manifest(args.model_id)
    detector = (
        registry.get_manifest(args.face_detector_model_id) if args.species == "person" else None
    )
    if manifest is None:
        parser.error("Model is not in the local catalog")
    extractor = IdentityExtractor(manifest, face_detector=detector)
    store = IdentityStore(args.data_dir / "identities", scope="installation")
    try:
        arguments = {"species": args.species, "space": extractor.embedding_space}
        if args.command == "status":
            result = representation_status(store, **arguments)
        elif args.command == "stage":
            result = rebuild_reference_batch(
                store,
                extractor,
                species=args.species,
                expected_revision=args.expected_revision,
                batch_size=args.batch_size,
                retry_failed=args.retry_failed,
            )
        elif args.command == "discard":
            result = discard_inactive_representations(
                store, **arguments, expected_revision=args.expected_revision
            )
        else:
            result = set_representation_active(
                store,
                **arguments,
                active=args.command == "activate",
                expected_revision=args.expected_revision,
            )
        print(json.dumps({"embedding_space": extractor.embedding_space, **result}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
