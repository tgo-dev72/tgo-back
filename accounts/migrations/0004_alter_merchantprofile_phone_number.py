import phonenumber_field.modelfields
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_merchant_registration_and_verification'),
    ]

    operations = [
        migrations.AlterField(
            model_name='merchantprofile',
            name='phone_number',
            field=phonenumber_field.modelfields.PhoneNumberField(blank=True, default='', max_length=128, region=None),
        ),
    ]
