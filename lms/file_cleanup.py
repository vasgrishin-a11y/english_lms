import logging

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from .models import Assignment, QuestionResponse, Submission

logger = logging.getLogger(__name__)


def delete_unreferenced_file(name, using="default"):
    if not name:
        return
    if (
        Assignment.objects.using(using).filter(material_file=name).exists()
        or Submission.objects.using(using).filter(file_answer=name).exists()
        or QuestionResponse.objects.using(using).filter(file_answer=name).exists()
    ):
        return
    try:
        default_storage.delete(name)
    except OSError:
        # The periodic cleanup command can retry a failed storage operation.
        logger.exception("file.cleanup_failed")


@receiver(post_delete, sender=Assignment)
@receiver(post_delete, sender=Submission)
@receiver(post_delete, sender=QuestionResponse)
def cleanup_deleted_file(sender, instance, using, **kwargs):
    name = instance.material_file.name if sender is Assignment else instance.file_answer.name
    if name:
        transaction.on_commit(
            lambda: delete_unreferenced_file(name, using), using=using, robust=True
        )


@receiver(pre_save, sender=Assignment)
def cleanup_replaced_material(sender, instance, using, raw=False, **kwargs):
    if raw or not instance.pk:
        return
    old = (
        sender.objects.using(using)
        .filter(pk=instance.pk)
        .values_list("material_file", flat=True)
        .first()
    )
    if old and old != instance.material_file.name:
        transaction.on_commit(
            lambda: delete_unreferenced_file(old, using), using=using, robust=True
        )
