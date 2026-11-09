"""SSRF-safe ingestion of remote catalog images into owned storage."""

from __future__ import annotations

import ipaddress
import socket
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError

from .models import Product, ProductImage

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_REDIRECTS = 4


def _safe_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("Use a public HTTP or HTTPS image URL without credentials.")
    try:
        answers = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationError("The image host could not be resolved.") from exc
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise ValidationError("Image URLs cannot resolve to private, loopback, link-local or reserved networks.")


def download_image(url: str) -> tuple[bytes, str, str]:
    current = str(url).strip()
    for _ in range(MAX_REDIRECTS + 1):
        _safe_target(current)
        try:
            response = requests.get(
                current, stream=True, allow_redirects=False, timeout=(4, 12),
                headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif", "User-Agent": "TijaraGO-Media-Ingest/1.0"},
            )
        except requests.RequestException as exc:
            raise ValidationError("The remote image could not be downloaded.") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValidationError("The image redirect has no destination.")
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            response.close()
            raise ValidationError(f"Image download returned HTTP {response.status_code}.")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_IMAGE_BYTES:
            response.close(); raise ValidationError("The image exceeds the 10 MB limit.")
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                response.close(); raise ValidationError("The image exceeds the 10 MB limit.")
        response.close()
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValidationError("The image dimensions are too large.")
                image_format = (image.format or "").lower()
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValidationError("The downloaded content is not a valid supported image.") from exc
        extension = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "avif": ".avif"}.get(image_format)
        if not extension:
            raise ValidationError("The downloaded image format is not supported.")
        return bytes(data), extension, current
    raise ValidationError("The image has too many redirects.")


def ingest_product_image(*, product: Product, url: str, order: int, is_primary: bool,
                         source_system: str = "", source_id: str = "", alt_text: str = "") -> ProductImage:
    existing = None
    if source_system and source_id:
        existing = ProductImage.objects.filter(product=product, source_system=source_system, source_id=source_id).first()
    if existing:
        return existing
    data, extension, final_url = download_image(url)
    stem = PurePosixPath(urlparse(final_url).path).stem[:80] or f"image-{order + 1}"
    media = ProductImage(
        product=product, order=order, is_primary=is_primary, alt_text=alt_text[:255],
        source_system=source_system, source_id=source_id, source_url=url,
    )
    media.image.save(f"{stem}{extension}", ContentFile(data), save=False)
    media.full_clean()
    media.save()
    return media


def reference_product_image(*, product: Product, url: str, order: int, is_primary: bool,
                            source_system: str = "", source_id: str = "", alt_text: str = "") -> ProductImage:
    """Persist validated remote media without blocking an import on a copy job."""
    existing = None
    if source_system and source_id:
        existing = ProductImage.objects.filter(
            product=product, source_system=source_system, source_id=source_id,
        ).first()
    if existing:
        return existing
    media = ProductImage(
        product=product, order=order, is_primary=is_primary, alt_text=alt_text[:255],
        external_url=url, source_system=source_system, source_id=source_id, source_url=url,
    )
    media.full_clean()
    media.save()
    return media
