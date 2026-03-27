import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.grok.utils import cache as cache_module
from app.services.grok.utils import local_assets as local_assets_module


class LocalAssetCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)

    def test_cache_lists_preview_url_for_images(self):
        with patch.object(local_assets_module, "DATA_DIR", self.data_dir):
            store = local_assets_module.LocalAssetStore()
            cache_service = cache_module.CacheService()

            self.assertEqual(cache_service.get_stats("image")["count"], 0)

            image_path = store.write_bytes("image", "preview-image.jpg", b"image-bytes")
            video_path = store.write_bytes("video", "clip.mp4", b"video-bytes")

            # write_bytes is async, so run it synchronously for this unittest.
            import asyncio

            image_path = asyncio.run(image_path)
            video_path = asyncio.run(video_path)

            self.assertTrue(image_path.exists())
            self.assertTrue(video_path.exists())

            image_items = cache_service.list_files("image")["items"]
            video_items = cache_service.list_files("video")["items"]

        self.assertEqual(cache_service.get_stats("image")["count"], 1)
        self.assertEqual(cache_service.get_stats("video")["count"], 1)
        self.assertEqual(image_items[0]["view_url"], "/v1/files/image/preview-image.jpg")
        self.assertEqual(image_items[0]["preview_url"], "/v1/files/image/preview-image.jpg")
        self.assertEqual(video_items[0]["view_url"], "/v1/files/video/clip.mp4")
        self.assertNotIn("preview_url", video_items[0])


if __name__ == "__main__":
    unittest.main()
