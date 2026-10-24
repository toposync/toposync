"""Bounded event sampling and image encoding; the vision extension owns segmentation."""
from collections import OrderedDict
import io
import math
import threading

from .operators_sinks import _pil_image_from_array

_buffer_lock = threading.Lock()
_buffer_bytes = 0


def reserve_capture_bytes(size):
    global _buffer_bytes
    with _buffer_lock:
        if size < 0 or _buffer_bytes + size > 32 * 1024 * 1024:
            return False
        _buffer_bytes += size
        return True


def release_capture_bytes(size):
    global _buffer_bytes
    with _buffer_lock:
        _buffer_bytes -= size


class CaptureSelector:
    def __init__(self, rate):
        self.rate = rate
        self.subjects = OrderedDict()

    def select(self, evidence):
        key = (evidence["episode_id"], evidence["participant_id"], evidence["camera_id"], evidence["source_id"])
        scope = (evidence["clock_domain"], evidence["capture_instance"], evidence["capture_generation"])
        sequence, stamp = evidence["frame_sequence"], evidence["observed_at"]
        if sequence is None or scope[1] is None or scope[2] is None or not math.isfinite(stamp):
            return "missing_provenance"
        previous = self.subjects.get(key)
        if previous and previous[0] == scope:
            if sequence <= previous[1] or stamp <= previous[4]:
                return "duplicate_or_reordered"
            if math.floor((stamp - previous[3]) * self.rate + 1e-9) <= math.floor((previous[2] - previous[3]) * self.rate + 1e-9):
                self.subjects[key] = (scope, sequence, previous[2], previous[3], stamp)
                return "cadence"
        # Integer-second origins align participants sharing the same source clock.
        origin = previous[3] if previous and previous[0] == scope else math.floor(stamp)
        self.subjects[key] = (scope, sequence, stamp, origin, stamp)
        self.subjects.move_to_end(key)
        if len(self.subjects) > 256:
            self.subjects.popitem(last=False)
        return None


def encode_capture(data, profile):
    from PIL import Image
    if isinstance(data, (bytes, bytearray, memoryview)):
        with Image.open(io.BytesIO(bytes(data))) as decoded:
            if decoded.width * decoded.height > 16_777_216:
                raise ValueError("Capture exceeds the pixel budget")
            image = decoded.convert("RGBA" if "A" in decoded.getbands() else "RGB")
    else:
        if getattr(data, "size", 0) > 16_777_216 * 4:
            raise ValueError("Capture exceeds the pixel budget")
        image = _pil_image_from_array(data)
    edge, quality = (320, 65) if profile == "light" else (640, 85)
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "WEBP", quality=quality, method=4)
    return output.getvalue(), {"width": image.width, "height": image.height, "profile": profile,
        "format": "webp", "has_alpha": "A" in image.getbands()}
