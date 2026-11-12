from django.test import SimpleTestCase

from .models import default_storefront_appearance, normalize_storefront_appearance


class StorefrontAppearanceTests(SimpleTestCase):
    def test_collections_menu_background_is_normalized(self):
        appearance = default_storefront_appearance()
        appearance["colors"]["menu_dropdown"] = "#aabbcc"

        normalized = normalize_storefront_appearance(appearance)

        self.assertEqual(normalized["colors"]["menu_dropdown"], "#AABBCC")
