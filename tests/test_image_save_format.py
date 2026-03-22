import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.grok.services import image as image_module


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _make_image_blob(fmt: str, *, include_mime: bool = True) -> tuple[bytes, str]:
    mode = "RGBA" if fmt == "PNG" else "RGB"
    color = (255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0)
    image = Image.new(mode, (2, 2), color)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    raw = buffer.getvalue()
    blob = base64.b64encode(raw).decode("utf-8")
    if not include_mime:
        return raw, blob
    mime_map = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
    }
    return raw, f"data:{mime_map[fmt]};base64,{blob}"


def _make_webp_signature_blob() -> tuple[bytes, str]:
    raw = b"RIFF\x1a\x00\x00\x00WEBPVP8 " + (b"\x00" * 16)
    blob = base64.b64encode(raw).decode("utf-8")
    return raw, f"data:image/webp;base64,{blob}"


class ImageSaveFormatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)

    def _config_side_effect(self, save_format: str):
        def _get_config(key, default=None):
            if key == "image.save_format":
                return save_format
            if key == "app.app_url":
                return ""
            return default

        return _get_config

    def _saved_path(self, url: str) -> Path:
        filename = url.rsplit("/", 1)[-1]
        return self.data_dir / "tmp" / "image" / filename

    async def test_source_mode_preserves_detected_extensions(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        png_raw, png_blob = _make_image_blob("PNG")
        jpg_raw, jpg_blob = _make_image_blob("JPEG")
        webp_raw, webp_blob = _make_webp_signature_blob()

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("source"),
        ):
            png_url = await processor._save_blob("png-image", png_blob, True)
            jpg_url = await processor._save_blob("jpg-image", jpg_blob, True)
            webp_url = await processor._save_blob("webp-image", webp_blob, True)

        self.assertTrue(png_url.endswith(".png"))
        self.assertTrue(jpg_url.endswith(".jpg"))
        self.assertTrue(webp_url.endswith(".webp"))
        self.assertEqual(self._saved_path(png_url).read_bytes(), png_raw)
        self.assertEqual(self._saved_path(jpg_url).read_bytes(), jpg_raw)
        self.assertEqual(self._saved_path(webp_url).read_bytes(), webp_raw)

    async def test_source_mode_detects_final_signature_without_mime(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        raw, blob = _make_image_blob("PNG", include_mime=False)

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("source"),
        ):
            url = await processor._save_blob("signature-image", blob, True)

        self.assertTrue(url.endswith(".png"))
        self.assertEqual(self._saved_path(url).read_bytes(), raw)

    async def test_source_mode_keeps_existing_fallback_for_unknown_format(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        raw = b"not-a-known-image-format"
        blob = base64.b64encode(raw).decode("utf-8")

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("source"),
        ):
            final_url = await processor._save_blob("fallback-final", blob, True)
            preview_url = await processor._save_blob("fallback-preview", blob, False)

        self.assertTrue(final_url.endswith(".jpg"))
        self.assertTrue(preview_url.endswith(".png"))
        self.assertEqual(self._saved_path(final_url).read_bytes(), raw)
        self.assertEqual(self._saved_path(preview_url).read_bytes(), raw)

    async def test_png_mode_transcodes_jpeg_to_png(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        _, blob = _make_image_blob("JPEG")

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("png"),
        ):
            url = await processor._save_blob("jpeg-to-png", blob, True)

        saved = self._saved_path(url).read_bytes()
        self.assertTrue(url.endswith(".png"))
        self.assertTrue(saved.startswith(PNG_SIGNATURE))

    async def test_png_mode_transcodes_webp_to_png(self):
        try:
            _, blob = _make_image_blob("WEBP")
        except Exception as exc:  # pragma: no cover - depends on Pillow build
            self.skipTest(f"WEBP support unavailable: {exc}")

        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("png"),
        ):
            url = await processor._save_blob("webp-to-png", blob, True)

        saved = self._saved_path(url).read_bytes()
        self.assertTrue(url.endswith(".png"))
        self.assertTrue(saved.startswith(PNG_SIGNATURE))

    async def test_png_mode_logs_warning_and_falls_back_to_source(self):
        processor = image_module.ImageWSBaseProcessor("grok-imagine-1.0", response_format="url")
        raw = b"broken-jpeg-payload"
        blob = f"data:image/jpeg;base64,{base64.b64encode(raw).decode('utf-8')}"

        with patch.object(image_module, "DATA_DIR", self.data_dir), patch.object(
            image_module,
            "get_config",
            side_effect=self._config_side_effect("png"),
        ), patch.object(image_module.logger, "warning") as warning_mock:
            url = await processor._save_blob("broken-image", blob, True)

        self.assertTrue(url.endswith(".jpg"))
        self.assertEqual(self._saved_path(url).read_bytes(), raw)
        warning_mock.assert_any_call(
            unittest.mock.ANY
        )


if __name__ == "__main__":
    unittest.main()
