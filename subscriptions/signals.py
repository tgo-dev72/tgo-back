from django.db import connection

from .models import SubscriptionPlan
from .bootstrap import restore_commercial_reference_data


def restore_subscription_reference_data(**kwargs):
    table = SubscriptionPlan._meta.db_table
    tables = connection.introspection.table_names()
    if table not in tables:
        return
    with connection.cursor() as cursor:
        actual_columns = {
            item.name for item in connection.introspection.get_table_description(cursor, table)
        }
    expected_columns = {field.column for field in SubscriptionPlan._meta.local_fields}
    if not expected_columns.issubset(actual_columns):
        # A deliberate backwards migration is using a historical schema while
        # the running code still imports the current model. Do not repopulate
        # reference data until code and schema match again.
        return
    restore_commercial_reference_data()
