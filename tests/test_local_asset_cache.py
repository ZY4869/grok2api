import asyncio
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

    def test_cache_lists_creator_metadata_and_export_csv(self):
        with patch.object(local_assets_module, "DATA_DIR", self.data_dir):
            store = local_assets_module.LocalAssetStore()
            cache_service = cache_module.CacheService()

            asyncio.run(
                store.write_bytes(
                    "image",
                    "preview-image.jpg",
                    b"image-bytes",
                    metadata={
                        "source_url": "https://cdn.example.com/generated/image.jpg",
                        "origin_kind": "localized_image",
                        "token": "token-a",
                        "email": "first@example.com",
                        "pool": "pool-a",
                        "trace_id": "trace-1",
                    },
                )
            )
            asyncio.run(
                store.write_bytes(
                    "image",
                    "preview-image.jpg",
                    b"image-bytes",
                    metadata={
                        "source_url": "https://cdn.example.com/generated/image.jpg",
                        "origin_kind": "localized_image",
                        "token": "token-b",
                        "email": "second@example.com",
                        "pool": "pool-b",
                        "trace_id": "trace-2",
                    },
                )
            )
            asyncio.run(
                store.write_bytes(
                    "video",
                    "clip.mp4",
                    b"video-bytes",
                    metadata={
                        "origin_kind": "localized_video",
                        "token": "token-c",
                        "email": "video@example.com",
                        "pool": "pool-c",
                    },
                )
            )

            image_items = cache_service.list_files("image")["items"]
            video_items = cache_service.list_files("video")["items"]
            exported = cache_service.export_csv("image").decode("utf-8-sig")

        self.assertEqual(cache_service.get_stats("image")["count"], 1)
        self.assertEqual(cache_service.get_stats("video")["count"], 1)
        self.assertEqual(image_items[0]["view_url"], "/v1/files/image/preview-image.jpg")
        self.assertEqual(image_items[0]["preview_url"], "/v1/files/image/preview-image.jpg")
        self.assertEqual(image_items[0]["creator_count"], 2)
        self.assertEqual(image_items[0]["source_kind"], "localized_image")
        self.assertEqual(image_items[0]["source_url"], "https://cdn.example.com/generated/image.jpg")
        self.assertIn("first@example.com", image_items[0]["creator_display"])
        self.assertIn("second@example.com", "\n".join(image_items[0]["creator_details"]))
        self.assertEqual(video_items[0]["creator_display"], "video@example.com (pool-c)")
        self.assertNotIn("preview_url", video_items[0])
        self.assertIn("creator_emails,creator_tokens,creator_pools,trace_ids", exported)
        self.assertIn("first@example.com | second@example.com", exported)
        self.assertIn("trace-1 | trace-2", exported)

    def test_delete_and_clear_remove_sidecar_files(self):
        with patch.object(local_assets_module, "DATA_DIR", self.data_dir):
            store = local_assets_module.LocalAssetStore()
            cache_service = cache_module.CacheService()

            asyncio.run(
                store.write_bytes(
                    "image",
                    "delete-me.jpg",
                    b"image-bytes",
                    metadata={"token": "token-a", "email": "first@example.com"},
                )
            )
            asyncio.run(
                store.write_bytes(
                    "video",
                    "delete-video.mp4",
                    b"video-bytes",
                    metadata={"token": "token-b", "email": "second@example.com"},
                )
            )

            image_meta = store.metadata_path("image", "delete-me.jpg")
            video_meta = store.metadata_path("video", "delete-video.mp4")
            self.assertTrue(image_meta.exists())
            self.assertTrue(video_meta.exists())

            deleted = cache_service.delete_file("image", "delete-me.jpg")
            cleared = cache_service.clear("video")

        self.assertTrue(deleted["deleted"])
        self.assertEqual(cleared["count"], 1)
        self.assertFalse((self.data_dir / "tmp" / "image" / "delete-me.jpg").exists())
        self.assertFalse((self.data_dir / "tmp" / "video" / "delete-video.mp4").exists())
        self.assertFalse(image_meta.exists())
        self.assertFalse(video_meta.exists())


if __name__ == "__main__":
    unittest.main()
