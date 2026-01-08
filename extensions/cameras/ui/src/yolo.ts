function toTitleCaseWords(value: string): string {
  return value
    .split(" ")
    .map((part) => {
      const trimmed = part.trim();
      if (!trimmed) return "";
      return `${trimmed.slice(0, 1).toUpperCase()}${trimmed.slice(1)}`;
    })
    .join(" ")
    .trim();
}

export const YOLO_V12_CATEGORIES = [
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
] as const;

export type YoloV12Category = (typeof YOLO_V12_CATEGORIES)[number];

export const YOLO_LEGACY_CATEGORY_MAP: Record<string, YoloV12Category> = {
  motorbike: "motorcycle",
  aeroplane: "airplane",
  sofa: "couch",
  pottedplant: "potted plant",
  diningtable: "dining table",
  tvmonitor: "tv",
};

export function formatYoloCategoryLabel(category: YoloV12Category): string {
  if (category === "tv") return "TV";
  return toTitleCaseWords(category);
}

