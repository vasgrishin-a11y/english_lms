# Generated for material type and multiple attachments
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import lms.models
import lms.validators


class Migration(migrations.Migration):

    dependencies = [
        ('lms', '0009_assignment_exam_mode'),
    ]

    operations = [
        migrations.AlterField(
            model_name='assignment',
            name='assignment_type',
            field=models.CharField(choices=[('text', 'Текстовый ответ'), ('file', 'Файл'), ('audio', 'Аудио'), ('mixed', 'Текст + файл/аудио'), ('quiz', 'Тест с автопроверкой'), ('flashcards', 'Карточки-тренажёр'), ('material', 'Материалы для занятий')], default='text', max_length=20, verbose_name='Тип ответа'),
        ),
        migrations.CreateModel(
            name='AssignmentAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to=lms.models.attachment_upload_to, validators=[django.core.validators.FileExtensionValidator(allowed_extensions=lms.validators.ALLOWED_FILE_EXTENSIONS), lms.validators.validate_upload], verbose_name='Файл')),
                ('order', models.PositiveIntegerField(default=0, verbose_name='Порядок')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attachments', to='lms.assignment', verbose_name='Задание')),
            ],
            options={
                'verbose_name': 'Вложение задания',
                'verbose_name_plural': 'Вложения заданий',
                'ordering': ['order', 'pk'],
            },
        ),
    ]
