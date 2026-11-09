from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('storefront_factory', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='storefrontrequest',
            name='deploy_status',
            field=models.CharField(
                choices=[
                    ('not_deployed', 'Not deployed'),
                    ('deploying', 'Deploying…'),
                    ('deployed', 'Deployed'),
                    ('failed', 'Deploy failed'),
                ],
                default='not_deployed',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='storefrontrequest',
            name='vercel_project_id',
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
        migrations.AddField(
            model_name='storefrontrequest',
            name='deployed_url',
            field=models.URLField(blank=True, editable=False, max_length=200),
        ),
        migrations.AddField(
            model_name='storefrontrequest',
            name='deploy_log',
            field=models.TextField(blank=True, editable=False),
        ),
    ]
