"""Signed direct-upload helpers for merchant-owned Cloudinary assets."""
from __future__ import annotations

import time
import re
from dataclasses import dataclass

import cloudinary
import cloudinary.api
from cloudinary.utils import api_sign_request, verify_api_response_signature
from django.conf import settings
from rest_framework import serializers


@dataclass(frozen=True)
class UploadProfile:
    resource_type: str
    formats: tuple[str, ...]
    max_bytes: int


# Add future file kinds here only after they have an owning model, access rules,
# retention/deletion behavior, and an explicit MIME/extension allow-list.
IMAGE_UPLOAD = UploadProfile(
    resource_type="image",
    formats=("jpg", "jpeg", "png", "webp", "gif"),
    max_bytes=10 * 1024 * 1024,
)
STOREFRONT_IMAGE_UPLOAD = UploadProfile(
    resource_type="image",
    formats=("jpg", "jpeg", "png", "webp"),
    max_bytes=8 * 1024 * 1024,
)


def normalize_direct_upload_url(value: str) -> str:
    """Repair URLs produced when a direct-upload public ID is treated as a media path.

    ``MediaCloudinaryStorage`` prefixes ordinary Django uploads with ``media/``.
    Direct uploads already contain their complete Cloudinary public ID, so applying
    that prefix produces a URL for a resource that does not exist.
    """
    if not isinstance(value, str) or "/image/upload/" not in value:
        return value
    return re.sub(
        r"(/image/upload/)(?:v\d+/)?media/(tijarago/stores/)",
        r"\1\2",
        value,
        count=1,
    )


def media_asset_url(field, *, request=None, base_url: str = "") -> str:
    """Return one canonical public URL for any Django/Cloudinary file field."""
    if not field:
        return ""
    try:
        url = normalize_direct_upload_url(field.url)
    except (AttributeError, ValueError):
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if base_url:
        return f"{base_url.rstrip('/')}/{url.lstrip('/')}"
    return request.build_absolute_uri(url) if request else url


def direct_upload_available() -> bool:
    config = settings.CLOUDINARY_STORAGE
    backend = settings.STORAGES.get("default", {}).get("BACKEND", "")
    return (
        "cloudinary_storage" in backend
        and all(config.get(key) for key in ("CLOUD_NAME", "API_KEY", "API_SECRET"))
    )


def image_folder(store_id: int, purpose: str, owner_id: int | None = None) -> str:
    suffix = f"/{owner_id}" if owner_id is not None else ""
    return f"tijarago/stores/{store_id}/{purpose}{suffix}"


def signed_upload_payload(folder: str, profile: UploadProfile = IMAGE_UPLOAD) -> dict:
    if not direct_upload_available():
        return {"direct_upload": False}

    timestamp = int(time.time())
    params = {
        "allowed_formats": ",".join(profile.formats),
        "folder": folder,
        "overwrite": "false",
        "timestamp": timestamp,
        "unique_filename": "true",
        "use_filename": "true",
    }
    config = settings.CLOUDINARY_STORAGE
    return {
        "direct_upload": True,
        "upload_url": (
            f"https://api.cloudinary.com/v1_1/{config['CLOUD_NAME']}/"
            f"{profile.resource_type}/upload"
        ),
        "api_key": config["API_KEY"],
        "signature": api_sign_request(params, config["API_SECRET"]),
        "params": params,
        "resource_type": profile.resource_type,
        "max_bytes": profile.max_bytes,
        "allowed_formats": list(profile.formats),
    }


def validate_cloudinary_upload(value, *, folder: str, profile: UploadProfile = IMAGE_UPLOAD) -> str:
    """Verify Cloudinary's signed response and return its safe storage name."""
    if not direct_upload_available():
        raise serializers.ValidationError("Direct uploads are not configured.")
    if not isinstance(value, dict):
        raise serializers.ValidationError("Invalid direct-upload response.")

    public_id = str(value.get("public_id", ""))
    signature = str(value.get("signature", ""))
    version = value.get("version")
    resource_type = str(value.get("resource_type", ""))
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError("Invalid direct-upload metadata.") from exc

    if resource_type != profile.resource_type:
        raise serializers.ValidationError("Unexpected uploaded asset type.")
    if not public_id.startswith(f"{folder}/") or ".." in public_id:
        raise serializers.ValidationError("Uploaded asset does not belong to this resource.")

    # The response signature proves these identifiers came from Cloudinary,
    # rather than trusting a browser-supplied public_id.
    cloudinary.config(api_secret=settings.CLOUDINARY_STORAGE["API_SECRET"])
    if not verify_api_response_signature(public_id, version, signature):
        raise serializers.ValidationError("Cloudinary upload verification failed.")

    # Response signatures cover public_id/version. Read the authoritative
    # metadata from Cloudinary rather than trusting browser-supplied bytes or
    # format fields when enforcing the upload policy.
    try:
        resource = cloudinary.api.resource(public_id, resource_type=profile.resource_type, type="upload")
        upload_format = str(resource.get("format", "")).lower()
        byte_count = int(resource.get("bytes", -1))
        stored_version = int(resource.get("version", -1))
    except Exception as exc:
        raise serializers.ValidationError("Cloudinary could not verify the uploaded asset.") from exc
    if upload_format not in profile.formats:
        raise serializers.ValidationError("Unsupported uploaded file format.")
    if stored_version != version:
        raise serializers.ValidationError("Uploaded asset version could not be verified.")
    if byte_count < 1 or byte_count > profile.max_bytes:
        raise serializers.ValidationError("Uploaded file exceeds the allowed size.")
    return public_id
