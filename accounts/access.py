"""Account-level audience eligibility shared by login and cookie auth."""


def has_merchant_access(user):
    """Verified merchant principals and explicit store members may enter."""
    verified_principal = (
        hasattr(user, "merchant_profile")
        and user.email_verified_at is not None
    )
    return verified_principal or user.store_memberships.exists()
