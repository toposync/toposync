from __future__ import annotations

COCO80_LABELS: tuple[str, ...] = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)

BUILTIN_LABEL_SOURCES: dict[str, tuple[str, ...]] = {
    "coco80": COCO80_LABELS,
}

OFFICIAL_RTMDET_DETECTION_MODEL_IDS: tuple[str, ...] = (
    "rtmdet_det_tiny",
    "rtmdet_det_small",
    "rtmdet_det_medium",
)

OFFICIAL_RFDETR_DETECTION_MODEL_IDS: tuple[str, ...] = (
    "rfdetr_det_nano",
    "rfdetr_det_small",
    "rfdetr_det_medium",
)

OFFICIAL_DETECTION_MODEL_IDS: tuple[str, ...] = (
    *OFFICIAL_RTMDET_DETECTION_MODEL_IDS,
    *OFFICIAL_RFDETR_DETECTION_MODEL_IDS,
)

OFFICIAL_RTMDET_SEGMENTATION_MODEL_IDS: tuple[str, ...] = (
    "rtmdet_ins_tiny",
    "rtmdet_ins_small",
    "rtmdet_ins_medium",
)

OFFICIAL_RTMPOSE_MODEL_IDS: tuple[str, ...] = ()


def resolve_builtin_labels(source: str) -> list[str]:
    key = str(source or "").strip().lower()
    labels = BUILTIN_LABEL_SOURCES.get(key, ())
    return [str(item) for item in labels]
