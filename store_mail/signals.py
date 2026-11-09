from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import MailAttachment


@receiver(post_delete, sender=MailAttachment)
def delete_mail_attachment_file(sender, instance, **kwargs):
    if instance.file and instance.file.name:
        instance.file.storage.delete(instance.file.name)
