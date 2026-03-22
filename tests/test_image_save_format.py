import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.grok.services import image as image_module


def _make_blob(raw: bytes, mime: str | None = None) -> str:
    payload = base64.b64encode(raw).decode("utf-8")
    if not mime:
        return payload
    return f"data:{mime};base64,{payload}"


PNG_RAW = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)
JPEG_RAW = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01" + (b"\x00" * 12)
WEBP_RAW = b"RIFF\x1a\x00\x00\x00WEBPVP8 " + (b"\x00" * 16)


class ImageSaveFormatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)

    def _config_side_effect(self):
        def _get_config(key, default=None):
            if key == "app.app_url":
                return ""
            return default

        return _get_config

    def _saved_path(self, url: str) -> Path:
        filename = url.rsplit("/", 1)[-1]
        return self.data_dir / "tmp" / "image" / filename

    async def test_preserves_detected_extensions(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        png_blob = _make_blob(PNG_RAW, "image/png")
        jpg_blob = _make_blob(JPEG_RAW, "image/jpeg")
        webp_blob = _make_blob(WEBP_RAW, "image/webp")

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect(),
        ):
            png_url = await processor._save_blob("png-image", png_blob, True)
            jpg_url = await processor._save_blob("jpg-image", jpg_blob, True)
            webp_url = await processor._save_blob("webp-image", webp_blob, True)

        self.assertTrue(png_url.endswith(".png"))
        self.assertTrue(jpg_url.endswith(".jpg"))
        self.assertTrue(webp_url.endswith(".webp"))
        self.assertEqual(self._saved_path(png_url).read_bytes(), PNG_RAW)
        self.assertEqual(self._saved_path(jpg_url).read_bytes(), JPEG_RAW)
        self.assertEqual(self._saved_path(webp_url).read_bytes(), WEBP_RAW)

    async def test_detects_final_signature_without_mime(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        blob = _make_blob(PNG_RAW)

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect(),
        ):
            url = await processor._save_blob("signature-image", blob, True)

        self.assertTrue(url.endswith(".png"))
        self.assertEqual(self._saved_path(url).read_bytes(), PNG_RAW)

    async def test_keeps_existing_fallback_for_unknown_format(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        raw = b"not-a-known-image-format"
        blob = _make_blob(raw)

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect(),
        ):
            final_url = await processor._save_blob("fallback-final", blob, True)
            preview_url = await processor._save_blob("fallback-preview", blob, False)

        self.assertTrue(final_url.endswith(".jpg"))
        self.assertTrue(preview_url.endswith(".png"))
        self.assertEqual(self._saved_path(final_url).read_bytes(), raw)
        self.assertEqual(self._saved_path(preview_url).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
