"""Checks shared by merchant/admin entry points and deployment workers."""
from .models import StorefrontRepositoryMigration


def migration_in_progress(request_id):
    return StorefrontRepositoryMigration.objects.filter(request_id=request_id).exclude(
        state__in=("finalized", "canceled"),
    ).exists()
