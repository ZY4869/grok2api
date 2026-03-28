import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin.cache import router as cache_router
from app.services.grok.utils import local_assets as local_assets_module


class AdminCacheExportApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = Path(self.tempdir.name)
        self.app = FastAPI()
        self.app.include_router(cache_router, prefix="/v1/admin")
        self.client = TestClient(self.app)
        self.auth_headers = {"Authorization": "Bearer grok2api"}

    def tearDown(self):
        self.client.close()

    def test_export_image_cache_csv(self):
        with patch.object(local_assets_module, "DATA_DIR", self.data_dir):
            store = local_assets_module.LocalAssetStore()
            asyncio.run(
                store.write_bytes(
                    "image",
                    "export-image.jpg",
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

            response = self.client.get(
                "/v1/admin/cache/export?type=image",
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers.get("content-type", ""))
        self.assertIn("cache-image-", response.headers.get("content-disposition", ""))
        content = response.content.decode("utf-8-sig")
        self.assertIn("export-image.jpg", content)
        self.assertIn("first@example.com", content)
        self.assertIn("localized_image", content)

    def test_export_rejects_online_cache(self):
        response = self.client.get(
            "/v1/admin/cache/export?type=online",
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
