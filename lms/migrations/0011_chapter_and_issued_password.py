# Глава — новый уровень иерархии между классом и темой; выданный пароль ученика.
#
# Существующие темы «висят» прямо на классе. Чтобы иерархия стала строгой без
# ручной работы, миграция создаёт в каждом классе с темами главу «Общее» и
# переносит темы туда. Порядок тем сохраняется.
import django.db.models.deletion
from django.db import migrations, models

DEFAULT_TITLE = "Общее"
DEFAULT_SLUG = "obshchee"


def create_default_chapters(apps, schema_editor):
    Block = apps.get_model("lms", "Block")
    Chapter = apps.get_model("lms", "Chapter")
    Topic = apps.get_model("lms", "Topic")
    block_ids = Topic.objects.values_list("block_id", flat=True).distinct()
    for block in Block.objects.filter(pk__in=block_ids):
        chapter = Chapter.objects.create(
            block=block, title=DEFAULT_TITLE, slug=DEFAULT_SLUG, order=1
        )
        Topic.objects.filter(block=block, chapter__isnull=True).update(chapter=chapter)


def drop_default_chapters(apps, schema_editor):
    # При откате поле chapter удаляется вместе с таблицей глав — данных терять нечего.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("lms", "0010_assignment_material_and_attachments"),
    ]

    operations = [
        migrations.AlterField(
            model_name="block",
            name="name",
            field=models.CharField(max_length=150, verbose_name="Название класса"),
        ),
        migrations.AlterModelOptions(
            name="block",
            options={
                "ordering": ["order", "name"],
                "verbose_name": "Класс",
                "verbose_name_plural": "Классы",
            },
        ),
        migrations.AddField(
            model_name="profile",
            name="issued_password",
            field=models.TextField(blank=True, verbose_name="Выданный пароль"),
        ),
        migrations.AlterField(
            model_name="topic",
            name="block",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="topics",
                to="lms.block",
                verbose_name="Класс",
            ),
        ),
        migrations.CreateModel(
            name="Chapter",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("title", models.CharField(max_length=200, verbose_name="Глава")),
                ("slug", models.SlugField(verbose_name="URL")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("order", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "block",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chapters",
                        to="lms.block",
                        verbose_name="Класс",
                    ),
                ),
            ],
            options={
                "verbose_name": "Глава",
                "verbose_name_plural": "Главы",
                "ordering": ["block", "order", "title"],
            },
        ),
        migrations.AddConstraint(
            model_name="chapter",
            constraint=models.UniqueConstraint(
                fields=("block", "slug"), name="unique_chapter_slug_per_block"
            ),
        ),
        # Сначала поле допускает NULL, чтобы заполнить его данными, затем становится обязательным.
        migrations.AddField(
            model_name="topic",
            name="chapter",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="topics",
                to="lms.chapter",
                verbose_name="Глава",
            ),
        ),
        migrations.RunPython(create_default_chapters, drop_default_chapters),
        migrations.AlterField(
            model_name="topic",
            name="chapter",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="topics",
                to="lms.chapter",
                verbose_name="Глава",
            ),
        ),
        migrations.AlterModelOptions(
            name="topic",
            options={
                "ordering": ["block", "chapter__order", "order", "title"],
                "verbose_name": "Тема",
                "verbose_name_plural": "Темы",
            },
        ),
    ]
