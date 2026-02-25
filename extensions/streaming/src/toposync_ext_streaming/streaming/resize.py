from __future__ import annotations

import cv2
import numpy


def resize_frame_contain(frame_bgr: numpy.ndarray, target_width: int, target_height: int) -> numpy.ndarray:
    normalized_target_width = max(1, int(target_width))
    normalized_target_height = max(1, int(target_height))

    source_frame = numpy.asarray(frame_bgr)
    if source_frame.ndim == 2:
        source_frame = cv2.cvtColor(source_frame, cv2.COLOR_GRAY2BGR)
    if source_frame.ndim != 3:
        raise ValueError("Expected frame with shape (height, width, channels)")
    if source_frame.shape[2] > 3:
        source_frame = source_frame[:, :, :3]
    if source_frame.shape[2] < 3:
        raise ValueError("Expected frame with at least 3 channels")
    if source_frame.dtype != numpy.uint8:
        source_frame = numpy.clip(source_frame, 0, 255).astype(numpy.uint8)

    source_height, source_width = source_frame.shape[:2]
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Source frame has invalid dimensions")

    if source_width == normalized_target_width and source_height == normalized_target_height:
        return numpy.ascontiguousarray(source_frame)

    source_aspect_ratio = float(source_width) / float(source_height)
    target_aspect_ratio = float(normalized_target_width) / float(normalized_target_height)

    if source_aspect_ratio >= target_aspect_ratio:
        resized_width = normalized_target_width
        resized_height = int(round(normalized_target_width / source_aspect_ratio))
    else:
        resized_height = normalized_target_height
        resized_width = int(round(normalized_target_height * source_aspect_ratio))

    resized_width = max(1, min(normalized_target_width, int(resized_width)))
    resized_height = max(1, min(normalized_target_height, int(resized_height)))

    interpolation = cv2.INTER_AREA if (resized_width < source_width or resized_height < source_height) else cv2.INTER_LINEAR
    resized_frame = cv2.resize(source_frame, (resized_width, resized_height), interpolation=interpolation)

    output_frame = numpy.zeros((normalized_target_height, normalized_target_width, 3), dtype=numpy.uint8)

    offset_x = (normalized_target_width - resized_width) // 2
    offset_y = (normalized_target_height - resized_height) // 2

    output_frame[offset_y : offset_y + resized_height, offset_x : offset_x + resized_width] = resized_frame
    return output_frame
