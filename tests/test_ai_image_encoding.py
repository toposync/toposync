import base64
import io

import numpy as np
import pytest
from PIL import Image
from toposync_ext_ai.providers import encode_image_base64


@pytest.mark.parametrize("kind", ["bgr", "bgra", "pil", "bytes", "gray"])
def test_provider_encoding_preserves_pipeline_colors_and_standard_images(kind):
    if kind in {"bgr", "bgra"}:
        image = np.zeros((32, 32, 4 if kind == "bgra" else 3), dtype=np.uint8)
        image[:, :, 2] = 255
        if kind == "bgra":
            image[:, :, 3] = 255
    elif kind == "gray":
        image = np.full((32, 32), 128, dtype=np.uint8)
    else:
        image = Image.new("RGB", (32, 32), (255, 0, 0))
        if kind == "bytes":
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            image = buffer.getvalue()
    encoded = encode_image_base64(image, max_side_px=128, jpeg_quality=95)
    result = Image.open(io.BytesIO(base64.b64decode(encoded.data_b64))).convert("RGB")
    red, green, blue = result.getpixel((16, 16))
    if kind == "gray":
        assert abs(red - 128) < 3 and abs(green - 128) < 3 and abs(blue - 128) < 3
    else:
        assert red > 245 and green < 10 and blue < 10
