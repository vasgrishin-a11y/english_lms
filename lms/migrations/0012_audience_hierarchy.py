# Назначения по иерархии: группы и ученики у класса, главы, темы и задания.
#
# У задания одиночная «Группа» становится множественной «Группы»; уже
# выбранная группа переносится без потерь, затем старое поле удаляется.
from django.conf import settings
from django.db import migrations, models


def copy_group_to_groups(apps, schema_editor):
    Assignment = apps.get_model("lms", "Assignment")
    for assignment in Assignment.objects.exclude(group__isnull=True).only("pk", "group_id"):
        assignment.groups.add(assignment.group_id)


def copy_groups_back(apps, schema_editor):
    Assignment = apps.get_model("lms", "Assignment")
    for assignment in Assignment.objects.prefetch_related("groups"):
        first = assignment.groups.order_by("name").first()
        if first is not None:
            assignment.group_id = first.pk
            assignment.save(update_fields=["group"])


def audience_fields(model_name):
    return [
        migrations.AddField(
            model_name=model_name,
            name="groups",
            field=models.ManyToManyField(
                blank=True,
                help_text="Назначить целиком группам учеников",
                related_name="%(class)ss",
                to="lms.group",
                verbose_name="Группы",
            ),
        ),
        migrations.AddField(
            model_name=model_name,
            name="students",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"profile__role": "student"},
                related_name="assigned_%(class)ss",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Ученики персонально",
            ),
        ),
    ]


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lms", "0011_chapter_and_issued_password"),
    ]

    operations = [
        *audience_fields("block"),
        *audience_fields("chapter"),
        *audience_fields("topic"),
        migrations.AlterField(
            model_name="assignment",
            name="assigned_students",
            field=models.ManyToManyField(
                blank=True,
                help_text="Открыть задание отдельным ученикам помимо групп",
                related_name="assigned_assignments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Ученики персонально",
            ),
        ),
        # Старый FK пока жив, но освобождает related_name для нового поля.
        migrations.AlterField(
            model_name="assignment",
            name="group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="legacy_assignments",
                to="lms.group",
                verbose_name="Группа",
            ),
        ),
        migrations.AddField(
            model_name="assignment",
            name="groups",
            field=models.ManyToManyField(
                blank=True,
                help_text="Назначить задание целиком группам учеников",
                related_name="assignments",
                to="lms.group",
                verbose_name="Группы",
            ),
        ),
        migrations.RunPython(copy_group_to_groups, copy_groups_back),
        migrations.RemoveField(model_name="assignment", name="group"),
    ]
