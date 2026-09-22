"""Проверка настроек ИИ-помощника на площадке (локальная модель).

Команда отвечает на два вопроса администратора: «что сейчас настроено» и
«работает ли ключ». Без ``--live`` она не делает ни одного сетевого запроса,
поэтому её безопасно запускать на production перед включением помощника::

    python manage.py ai_check            # настройки и режим
    python manage.py ai_check --live     # пробный разбор текста провайдером

Содержимое ответа модели не печатается целиком — только счётчики структуры,
как и в журнале (``lms.ai``): в логи не должны попадать материалы и данные.
"""

from django.core.management.base import BaseCommand, CommandError

from lms import ai

SAMPLE = """# Travel A2
## At the airport
### Check-in [quiz]
Прочитайте диалог и ответьте на вопросы.
? What do you show at the check-in desk?
* passport
- luggage
### Слова темы [карточки]
- to book | бронировать | We booked a table.
- itinerary | маршрут
"""


class Command(BaseCommand):
    help = "Показать настройки ИИ-помощника и (по флагу --live) проверить ключ провайдера."

    def add_arguments(self, parser):
        parser.add_argument(
            "--live",
            action="store_true",
            help="Отправить пробный материал провайдеру (один запрос, без импорта черновиков).",
        )

    def handle(self, *args, **options):
        spec = ai.provider_spec()
        mode = ai.ai_mode()
        self.stdout.write(f"Режим: {mode} — {ai.ai_mode_label(mode)}")
        self.stdout.write(f"Провайдер: {spec['key']} ({spec['label']}), адаптер: {spec['kind']}")
        self.stdout.write(f"Модель: {ai.ai_model()} · адрес: {ai.ai_endpoint() or 'не задан'}")
        self.stdout.write(f"Вложения: {', '.join(ai.provider_uploads()) or 'нет'}")
        if spec.get("hint"):
            self.stdout.write(f"Подсказка: {spec['hint']}")

        if mode == "off":
            raise CommandError("Помощник выключен (LMS_AI_ENABLED=0) — проверять нечего.")
        if mode == "offline":
            self.stdout.write(
                self.style.WARNING(
                    "Локальная модель не включена: помощник работает офлайн-эвристиками. "
                    "Включить: LMS_AI_LOCAL=1 (или задать LMS_AI_API_KEY для своего шлюза)."
                )
            )
            return
        if not options["live"]:
            self.stdout.write(
                "Модель включена. Чтобы проверить её живым запросом, запустите с флагом --live."
            )
            return

        if ai.ai_endpoint().startswith("http://") and not ai.provider_spec().get("keyless"):
            self.stdout.write(
                self.style.WARNING(
                    "Адрес модели без HTTPS: подойдёт только для доверенной локальной сети."
                )
            )

        # Только один пробный запрос: реальные материалы и работы учеников сюда не попадают.
        try:
            material, meta = ai.build_material(text=SAMPLE, target="mixed")
        except ai.AiError as exc:
            raise CommandError(f"Провайдер недоступен: {exc}") from exc

        if meta.get("result") != "online":
            for note in meta.get("notes") or []:
                self.stdout.write(self.style.WARNING(f"Замечание: {note}"))
            raise CommandError("Онлайн-разбор не сработал: ответа модели нет.")
        summary = ai.material_summary(material)
        self.stdout.write(
            self.style.SUCCESS(
                "Модель ответила. Черновик: блоков {blocks}, тем {topics}, заданий "
                "{assignments}, вопросов {questions}, карточек {cards}. Импорт не выполнялся.".format(
                    **summary
                )
            )
        )
