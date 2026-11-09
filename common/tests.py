from django.test import SimpleTestCase
from types import SimpleNamespace

from api.storefront_config import _sections_for_client
from common.media_uploads import media_asset_url, normalize_direct_upload_url


class DirectUploadUrlTests(SimpleTestCase):
    broken = (
        "https://res.cloudinary.com/demo/image/upload/v1/media/"
        "tijarago/stores/2/storefront/banner"
    )
    expected = (
        "https://res.cloudinary.com/demo/image/upload/"
        "tijarago/stores/2/storefront/banner"
    )

    def test_removes_storage_prefix_and_synthetic_version(self):
        self.assertEqual(normalize_direct_upload_url(self.broken), self.expected)

    def test_canonical_media_url_repairs_cloudinary_field_urls(self):
        self.assertEqual(media_asset_url(SimpleNamespace(url=self.broken)), self.expected)

    def test_normalizes_published_section_images_without_mutating_document(self):
        sections = [{
            "id": "hero",
            "type": "hero",
            "enabled": True,
            "settings": {"image_url": self.broken},
        }]

        result = _sections_for_client(sections, rich_text=True)

        self.assertEqual(result[0]["settings"]["image_url"], self.expected)
        self.assertEqual(sections[0]["settings"]["image_url"], self.broken)
