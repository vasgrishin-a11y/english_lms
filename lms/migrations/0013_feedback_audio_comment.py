# Голосовой комментарий преподавателя к проверке сдачи.
from django.db import migrations, models
import django.core.validators
import lms.models
import lms.validators


class Migration(migrations.Migration):

    dependencies = [
        ("lms", "0012_audience_hierarchy"),
    ]

    operations = [
        migrations.AddField(
            model_name="feedback",
            name="audio_comment",
            field=models.FileField(
                blank=True,
                upload_to=lms.models.feedback_audio_upload_to,
                validators=[
                    django.core.validators.FileExtensionValidator(
                        allowed_extensions=lms.validators.ALLOWED_FILE_EXTENSIONS
                    ),
                    lms.validators.validate_upload,
                ],
                verbose_name="Голосовой комментарий",
            ),
        ),
    ]
