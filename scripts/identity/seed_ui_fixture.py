"""Disposable synthetic UI fixtures, NOT a recognition-quality evaluation.

Uses production persistence services. Never run against user or production data.
"""
from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import time

from PIL import Image, ImageDraw
from toposync.runtime.notifications.store import NotificationStore
from toposync_ext_vision.identity.contracts import IdentityEvidence
from toposync_ext_vision.identity.store import IdentityStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--disposable-test-data", required=True, action="store_true")
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    store = IdentityStore(directory / "identities", scope="installation")
    if store.observations() or store.list_identities():
        raise SystemExit("Refusing to seed a gallery that already contains observations or identities")
    notifications = NotificationStore(directory / "notifications/notifications.sqlite3")
    for index, species in enumerate(("person", "cat", "cat", "dog")):
        occurrence = f"identity-ui-fixture-{index}"
        for frame in range(2):
            sample = IdentityEvidence(species=species, occurrence_id=occurrence, source_id="test:synthetic", camera_id="identity-test-camera", capture_id=f"{index}:{frame}", observed_at=time.time()+frame, embedding_space="test:synthetic-ui-only:v1", vector=tuple([1.0]+[0.0]*15), quality=0.9, reference_eligible=True, region=(0,0,1,1))
            image = Image.new("RGB", (320,240), (26+index*35, 66, 87+frame*35))
            draw = ImageDraw.Draw(image)
            draw.rectangle((25,25,295,215), outline="white", width=3)
            draw.text((45,105), f"SYNTHETIC UI TEST: {species} {index}/{frame}", fill="white")
            blob = BytesIO()
            image.save(blob, format="JPEG")
            decision = store.observe(sample, None, crop=blob.getvalue())
        notifications.upsert(type="pipelines.event", title=f"Ensaio de curadoria · {species} {index}", description="Evidência sintética para validar a interface. Não mede acurácia.", dedupe_key=occurrence, payload={"source":"pipelines","pipeline_name":"identity-ui-fixture","status":"closed","lifecycle":"close","priority":"medium","subject":{"id":occurrence,"type":"event","category":species},"data":{"camera_id":"identity-test-camera","recognition":decision.packet_summary()}})
        store.close_occurrence(occurrence)
    store.close()
    print("Prepared four synthetic occurrences. No recognition quality is certified.")


if __name__ == "__main__":
    main()
