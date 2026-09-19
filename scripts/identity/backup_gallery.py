"""Back up or restore the local identity gallery without overwriting existing data.

A backup includes the encryption key and must be protected like the live gallery.
Restore always targets a fresh directory; switching the application to it remains
an explicit operational action. Never restore untrusted backup files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from toposync_ext_vision.identity.store import IdentityStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--data-dir", required=True, type=Path)
    backup.add_argument("--destination", required=True, type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--new-data-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        import hashlib

        existing = (
            args.data_dir
            / "identities"
            / hashlib.sha256(b"installation").hexdigest()[:32]
            / "gallery.sqlite3"
        )
        if not existing.is_file():
            parser.error("No existing identity gallery at this data directory")
        store = IdentityStore(args.data_dir / "identities", scope="installation")
        try:
            result = store.backup_to(args.destination)
            print(
                f"Verified backup created at {args.destination}; gallery revision {result['gallery_revision']}."
            )
        finally:
            store.close()
    else:
        if args.new_data_dir.exists():
            parser.error("Recovery requires a new data directory")
        store = IdentityStore.restore_backup(
            args.backup, args.new_data_dir / "identities", scope="installation"
        )
        try:
            print(
                f"Gallery restored at {args.new_data_dir}; revision {store.revision}. Application data was not switched."
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
