from django.conf import settings

from .models import StorefrontRequest


ACTIVE_PIPELINE_STATES = {
    StorefrontRequest.PIPELINE_ACCEPTED,
    StorefrontRequest.PIPELINE_PREPARING,
    StorefrontRequest.PIPELINE_PUBLISHING,
    StorefrontRequest.PIPELINE_PROVISIONING,
    StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
    StorefrontRequest.PIPELINE_BUILDING,
    StorefrontRequest.PIPELINE_VERIFYING,
}


def approved_release_revision():
    return str(getattr(settings, "STOREFRONT_RELEASE_REVISION", "") or "").strip().lower()


def storefront_release_status(request):
    """Return a stable business status without exposing provider details."""
    has_live_release = bool(
        request.deployed_url
        and (request.deployed_revision or request.last_known_good_revision)
    )
    if has_live_release and request.pipeline_state in ACTIVE_PIPELINE_STATES:
        return "updating"
    if has_live_release and request.pipeline_state == StorefrontRequest.PIPELINE_FAILED:
        return "update_failed"
    if request.pipeline_state == StorefrontRequest.PIPELINE_LIVE:
        approved = approved_release_revision()
        # A live storefront without a recorded source revision is not proven to
        # contain the approved bundle. Surface it for a controlled update just
        # like any explicitly outdated release instead of calling it current.
        if approved and request.deployed_revision != approved:
            return "update_available"
        return "current"
    if request.pipeline_state == StorefrontRequest.PIPELINE_FAILED:
        return "failed"
    return "preparing"
