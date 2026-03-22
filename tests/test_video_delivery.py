import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.responses import FileResponse

from app.api.v1 import files as files_api
from app.api.v1 import video as video_api
from app.core.exceptions import UpstreamException, ValidationException
from app.services.grok.utils import download as download_module
from app.services.grok.utils import video_assets


class _FakeResponse:
    def __init__(self, chunks, content_type="video/mp4"):
        self.headers = {"content-type": content_type}
        self._chunks = list(chunks)

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


def _video_result(content_url: str, meta: dict | None = None) -> dict:
    result = {
        "choices": [
            {
                "message": {
                    "content": f"[video]({content_url})",
                }
            }
        ]
    }
    if meta is not None:
        result["_video_delivery"] = meta
    return result


class VideoAssetServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_persist_video_saves_local_file_and_returns_local_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            legacy_dir = Path(temp_dir) / "legacy"

            def _cfg(key, default=None):
                values = {
                    "video.local_persist_enabled": True,
                    "video.local_retention_days": 7,
                    "app.app_url": "http://localhost:8000",
                }
                return values.get(key, default)

            with patch.object(video_assets, "VIDEO_MEDIA_DIR", media_dir), patch.object(
                video_assets, "LEGACY_VIDEO_DIR", legacy_dir
            ), patch(
                "app.services.grok.utils.video_assets.get_config",
                side_effect=_cfg,
            ), patch(
                "app.services.grok.utils.video_assets.AssetsDownloadReverse.request",
                new=AsyncMock(return_value=_FakeResponse([b"video-bytes"])),
            ):
                meta = await video_assets.VideoAssetService.persist_video(
                    "https://assets.grok.com/users/demo/generated/demo/generated_video.mp4",
                    "token",
                )

            self.assertEqual(meta.storage, "local")
            self.assertTrue(meta.url.startswith("http://localhost:8000/v1/files/video/"))
            self.assertTrue(Path(meta.path).exists())
            self.assertEqual(Path(meta.path).read_bytes(), b"video-bytes")
            self.assertEqual(meta.media_type, "video/mp4")
            self.assertIsNotNone(meta.expires_at)

    async def test_prepare_delivery_rejects_file_mode_when_local_persist_disabled(self):
        with patch(
            "app.services.grok.utils.video_assets.get_config",
            side_effect=lambda key, default=None: False
            if key == "video.local_persist_enabled"
            else default,
        ):
            with self.assertRaises(ValidationException):
                await video_assets.VideoAssetService.prepare_delivery(
                    "https://assets.grok.com/demo.mp4",
                    "token",
                    "file",
                )

    async def test_prepare_delivery_url_mode_falls_back_when_local_persist_fails(self):
        with patch(
            "app.services.grok.utils.video_assets.get_config",
            side_effect=lambda key, default=None: True
            if key == "video.local_persist_enabled"
            else default,
        ), patch(
            "app.services.grok.utils.video_assets.VideoAssetService.persist_video",
            new=AsyncMock(side_effect=OSError("disk full")),
        ):
            meta = await video_assets.VideoAssetService.prepare_delivery(
                "https://assets.grok.com/demo.mp4",
                "token",
                "url",
            )

        self.assertEqual(meta.url, "https://assets.grok.com/demo.mp4")
        self.assertEqual(meta.storage, "upstream_fallback")

    async def test_prepare_delivery_file_mode_wraps_local_persist_error(self):
        with patch(
            "app.services.grok.utils.video_assets.get_config",
            side_effect=lambda key, default=None: True
            if key == "video.local_persist_enabled"
            else default,
        ), patch(
            "app.services.grok.utils.video_assets.VideoAssetService.persist_video",
            new=AsyncMock(side_effect=OSError("disk full")),
        ):
            with self.assertRaises(UpstreamException) as context:
                await video_assets.VideoAssetService.prepare_delivery(
                    "https://assets.grok.com/demo.mp4",
                    "token",
                    "file",
                )

        self.assertEqual(context.exception.code, "video_local_persist_failed")
        self.assertEqual(context.exception.status_code, 502)

    async def test_cleanup_expired_files_removes_old_persistent_videos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir) / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            old_path = media_dir / "old.mp4"
            new_path = media_dir / "new.mp4"
            old_path.write_bytes(b"old")
            new_path.write_bytes(b"new")

            old_mtime = 1_700_000_000
            new_mtime = old_mtime + (8 * 24 * 3600)

            with patch.object(video_assets, "VIDEO_MEDIA_DIR", media_dir), patch(
                "app.services.grok.utils.video_assets.get_config",
                side_effect=lambda key, default=None: 7
                if key == "video.local_retention_days"
                else default,
            ), patch("app.services.grok.utils.video_assets.time.time", return_value=new_mtime):
                old_path.touch()
                new_path.touch()
                old_path.chmod(0o666)
                new_path.chmod(0o666)
                import os

                os.utime(old_path, (old_mtime, old_mtime))
                os.utime(new_path, (new_mtime, new_mtime))
                removed = await video_assets.VideoAssetService.cleanup_expired_files()

            self.assertEqual(removed, 1)
            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.exists())


class VideoApiDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_video_uses_request_delivery_mode_over_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "video.mp4"
            local_file.write_bytes(b"video")
            payload = video_api.VideoCreateRequest(
                prompt="demo",
                delivery_mode="file",
            )

            with patch(
                "app.api.v1.video.VideoService.completions",
                new=AsyncMock(
                    return_value=_video_result(
                        "https://assets.grok.com/demo.mp4",
                        {
                            "url": "/v1/files/video/video.mp4",
                            "storage": "local",
                            "expires_at": 123,
                            "path": str(local_file),
                            "media_type": "video/mp4",
                        },
                    )
                ),
            ) as completions_mock, patch(
                "app.api.v1.video.get_config",
                side_effect=lambda key, default=None: "url"
                if key == "video.delivery_mode_default"
                else default,
            ):
                response = await video_api._create_video_from_payload(payload, [])

            self.assertIsInstance(response, FileResponse)
            self.assertEqual(response.headers["x-video-local-url"], "/v1/files/video/video.mp4")
            self.assertEqual(response.headers["x-video-expires-at"], "123")
            self.assertEqual(
                completions_mock.await_args.kwargs["delivery_mode"],
                "file",
            )

    async def test_create_video_url_mode_returns_fallback_metadata(self):
        payload = video_api.VideoCreateRequest(prompt="demo", delivery_mode="url")

        with patch(
            "app.api.v1.video.VideoService.completions",
            new=AsyncMock(
                return_value=_video_result(
                    "https://assets.grok.com/demo.mp4",
                    {
                        "url": "https://assets.grok.com/demo.mp4",
                        "storage": "upstream_fallback",
                        "expires_at": None,
                        "path": "",
                        "media_type": "video/mp4",
                    },
                )
            ),
        ):
            response = await video_api._create_video_from_payload(payload, [])

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["url"], "https://assets.grok.com/demo.mp4")
        self.assertEqual(body["storage"], "upstream_fallback")
        self.assertIsNone(body["expires_at"])

    async def test_create_video_file_mode_raises_when_local_persist_failed(self):
        payload = video_api.VideoCreateRequest(prompt="demo", delivery_mode="file")

        with patch(
            "app.api.v1.video.VideoService.completions",
            new=AsyncMock(
                return_value=_video_result(
                    "https://assets.grok.com/demo.mp4",
                    {
                        "url": "https://assets.grok.com/demo.mp4",
                        "storage": "upstream_fallback",
                        "expires_at": None,
                        "path": "",
                        "media_type": "video/mp4",
                    },
                )
            ),
        ):
            with self.assertRaises(UpstreamException) as context:
                await video_api._create_video_from_payload(payload, [])

        self.assertEqual(context.exception.code, "video_local_persist_failed")


class VideoFilesAndDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_files_route_still_serves_legacy_video_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_file = Path(temp_dir) / "legacy.mp4"
            legacy_file.write_bytes(b"legacy")

            with patch(
                "app.api.v1.files.VideoAssetService.resolve_file_path",
                return_value=legacy_file,
            ):
                response = await files_api.get_video("legacy.mp4")

            self.assertIsInstance(response, FileResponse)
            self.assertEqual(Path(response.path), legacy_file)

    async def test_render_video_uses_local_persistent_url_when_enabled(self):
        service = download_module.DownloadService()
        try:
            with patch(
                "app.services.grok.utils.download.get_config",
                side_effect=lambda key, default=None: "url"
                if key == "app.video_format"
                else default,
            ), patch(
                "app.services.grok.utils.download.VideoAssetService.is_local_video_url",
                return_value=False,
            ), patch(
                "app.services.grok.utils.download.VideoAssetService.local_persist_enabled",
                return_value=True,
            ), patch(
                "app.services.grok.utils.download.VideoAssetService.prepare_delivery",
                new=AsyncMock(
                    return_value=video_assets.VideoDeliveryMeta(
                        url="/v1/files/video/demo.mp4",
                        storage="local",
                        path="D:/demo.mp4",
                        media_type="video/mp4",
                    )
                ),
            ):
                rendered = await service.render_video(
                    "https://assets.grok.com/demo.mp4",
                    "token",
                )
        finally:
            await service.close()

        self.assertEqual(rendered, "/v1/files/video/demo.mp4\n")


if __name__ == "__main__":
    unittest.main()
