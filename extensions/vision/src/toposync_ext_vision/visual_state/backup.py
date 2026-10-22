from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid
import zipfile

from pydantic import Field, TypeAdapter, ValidationError

from .schemas import Identifier, ReferenceDraft, StoredReference, VisualStateConfig, VisualStateModel
from .store import ReferenceConflict, ReferenceLimit, VisualReferenceStore


MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BACKUP_PIXELS = 64 * 1024 * 1024


def _configuration_digest(configuration: VisualStateConfig) -> str:
    return hashlib.sha256(json.dumps(configuration.model_dump(mode='json'), sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class BackupManifest(VisualStateModel):
    version: int = Field(strict=True, ge=1, le=1)
    configuration: VisualStateConfig
    configuration_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    references: tuple[StoredReference, ...] = Field(max_length=300)


def export_backup(store: VisualReferenceStore, draft_id: str, *, expected_revision: int) -> bytes:
    with store._lock:
        draft = store._draft(draft_id, expected_revision)
        references = [store.read_configured_reference(draft.configuration, category.class_id, identifier)[0]
                      for category in draft.configuration.classes for identifier in category.reference_ids]
        manifest = BackupManifest(version=1, configuration=draft.configuration,
            configuration_sha256=_configuration_digest(draft.configuration), references=tuple(references))
        metadata = manifest.model_dump_json().encode()
        if len(metadata) > MAX_MANIFEST_BYTES:
            raise ReferenceLimit('backup manifest limit exceeded')
        images = {reference.sha256: reference for reference in references}
        if sum(reference.width * reference.height for reference in images.values()) > MAX_BACKUP_PIXELS:
            raise ReferenceLimit('backup pixel limit exceeded')
        # Include archive headers so the exporter never produces an oversized import.
        estimated_bytes = len(metadata) + sum(item.size_bytes for item in images.values()) + 512 * (len(images) + 1)
        if estimated_bytes > MAX_BACKUP_BYTES:
            raise ReferenceLimit('backup byte limit exceeded')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('manifest.json', metadata)
            for digest, reference in images.items():
                archive.writestr(f'images/{digest}.png', store._read_image(reference))
        return output.getvalue()


def _stage_backup(store: VisualReferenceStore, data: bytes, staging: Path) -> BackupManifest:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 301 or len(set(names)) != len(names):
                raise ReferenceConflict('backup entries must be unique and bounded')
            for entry in entries:
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1
                        or kind not in (0, stat.S_IFREG) or entry.is_dir()):
                    raise ReferenceConflict('backup requires uncompressed regular files')
                limit = MAX_MANIFEST_BYTES if entry.filename == 'manifest.json' else store.limits.image_bytes
                if entry.file_size > limit or entry.compress_size != entry.file_size:
                    raise ReferenceLimit('backup entry byte limit exceeded')
            if 'manifest.json' not in names:
                raise ReferenceConflict('backup manifest is missing')
            manifest = BackupManifest.model_validate_json(archive.read('manifest.json'))
            configuration = manifest.configuration
            if _configuration_digest(configuration) != manifest.configuration_sha256:
                raise ReferenceConflict('backup configuration integrity failed')
            configured = {identifier: category.class_id for category in configuration.classes for identifier in category.reference_ids}
            if len({reference.reference_id for reference in manifest.references}) != len(manifest.references):
                raise ReferenceConflict('backup reference identifiers must be unique')
            if set(configured) != {reference.reference_id for reference in manifest.references}:
                raise ReferenceConflict('backup references do not match configuration')
            images = {}
            for reference in manifest.references:
                if (reference.class_id != configured[reference.reference_id] or reference.status != 'confirmed'
                        or reference.evidence.source_identity != configuration.source_identity
                        or reference.evidence.preparation_revision != configuration.preparation_revision
                        or (configuration.region_id is not None and not reference.evidence.prepared_region)):
                    raise ReferenceConflict('backup reference is incompatible with configuration')
                previous = images.setdefault(reference.sha256, reference)
                if (previous.size_bytes, previous.width, previous.height) != (reference.size_bytes, reference.width, reference.height):
                    raise ReferenceConflict('backup image metadata is inconsistent')
            if set(names) != {'manifest.json', *(f'images/{digest}.png' for digest in images)}:
                raise ReferenceConflict('backup contains missing or unexpected paths')
            if sum(reference.width * reference.height for reference in images.values()) > MAX_BACKUP_PIXELS:
                raise ReferenceLimit('backup pixel limit exceeded')
            actual_pixels = 0
            for digest, reference in images.items():
                image = archive.read(f'images/{digest}.png')
                if len(image) != reference.size_bytes or hashlib.sha256(image).hexdigest() != digest:
                    raise ReferenceConflict('backup image integrity failed')
                if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise ReferenceConflict('backup image must be PNG')
                _, width, height = store._prepare_image(image)
                actual_pixels += width * height
                if (width, height) != (reference.width, reference.height) or actual_pixels > MAX_BACKUP_PIXELS:
                    raise ReferenceConflict('backup image dimensions are inconsistent')
                (staging / f'{digest}.png').write_bytes(image)
            return manifest
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValidationError, KeyError, UnicodeError, EOFError) as error:
        raise ReferenceConflict('backup format or version is invalid') from error


def restore_backup(store: VisualReferenceStore, data: bytes, *, operation_id: str) -> ReferenceDraft:
    TypeAdapter(Identifier).validate_python(operation_id)
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BACKUP_BYTES:
        raise ReferenceLimit('backup byte limit exceeded')
    digest = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix='restore-', dir=store.directory) as directory:
        staging = Path(directory)
        manifest = _stage_backup(store, data, staging)
        values = manifest.configuration.model_dump(mode='json')
        values['home_assistant']['enabled'] = False
        values['reference_revision'] = uuid.uuid4().hex
        configuration = VisualStateConfig.model_validate(values)
        with store._transaction():
            operation = store._connection.execute('SELECT digest,draft_id FROM restore_operation WHERE operation_id=?', (operation_id,)).fetchone()
            if operation is not None:
                if operation['digest'] != digest:
                    raise ReferenceConflict('restore operation was reused with different content')
                return store._draft(operation['draft_id'])
            new_references = []
            for reference in manifest.references:
                row = store._connection.execute('SELECT metadata FROM reference WHERE id=?', (reference.reference_id,)).fetchone()
                if row is None:
                    new_references.append(reference)
                elif StoredReference.model_validate_json(row[0]) != reference:
                    raise ReferenceConflict('restore reference identity conflicts with existing data')
            occupied = store._connection.execute('SELECT count(*) FROM reference').fetchone()[0]
            if occupied + len(new_references) > store.limits.references:
                raise ReferenceLimit('stored reference limit reached')
            created_images = set()
            replaced_images = {}
            try:
                for reference in manifest.references:
                    path = store._image_path(reference.sha256)
                    if path.exists() and (path.stat().st_size != reference.size_bytes
                            or hashlib.sha256(path.read_bytes()).hexdigest() != reference.sha256):
                        previous = staging / f'previous-{reference.sha256}.png'
                        os.replace(path, previous)
                        replaced_images[path] = previous
                    if not path.exists():
                        created_images.add(path)
                    store._write_image(reference.sha256, (staging / f'{reference.sha256}.png').read_bytes())
                store._connection.executemany('INSERT INTO reference VALUES (?,?,?)', [
                    (reference.reference_id, reference.model_dump_json(), reference.created_at) for reference in new_references])
                draft = store._create_draft(configuration)
                store._connection.execute('INSERT INTO restore_operation VALUES (?,?,?)', (operation_id, digest, draft.draft_id))
                return draft
            except BaseException:
                # Remove only this import's files while its database write lock still excludes other writers.
                for path in created_images:
                    path.unlink(missing_ok=True)
                for path, previous in replaced_images.items():
                    os.replace(previous, path)
                raise
