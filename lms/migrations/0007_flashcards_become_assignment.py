"""Карточки-тренажёр становятся разновидностью задания.

Раньше набор карточек был отдельной сущностью ``FlashcardDeck`` со своим
экраном и отдельной строкой в теме. Теперь это задание типа ``flashcards``:
набор карточек темы превращается в задание той же темы, а личный словарь
ученика остаётся у него самого — карточки без задания.

Миграция сохраняет все карточки, состояния повторений и порядок: сначала
появляются новые поля и данные, затем удаляются старые.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def decks_to_assignments(apps, schema_editor):
    """Наборы темы → задания-тренажёры; личные наборы → карточки ученика."""
    Assignment = apps.get_model("lms", "Assignment")
    Flashcard = apps.get_model("lms", "Flashcard")
    FlashcardDeck = apps.get_model("lms", "FlashcardDeck")

    decks = FlashcardDeck.objects.select_related("topic").order_by("pk")
    for deck in decks.iterator():
        cards = Flashcard.objects.filter(deck_id=deck.pk)
        if deck.topic_id:
            assignment = Assignment.objects.create(
                topic_id=deck.topic_id,
                title=deck.title[:200],
                description=deck.description,
                assignment_type="flashcards",
                status="published" if deck.is_active else "draft",
                max_points=0,
                order=deck.order,
                is_active=deck.is_active,
            )
            cards.update(assignment=assignment)
        elif deck.owner_id:
            cards.update(owner_id=deck.owner_id)


def assignments_to_decks(apps, schema_editor):
    """Обратный ход: задания-тренажёры → наборы, личные карточки → наборы ученика."""
    Assignment = apps.get_model("lms", "Assignment")
    Flashcard = apps.get_model("lms", "Flashcard")
    FlashcardDeck = apps.get_model("lms", "FlashcardDeck")

    for assignment in Assignment.objects.filter(assignment_type="flashcards").iterator():
        deck = FlashcardDeck.objects.create(
            topic_id=assignment.topic_id,
            title=assignment.title[:150],
            description=assignment.description,
            order=assignment.order,
            is_active=assignment.is_active,
        )
        Flashcard.objects.filter(assignment_id=assignment.pk).update(deck=deck)

    owner_ids = (
        Flashcard.objects.filter(owner__isnull=False)
        .values_list("owner_id", flat=True)
        .distinct()
    )
    for owner_id in list(owner_ids):
        deck = FlashcardDeck.objects.create(
            topic=None,
            owner_id=owner_id,
            title="Мой словарь",
            is_active=True,
        )
        Flashcard.objects.filter(owner_id=owner_id).update(deck=deck)


class Migration(migrations.Migration):

    dependencies = [
        ("lms", "0006_flashcarddeck_owner_alter_flashcarddeck_topic_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="assignment",
            name="assignment_type",
            field=models.CharField(
                choices=[
                    ("text", "Текстовый ответ"),
                    ("file", "Файл"),
                    ("audio", "Аудио"),
                    ("mixed", "Текст + файл/аудио"),
                    ("quiz", "Тест с автопроверкой"),
                    ("flashcards", "Карточки-тренажёр"),
                ],
                default="text",
                max_length=20,
                verbose_name="Тип ответа",
            ),
        ),
        migrations.AddField(
            model_name="flashcard",
            name="assignment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="cards",
                to="lms.assignment",
                verbose_name="Задание",
            ),
        ),
        migrations.AddField(
            model_name="flashcard",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="dictionary_cards",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Владелец личного словаря",
            ),
        ),
        migrations.RunPython(decks_to_assignments, assignments_to_decks),
        migrations.RemoveField(model_name="flashcard", name="deck"),
        migrations.DeleteModel(name="FlashcardDeck"),
        migrations.AddConstraint(
            model_name="flashcard",
            constraint=models.CheckConstraint(
                condition=models.Q(assignment__isnull=False, owner__isnull=True)
                | models.Q(assignment__isnull=True, owner__isnull=False),
                name="flashcard_single_owner",
            ),
        ),
    ]
