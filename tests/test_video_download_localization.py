import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.chat import router as chat_router
from app.api.v1.video import router as video_router
from app.services.grok.utils import download as download_module


class _DummyResponse:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.headers = {"content-type": content_type}


@asynccontextmanager
async def _noop_file_lock(*args, **kwargs):
    yield


class VideoDownloadLocalizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)

    def _config_side_effect(self, **overrides):
        defaults = {
            "app.app_url": "",
            "app.video_format": "url",
            "asset.download_timeout": 5,
            "cache.enable_auto_clean": False,
        }
        defaults.update(overrides)

        def _get_config(key, default=None):
            return defaults.get(key, default)

        return _get_config

    def _service(self) -> download_module.DownloadService:
        service = download_module.DownloadService()
        service.create = AsyncMock(return_value=object())
        return service

    async def test_localize_video_asset_downloads_any_https_domain(self):
        video_url = "https://cdn.example.com/generated/video.mp4?sig=abc"
        request_mock = AsyncMock(
            return_value=_DummyResponse(b"video-bytes", "video/mp4")
        )

        with patch.object(download_module, "DATA_DIR", self.data_dir), patch.object(
            download_module, "get_config", side_effect=self._config_side_effect()
        ), patch.object(
            download_module, "_get_download_semaphore", return_value=asyncio.Semaphore(1)
        ), patch.object(
            download_module, "_file_lock", _noop_file_lock
        ), patch.object(
            download_module.AssetsDownloadReverse, "request", request_mock
        ):
            service = self._service()
            localized = await service.localize_video_asset(
                video_url, "token", "video", asset_type="video"
            )

        self.assertTrue(localized.startswith("/v1/files/video/"))
        filename = localized.rsplit("/", 1)[-1]
        self.assertEqual(
            (self.data_dir / "tmp" / "video" / filename).read_bytes(),
            b"video-bytes",
        )
        self.assertEqual(request_mock.await_args.args[2], video_url)

    async def test_localize_video_asset_uses_domain_aware_cache_names(self):
        first = "https://a.example.com/generated/video.mp4"
        second = "https://b.example.com/generated/video.mp4"
        request_mock = AsyncMock(
            side_effect=[
                _DummyResponse(b"first", "video/mp4"),
                _DummyResponse(b"second", "video/mp4"),
            ]
        )

        with patch.object(download_module, "DATA_DIR", self.data_dir), patch.object(
            download_module, "get_config", side_effect=self._config_side_effect()
        ), patch.object(
            download_module, "_get_download_semaphore", return_value=asyncio.Semaphore(1)
        ), patch.object(
            download_module, "_file_lock", _noop_file_lock
        ), patch.object(
            download_module.AssetsDownloadReverse, "request", request_mock
        ):
            service = self._service()
            first_local = await service.localize_video_asset(first, "token", "video")
            second_local = await service.localize_video_asset(second, "token", "video")

        self.assertNotEqual(first_local, second_local)
        self.assertTrue((self.data_dir / "tmp" / "video" / first_local.rsplit("/", 1)[-1]).exists())
        self.assertTrue((self.data_dir / "tmp" / "video" / second_local.rsplit("/", 1)[-1]).exists())

    async def test_render_video_url_returns_local_relative_path(self):
        request_mock = AsyncMock(
            return_value=_DummyResponse(b"video-bytes", "video/mp4")
        )

        with patch.object(download_module, "DATA_DIR", self.data_dir), patch.object(
            download_module,
            "get_config",
            side_effect=self._config_side_effect(**{"app.video_format": "url"}),
        ), patch.object(
            download_module, "_get_download_semaphore", return_value=asyncio.Semaphore(1)
        ), patch.object(
            download_module, "_file_lock", _noop_file_lock
        ), patch.object(
            download_module.AssetsDownloadReverse, "request", request_mock
        ):
            service = self._service()
            rendered = await service.render_video(
                "https://cdn.example.com/generated/video.mp4", "token"
            )

        self.assertTrue(rendered.startswith("/v1/files/video/"))
        self.assertTrue(rendered.endswith("\n"))

    async def test_render_video_markdown_returns_local_link(self):
        request_mock = AsyncMock(
            return_value=_DummyResponse(b"video-bytes", "video/mp4")
        )

        with patch.object(download_module, "DATA_DIR", self.data_dir), patch.object(
            download_module,
            "get_config",
            side_effect=self._config_side_effect(**{"app.video_format": "markdown"}),
        ), patch.object(
            download_module, "_get_download_semaphore", return_value=asyncio.Semaphore(1)
        ), patch.object(
            download_module, "_file_lock", _noop_file_lock
        ), patch.object(
            download_module.AssetsDownloadReverse, "request", request_mock
        ):
            service = self._service()
            rendered = await service.render_video(
                "https://cdn.example.com/generated/video.mp4", "token"
            )

        self.assertRegex(rendered, r"^\[video\]\(/v1/files/video/.+\)$")

    async def test_render_video_html_localizes_video_and_falls_back_thumbnail(self):
        def _request_side_effect(session, token, url):
            if "thumbnail" in url:
                raise RuntimeError("thumbnail download failed")
            if url.endswith(".jpg"):
                return _DummyResponse(b"image-bytes", "image/jpeg")
            return _DummyResponse(b"video-bytes", "video/mp4")

        with patch.object(download_module, "DATA_DIR", self.data_dir), patch.object(
            download_module,
            "get_config",
            side_effect=self._config_side_effect(**{"app.video_format": "html"}),
        ), patch.object(
            download_module, "_get_download_semaphore", return_value=asyncio.Semaphore(1)
        ), patch.object(
            download_module, "_file_lock", _noop_file_lock
        ), patch.object(
            download_module.AssetsDownloadReverse,
            "request",
            new=AsyncMock(side_effect=_request_side_effect),
        ):
            service = self._service()
            rendered = await service.render_video(
                "https://cdn.example.com/generated/video.mp4",
                "token",
                "https://thumbs.example.com/thumbnail.jpg",
            )

        self.assertIn('src="/v1/files/video/', rendered)
        self.assertIn('poster="https://thumbs.example.com/thumbnail.jpg"', rendered)


class VideoApiLocalizationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(video_router, prefix="/v1")
        self.app.include_router(chat_router, prefix="/v1")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_videos_create_accepts_relative_local_file_url(self):
        model_info = SimpleNamespace(is_video=True)
        result = {
            "choices": [
                {
                    "message": {
                        "content": "/v1/files/video/localized-video.mp4\n",
                    }
                }
            ]
        }

        with patch("app.api.v1.video.ModelService.get", return_value=model_info), patch(
            "app.api.v1.video.VideoService.completions",
            new=AsyncMock(return_value=result),
        ):
            response = self.client.post(
                "/v1/videos",
                json={
                    "prompt": "make a video",
                    "model": "grok-imagine-1.0-video",
                    "size": "1792x1024",
                    "seconds": 6,
                    "quality": "standard",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "/v1/files/video/localized-video.mp4")

    def test_chat_completions_video_branch_keeps_response_shape_with_local_url(self):
        model_info = SimpleNamespace(is_video=True, is_image=False, is_image_edit=False)
        result = {
            "id": "chatcmpl-video",
            "object": "chat.completion",
            "created": 0,
            "model": "grok-imagine-1.0-video",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "/v1/files/video/localized-video.mp4\n",
                        "refusal": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

        with patch("app.api.v1.chat.ModelService.valid", return_value=True), patch(
            "app.api.v1.chat.ModelService.get", return_value=model_info
        ), patch(
            "app.api.v1.chat.VideoService.completions",
            new=AsyncMock(return_value=result),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-imagine-1.0-video",
                    "messages": [{"role": "user", "content": "make a video"}],
                    "stream": False,
                    "video_config": {
                        "aspect_ratio": "3:2",
                        "video_length": 6,
                        "resolution_name": "480p",
                        "preset": "custom",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "/v1/files/video/localized-video.mp4\n",
        )


if __name__ == "__main__":
    unittest.main()
