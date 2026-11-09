from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from .models import default_content_sections, localized_text, validate_storefront_sections


class StorefrontRichTextValidationTests(SimpleTestCase):
    def test_promotional_banner_accepts_bounded_layout_and_localized_copy(self):
        validate_storefront_sections([{
            "id": "seasonal-offer",
            "type": "promotional_banner",
            "enabled": True,
            "settings": {
                "title": {"en": "Seasonal offer", "fr": "Offre saisonnière", "ar": "عرض موسمي"},
                "body": {"en": "<p>Discover the edit.</p>", "fr": "", "ar": ""},
                "cta_label": {"en": "Discover", "fr": "Découvrir", "ar": "اكتشف"},
                "cta_url": "/collections/all",
                "image_url": "https://res.cloudinary.com/demo/image/upload/banner.jpg",
                "width": "full",
                "height": "standard",
            },
        }])

    def test_formatted_section_copy_is_accepted(self):
        sections = default_content_sections(
            localized_text("Summer collection"),
            localized_text('<p style="text-align: center">One line<br>then another</p>'),
            localized_text('<p><strong>Bold</strong> <mark>highlight</mark> <a href="https://example.com" target="_blank" rel="noopener noreferrer">link</a></p><ul><li><p>Item</p></li></ul>'),
        )

        validate_storefront_sections(sections)

    def test_unsafe_markup_is_rejected(self):
        sections = default_content_sections(
            localized_text("Summer collection"),
            localized_text("Plain copy"),
            localized_text("<p>Hello<script>alert(1)</script></p>"),
        )

        with self.assertRaises(ValidationError):
            validate_storefront_sections(sections)

    def test_unsafe_alignment_style_is_rejected(self):
        sections = default_content_sections(
            localized_text("Summer collection"),
            localized_text('<p style="text-align: center; color: red">Unsafe style</p>'),
            localized_text("Plain copy"),
        )

        with self.assertRaises(ValidationError):
            validate_storefront_sections(sections)

    def test_link_sanitizer_drops_unsafe_relative_url(self):
        from catalog.rich_text import sanitize_product_description

        html = sanitize_product_description('<p><a href="/\\evil.example">Visit</a></p>')
        self.assertEqual(html, "<p><a>Visit</a></p>")

    def test_legacy_plain_copy_with_angle_bracket_remains_valid(self):
        sections = default_content_sections(
            localized_text("Summer collection"),
            localized_text("Ready in < 5 minutes"),
            localized_text("Fresh & simple"),
        )

        validate_storefront_sections(sections)
