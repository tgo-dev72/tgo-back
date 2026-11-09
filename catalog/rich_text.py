import re
from html import escape
from html.parser import HTMLParser


ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "em", "h2", "h3", "i", "li",
    "ol", "p", "s", "strike", "strong", "u", "ul", "mark",
}
DROP_WITH_CONTENT_TAGS = {"iframe", "object", "script", "style", "svg"}
VOID_TAGS = {"br"}
SAFE_LINK = re.compile(r"^(?:https?://|mailto:|tel:|/(?!/)|#)", re.IGNORECASE)


class _ProductDescriptionSanitizer(HTMLParser):
    """Small allow-list sanitizer for merchant-authored product descriptions."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.blocked_depth:
            self.blocked_depth += 1
            return
        if tag in DROP_WITH_CONTENT_TAGS:
            self.blocked_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = next((value for key, value in attrs if key.lower() == "href"), "") or ""
            href = href.strip()
            if SAFE_LINK.match(href) and not re.search(r"[\x00-\x20\\]", href):
                self.output.append(
                    f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
                )
            else:
                self.output.append("<a>")
            return
        if tag in {"p", "h2", "h3"}:
            style = next((value for key, value in attrs if key.lower() == "style"), "") or ""
            aligned = re.fullmatch(r"\s*text-align:\s*(left|center|right|justify)\s*;?\s*", style, re.IGNORECASE)
            if aligned:
                self.output.append(f'<{tag} style="text-align: {aligned.group(1).lower()}">')
                return
        self.output.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.blocked_depth:
            self.blocked_depth -= 1
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.blocked_depth:
            self.output.append(escape(data, quote=False))


def sanitize_product_description(value):
    """Return safe, portable HTML while retaining supported rich-text structure."""
    if not isinstance(value, str) or not value:
        return ""
    sanitizer = _ProductDescriptionSanitizer()
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.output).strip()
