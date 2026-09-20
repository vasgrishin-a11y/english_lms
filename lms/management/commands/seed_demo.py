"""Демо-данные для локального осмотра продукта: курс, задания, работы, карточки.

Команда идемпотентна (повторный запуск ничего не дублирует) и по умолчанию
отказывается работать на не-DEBUG окружении, чтобы не засорять прод.

    python manage.py seed_demo                    # создать демо-курс и пользователей
    python manage.py seed_demo --password '...'   # задать пароль демо-доступов

Пароль в коде не хранится: без --password команда генерирует случайный и
печатает его один раз в консоль.
"""

import io
import secrets
import string
import wave
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from lms.models import (
    Assignment,
    Block,
    CefrLevel,
    Choice,
    CommentSnippet,
    Flashcard,
    FlashcardDeck,
    Profile,
    Question,
    Skill,
    Submission,
    Topic,
)
from lms.services import (
    RATING_CHOICES,
    review_flashcard,
    review_submission,
    save_answer_draft,
    submit_assignment,
    submit_quiz,
)


def generate_password():
    """Случайный пароль демо-доступа: проходит валидаторы и не зашит в коде."""
    alphabet = string.ascii_letters + string.digits
    for _ in range(20):
        candidate = "".join(secrets.choice(alphabet) for _ in range(14)) + "!7"
        try:
            validate_password(candidate)
        except ValidationError:
            continue
        return candidate
    raise CommandError("Не удалось сгенерировать пароль демо-доступа. Передайте --password.")


# Минимальные корректные файлы: PNG 1×1 и 0.1 c тишины в WAV.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def wav_silence(seconds=0.2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * int(8000 * seconds))
    return stream.getvalue()


class Command(BaseCommand):
    help = "Создать демонстрационный курс, пользователей и учебные данные (локально)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=None,
            help="Пароль демо-пользователей. Без него генерируется случайный и печатается.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Разрешить запуск при DEBUG=False (не рекомендуется).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo работает только при DEBUG=True. Для продакшена используйте "
                "Django Admin или --force, если понимаете последствия."
            )
        password = options["password"] or generate_password()
        users = self._users(password)
        skills = self._skills()
        blocks = self._course(skills)
        self._work(blocks, users)
        self._snippets(users["teacher"])
        self.stdout.write(
            self.style.SUCCESS(
                "Демо-данные готовы. Логины: admin, teacher (преподаватель), "
                "anna, maxim, sofia (ученики)."
            )
        )
        self.stdout.write(
            self.style.WARNING(
                f"Пароль демо-доступа: {password} — только для локального стенда, "
                "в коде и репозитории не хранится."
            )
        )

    # ── Пользователи ────────────────────────────────────────────────
    def _users(self, password):
        User = get_user_model()

        def ensure(username, role, *, first="", last="", superuser=False, email=""):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "email": email or f"{username}@example.com",
                    "is_superuser": superuser,
                    "is_staff": superuser,
                },
            )
            if created:
                user.set_password(password)
                user.save()
            # Сигнал post_save уже создал профиль и закэшировал его на user.profile,
            # поэтому работаем с этим же экземпляром и не плодим второй.
            profile = getattr(user, "profile", None) or Profile.objects.create(user=user)
            if profile.role != role:
                profile.role = role
                profile.save(update_fields=["role", "updated_at"])
            user.profile = profile
            return user

        return {
            "admin": ensure(
                "admin", Profile.Role.TEACHER, first="Ada", last="Admin", superuser=True
            ),
            "teacher": ensure("teacher", Profile.Role.TEACHER, first="Maria", last="Petrova"),
            "anna": ensure("anna", Profile.Role.STUDENT, first="Anna", last="Ivanova"),
            "maxim": ensure("maxim", Profile.Role.STUDENT, first="Maxim", last="Sokolov"),
            "sofia": ensure("sofia", Profile.Role.STUDENT, first="Sofia", last="Kuznetsova"),
        }

    def _skills(self):
        wanted = [
            ("Грамматика", "grammar"),
            ("Лексика", "vocabulary"),
            ("Аудирование", "listening"),
            ("Говорение", "speaking"),
            ("Письмо", "writing"),
            ("Чтение", "reading"),
        ]
        skills = {}
        for order, (name, kind) in enumerate(wanted, start=1):
            skill, _ = Skill.objects.get_or_create(
                slug=slugify(name), defaults={"name": name, "kind": kind, "order": order}
            )
            skills[kind] = skill
        return skills

    # ── Курс: блоки → темы → задания ────────────────────────────────
    def _course(self, skills):
        now = timezone.now()
        plan = [
            (
                "Грамматика: времена группы Perfect",
                CefrLevel.B1,
                "Система времён, типичные ошибки ЕГЭ/ОГЭ.",
                [("Present Perfect и Past Simple", 1), ("Условные предложения", 2)],
            ),
            (
                "Лексика и говорение",
                CefrLevel.B2,
                "Темы travel, work, technology + устная практика.",
                [("Travel vocabulary", 1), ("Speaking: describe a trip", 2)],
            ),
            (
                "Подготовка к IELTS: Writing",
                CefrLevel.B2,
                "Task 1 и Task 2 по официальным band descriptors.",
                [("IELTS Task 1: line graph", 1), ("IELTS Task 2: essay", 2)],
            ),
        ]
        blocks = {}
        for block_name, level, description, topics in plan:
            block, _ = Block.objects.get_or_create(
                slug=slugify(block_name),
                defaults={
                    "name": block_name,
                    "description": description,
                    "cefr_level": level,
                    "order": len(blocks) + 1,
                },
            )
            for topic_name, order in topics:
                Topic.objects.get_or_create(
                    block=block,
                    slug=slugify(topic_name),
                    defaults={"title": topic_name, "order": order},
                )
            blocks[block_name] = block
        self._assignments(blocks, skills, now)
        return blocks

    def _assignments(self, blocks, skills, now):
        def topic(block_name, title):
            return blocks[block_name].topics.get(title=title)

        def make(topic_obj, title, description, *, kind, order, deadline=None, max_points=100):
            assignment, _ = Assignment.objects.get_or_create(
                topic=topic_obj,
                title=title,
                defaults={
                    "description": description,
                    "assignment_type": kind,
                    "order": order,
                    "deadline": deadline,
                    "max_points": max_points,
                },
            )
            return assignment

        grammar = "Грамматика: времена группы Perfect"
        lexis = "Лексика и говорение"
        ielts = "Подготовка к IELTS: Writing"

        quiz = make(
            topic(grammar, "Present Perfect и Past Simple"),
            "Тест: времена и маркеры",
            "Четыре вопроса с автоматической проверкой: выбор, пропуск, соответствие.",
            kind=Assignment.Type.QUIZ,
            order=1,
            max_points=0,
        )
        quiz.skills.set([skills["grammar"]])
        self._quiz_questions(quiz)

        gaps = make(
            topic(grammar, "Present Perfect и Past Simple"),
            "Раскройте скобки",
            "Перепишите предложения, поставив глагол в верную форму. 10 предложений.",
            kind=Assignment.Type.TEXT,
            order=2,
            deadline=now + timedelta(days=2),
        )
        gaps.skills.set([skills["grammar"], skills["writing"]])

        conditionals = make(
            topic(grammar, "Условные предложения"),
            "Conditionals: 12 предложений",
            "Составьте условные предложения 1–3 типа по схемам из конспекта.",
            kind=Assignment.Type.TEXT,
            order=1,
            deadline=now - timedelta(days=1),
        )
        conditionals.skills.set([skills["grammar"]])

        worksheet = make(
            topic(grammar, "Условные предложения"),
            "Рабочий лист: Conditionals",
            "Заполните рабочий лист и загрузите скан или фото. Допустимы PDF, DOCX, JPG, PNG.",
            kind=Assignment.Type.FILE,
            order=2,
            deadline=now + timedelta(days=3),
        )
        worksheet.skills.set([skills["grammar"], skills["reading"]])

        vocab = make(
            topic(lexis, "Travel vocabulary"),
            "Конспект лексики Travel",
            "Выпишите 20 словосочетаний с переводом и примерами. Файл или текст.",
            kind=Assignment.Type.MIXED,
            order=1,
            deadline=now + timedelta(days=4),
        )
        vocab.skills.set([skills["vocabulary"], skills["reading"]])
        if not vocab.material_file:
            vocab.material_file.save("travel-wordlist.png", ContentFile(PNG_1PX), save=False)
            vocab.save(update_fields=["material_file", "updated_at"])

        speaking = make(
            topic(lexis, "Speaking: describe a trip"),
            "Аудиоответ: моё путешествие",
            "Запишите 2–3 минуты рассказа о поездке. Оцениваем беглость и лексику.",
            kind=Assignment.Type.AUDIO,
            order=2,
            deadline=now + timedelta(days=6),
            max_points=50,
        )
        speaking.skills.set([skills["speaking"], skills["listening"]])

        task1 = make(
            topic(ielts, "IELTS Task 1: line graph"),
            "IELTS Task 1: line graph",
            "Опишите график по плану: overview → ключевые тенденции → детали. 150+ слов.",
            kind=Assignment.Type.TEXT,
            order=1,
            deadline=now + timedelta(days=1),
        )
        task1.skills.set([skills["writing"]])

        task2 = make(
            topic(ielts, "IELTS Task 2: essay"),
            "IELTS Task 2: opinion essay",
            "Черновик задания: эссе 250+ слов. Публикуем после проверки критериев.",
            kind=Assignment.Type.TEXT,
            order=2,
            deadline=now + timedelta(days=9),
        )
        task2.status = Assignment.Publication.DRAFT
        task2.save(update_fields=["status", "updated_at"])
        task2.skills.set([skills["writing"]])

        self._deck(topic(lexis, "Travel vocabulary"))
        self._deck(topic(grammar, "Present Perfect и Past Simple"))

    def _quiz_questions(self, quiz):
        if quiz.questions.exists():
            quiz.max_points = sum(question.points for question in quiz.questions.all())
            quiz.save(update_fields=["max_points", "updated_at"])
            return
        single = Question.objects.create(
            assignment=quiz,
            kind=Question.Kind.MCQ,
            text="I ___ my essay already, so I can relax now.",
            explanation="already + результат в настоящем → Present Perfect.",
            points=2,
            order=1,
        )
        Choice.objects.create(question=single, text="have finished", is_correct=True, order=1)
        Choice.objects.create(question=single, text="finished", order=2)
        Choice.objects.create(question=single, text="am finishing", order=3)

        multi = Question.objects.create(
            assignment=quiz,
            kind=Question.Kind.MULTI,
            text="Выберите маркеры Present Perfect.",
            points=4,
            order=2,
        )
        Choice.objects.create(question=multi, text="just", is_correct=True, order=1)
        Choice.objects.create(question=multi, text="so far", is_correct=True, order=2)
        Choice.objects.create(question=multi, text="yesterday", order=3)
        Choice.objects.create(question=multi, text="last week", order=4)

        gap = Question.objects.create(
            assignment=quiz,
            kind=Question.Kind.GAP,
            text="She ___ (live) in Moscow since 2019. Впишите форму глагола.",
            points=2,
            order=3,
        )
        Choice.objects.create(question=gap, text="has lived", is_correct=True, order=1)

        match = Question.objects.create(
            assignment=quiz,
            kind=Question.Kind.MATCH,
            text="Соотнесите фразовый глагол и значение.",
            points=2,
            order=4,
        )
        Choice.objects.create(
            question=match, text="look after", match_text="заботиться", is_correct=True, order=1
        )
        Choice.objects.create(
            question=match, text="set off", match_text="отправиться", is_correct=True, order=2
        )

        quiz.max_points = sum(question.points for question in quiz.questions.all())
        quiz.save(update_fields=["max_points", "updated_at"])

    def _deck(self, topic_obj):
        deck, created = FlashcardDeck.objects.get_or_create(
            topic=topic_obj,
            title=f"Карточки: {topic_obj.title}",
            defaults={"description": "Набор для интервального повторения.", "order": 1},
        )
        if created:
            pairs = [
                ("look after", "заботиться", "She looks after her little brother."),
                ("set off", "отправиться", "We set off early in the morning."),
                ("put off", "откладывать", "Don't put off your revision."),
                ("get along with", "ладить с", "I get along with my classmates."),
                ("come up with", "придумать", "He came up with a great idea."),
                ("keep up with", "успевать за", "It is hard to keep up with the schedule."),
            ]
            for order, (front, back, example) in enumerate(pairs, start=1):
                Flashcard.objects.create(
                    deck=deck, front=front, back=back, example=example, order=order
                )
        return deck

    # ── Учебная активность ──────────────────────────────────────────
    def _work(self, blocks, users):
        anna, maxim, sofia, teacher = (
            users["anna"],
            users["maxim"],
            users["sofia"],
            users["teacher"],
        )
        assignments = {
            title: Assignment.objects.get(title=title)
            for title in [
                "Тест: времена и маркеры",
                "Раскройте скобки",
                "Conditionals: 12 предложений",
                "Аудиоответ: моё путешествие",
                "IELTS Task 1: line graph",
            ]
        }

        # Автопроверенные тесты: у Максима идеальный результат, у Анны частичный.
        quiz = assignments["Тест: времена и маркеры"]
        for student, mistakes in [(maxim, 0), (anna, 1)]:
            if not Submission.objects.filter(student=student, assignment=quiz).exists():
                submit_quiz(
                    student=student,
                    assignment_id=quiz.pk,
                    expected_version=0,
                    answers=self._answers(quiz, mistakes=mistakes),
                )

        # Работа Анны на доработке (просроченный дедлайн).
        conditionals = assignments["Conditionals: 12 предложений"]
        submission = self._submitted(
            anna,
            conditionals,
            "If I will have time, I will do the third type.",
        )
        if submission and submission.status != Submission.Status.CHECKED:
            review_submission(
                teacher=teacher,
                submission_id=submission.pk,
                expected_version=submission.version,
                expected_review_revision=submission.review_revision,
                grade=62,
                comment=(
                    "Хорошая попытка, но в условных предложениях первого типа после if "
                    "используем Present Simple: If I have time, I will do it. "
                    "Пересоберите 3-е предложение и пришлите новую попытку."
                ),
                decision="needs_revision",
            )

        # Проверенный аудиоответ Максима.
        speaking = assignments["Аудиоответ: моё путешествие"]
        submission = self._submitted(
            maxim,
            speaking,
            "Last summer I went to Sochi with my family...",
            file_answer=SimpleUploadedFile(
                "maxim-trip.wav", wav_silence(), content_type="audio/wav"
            ),
        )
        if submission and submission.status != Submission.Status.CHECKED:
            review_submission(
                teacher=teacher,
                submission_id=submission.pk,
                expected_version=submission.version,
                expected_review_revision=submission.review_revision,
                grade=39,
                comment=(
                    "Беглость хорошая, темп уверенный. Добавьте связки (however, as a result) "
                    "и следите за артиклями: a trip, the sea. Лексика темы раскрыта."
                ),
                decision="checked",
            )

        # Работа Софии ждёт проверки — видна в очереди преподавателя.
        task1 = assignments["IELTS Task 1: line graph"]
        self._submitted(
            sofia,
            task1,
            "The line graph illustrates the number of visitors to three museums "
            "in London between 2010 and 2020. Overall, the figures rose...",
        )

        # Черновик Анны: автосохранение ответа.
        gaps = assignments["Раскройте скобки"]
        save_answer_draft(
            student=anna,
            assignment_id=gaps.pk,
            text="1. I have just (finish) my homework. 2. She ...",
        )

        # Повторения карточек: у Анны часть карточек уже изучена.
        deck = FlashcardDeck.objects.filter(title="Карточки: Travel vocabulary").first()
        if deck:
            for card, rating in list(zip(deck.cards.all()[:3], ["good", "easy", "again"])):
                review_flashcard(student=anna, card_id=card.pk, rating=RATING_CHOICES[rating])

    def _submitted(self, student, assignment, text, file_answer=None):
        """Отправить работу через сервис, чтобы сохранить инварианты и историю."""
        existing = (
            Submission.objects.filter(student=student, assignment=assignment)
            .order_by("-version")
            .first()
        )
        if existing:
            return existing
        return submit_assignment(
            student=student,
            assignment_id=assignment.pk,
            expected_version=0,
            text_answer=text,
            file_answer=file_answer,
        )

    def _answers(self, quiz, *, mistakes=0):
        """Ответы теста в формате сервиса: {str(question_id): значение}.

        mistakes=1 намеренно портит первый одиночный выбор, чтобы в аналитике
        был виден частичный результат, а не только идеальная сдача.
        """
        answers = {}
        spoiled = False
        for question in quiz.questions.prefetch_related("choices"):
            choices = list(question.choices.all())
            correct = [choice for choice in choices if choice.is_correct]
            if not correct:
                continue
            if question.kind == Question.Kind.MULTI:
                value = [str(choice.pk) for choice in correct]
            elif question.kind == Question.Kind.MATCH:
                value = {str(choice.pk): str(choice.pk) for choice in correct}
            elif question.kind == Question.Kind.GAP:
                value = correct[0].text
            else:
                wrong = [choice for choice in choices if not choice.is_correct]
                if mistakes and wrong and not spoiled:
                    value = str(wrong[0].pk)
                    spoiled = True
                else:
                    value = str(correct[0].pk)
            answers[str(question.pk)] = value
        return answers

    def _snippets(self, teacher):
        bank = [
            ("Структура эссе", "essay-structure", "Соберите план: тезис → 2 аргумента → вывод."),
            (
                "Артикли",
                "articles",
                "Проверьте артикли: a/an для первого упоминания, the для известного.",
            ),
            (
                "Связки",
                "linkers",
                "Добавьте связки: however, moreover, as a result — это поднимает cohesion.",
            ),
            ("Времена", "tenses", "Согласуйте времена: маркер since требует Present Perfect."),
        ]
        for title, code, text in bank:
            CommentSnippet.objects.get_or_create(
                author=teacher,
                code=code,
                defaults={"title": title, "text": text, "is_shared": True},
            )
