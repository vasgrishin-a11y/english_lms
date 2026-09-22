from django import template
from django.utils import timezone
from django.utils.html import format_html

from lms.models import Submission

register = template.Library()

STATUS_LABELS = dict(Submission.Status.choices)
STATUS_ICONS = {
    Submission.Status.NEW: "circle-dashed",
    Submission.Status.SUBMITTED: "clock",
    Submission.Status.IN_REVIEW: "eye",
    Submission.Status.NEEDS_REVISION: "alert",
    Submission.Status.CHECKED: "check-circle",
}
TYPE_ICONS = {
    "text": "file-text",
    "file": "file-text",
    "audio": "mic",
    "mixed": "layers",
    "quiz": "target",
    "flashcards": "cards",
}

COVER_THEMES = {
    "travel": {"emoji": "🧳", "label": "Путешествия"},
    "movie": {"emoji": "🎬", "label": "Кино"},
    "business": {"emoji": "💼", "label": "Бизнес"},
    "exam": {"emoji": "📝", "label": "Экзамены"},
    "it": {"emoji": "💻", "label": "IT"},
    "kids": {"emoji": "🧒", "label": "Детям"},
    "teens": {"emoji": "🎧", "label": "Подросткам"},
    "ielts": {"emoji": "🎓", "label": "IELTS"},
    "happy": {"emoji": "🥽", "label": "Марафон"},
    "grammar": {"emoji": "📚", "label": "Грамматика"},
    "general": {"emoji": "🌍", "label": "Общий курс"},
}

# Ключевые слова названия → тема обложки. Порядок важен: первые совпадения точнее.
COVER_KEYWORDS = [
    (
        ("travel", "travell", "путешеств", "scandinav", "denmark", "hotel", "airport", "trip"),
        "travel",
    ),
    (("movie", "cinema", "film", "кино", "фильм"), "movie"),
    (("business", "делов", "бизнес", "finance", "market", "meeting", "negotiat"), "business"),
    (("exam", "огэ", "егэ", "ielts", "test prep", "экзамен"), "exam"),
    (("ielts",), "ielts"),
    (("it ", " it", "python", "code", "agile", "scrum", "develop", "разработ"), "it"),
    (("kid", "child", "дет", "ребён", "ребен"), "kids"),
    (("teen", "подрост"), "teens"),
    (("happy", "marathon", "марафон"), "happy"),
    (("grammar", "грамматик", "tenses", "passive", "conditional"), "grammar"),
    (("vocab", "словар", "слов", "dictionary", "words", "word"), "grammar"),
]
SKILL_ICONS = {
    "grammar": "book",
    "vocabulary": "cards",
    "listening": "headphones",
    "speaking": "mic",
    "writing": "pencil",
    "reading": "eye",
}
KIND_ICONS = {
    "mcq": "target",
    "multi": "grid",
    "gap": "pencil",
    "match": "sort",
    "order": "list-ordered",
    "sort": "columns",
    "spell": "shuffle",
}
KIND_HINTS = {
    "mcq": "один правильный ответ",
    "multi": "несколько правильных ответов",
    "gap": "впишите ответ",
    "match": "установите соответствие",
    "order": "соберите предложение из слов",
    "sort": "распределите по колонкам",
    "spell": "соберите слово из букв",
}


@register.filter
def status_label(value):
    if not value:
        return "Не отправлено"
    return STATUS_LABELS.get(value, "Не отправлено")


@register.filter
def status_icon(value):
    return STATUS_ICONS.get(value, "circle-dashed")


@register.filter
def type_icon(value):
    return TYPE_ICONS.get(value, "file-text")


@register.filter
def skill_icon(kind):
    return SKILL_ICONS.get(kind, "tag")


@register.filter
def kind_icon(kind):
    return KIND_ICONS.get(kind, "target")


@register.filter
def kind_hint(kind):
    return KIND_HINTS.get(kind, "")


@register.simple_tag
def icon(name, css="icon", title=""):
    """Инлайн-иконка из спрайта: без шрифтов, без внешних запросов, без mark_safe."""
    href = f"#i-{name}"
    if title:
        return format_html(
            '<svg class="{}" role="img" aria-label="{}" focusable="false">'
            '<use href="{}"></use></svg>',
            css,
            title,
            href,
        )
    return format_html(
        '<svg class="{}" aria-hidden="true" focusable="false"><use href="{}"></use></svg>',
        css,
        href,
    )


@register.filter
def initials(user):
    name = (user.get_full_name() or user.username or "?").strip()
    parts = [part for part in name.replace(".", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2]
    return f"{parts[0][0]}{parts[1][0]}"


@register.filter
def grade_class(grade, maximum=None):
    if grade is None:
        return "is-empty"
    try:
        maximum = float(maximum or 0)
    except (TypeError, ValueError):
        maximum = 0
    if maximum <= 0:
        return ""
    ratio = float(grade) / maximum
    if ratio < 0.5:
        return "is-low"
    if ratio < 0.75:
        return "is-mid"
    return ""


@register.filter
def human_due(value):
    """Понятный срок: «сегодня, 18:00», «завтра, 09:00», «просрочено на 2 ч»."""
    if not value:
        return ""
    now = timezone.localtime()
    local = timezone.localtime(value)
    delta = local - now
    seconds = delta.total_seconds()
    stamp = local.strftime("%d.%m.%Y %H:%M")
    if seconds <= 0:
        return f"просрочено на {human_hours(delta)}"
    days = int(seconds // 86400)
    if days == 0:
        return f"сегодня, {local.strftime('%H:%M')}"
    if days == 1:
        return f"завтра, {local.strftime('%H:%M')}"
    if days < 7:
        return f"через {days} дн. ({stamp})"
    return stamp


def human_hours(delta):
    hours = int(abs(delta.total_seconds()) // 3600)
    if hours < 1:
        minutes = max(1, int(abs(delta.total_seconds()) // 60))
        return f"{minutes} мин"
    if hours < 48:
        return f"{hours} ч"
    return f"{hours // 24} дн."


@register.filter
def due_state(value):
    """Класс срочности для карточки задания."""
    if not value:
        return "none"
    now = timezone.localtime()
    local = timezone.localtime(value)
    delta = (local - now).total_seconds()
    if delta <= 0:
        return "late"
    if delta <= 86400:
        return "today"
    if delta <= 3 * 86400:
        return "soon"
    return "later"


@register.filter
def percent(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


@register.filter
def compact_number(value):
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}".replace(".", ",")


@register.filter
def multiply(value, factor):
    try:
        return float(value) * float(factor)
    except (TypeError, ValueError):
        return 0


@register.simple_tag(takes_context=True)
def querystring_replace(context, **kwargs):
    """Ссылка с заменой части GET-параметров (страница сбрасывается при смене фильтра)."""
    request = context.get("request")
    if request is None:
        return ""
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = value
    if "page" not in kwargs:
        params.pop("page", None)
    query = params.urlencode()
    return f"?{query}" if query else "?"


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".ogg")


@register.filter
def media_kind(name):
    """Тип файла для превью: image, audio или other. Превью разрешено не для всего."""
    lowered = str(name or "").lower()
    if lowered.endswith(IMAGE_SUFFIXES):
        return "image"
    if lowered.endswith(AUDIO_SUFFIXES):
        return "audio"
    return "other"


@register.filter
def dictkey(mapping, key):
    """Значение словаря по ключу-числу: разбор автопроверки хранится с строковыми ключами."""
    if not isinstance(mapping, dict):
        return None
    if str(key) in mapping:
        return mapping[str(key)]
    return mapping.get(key)


@register.filter
def answered(mapping, key):
    detail = dictkey(mapping, key)
    if not isinstance(detail, dict):
        return ""
    given = detail.get("given")
    if isinstance(given, list):
        return ", ".join(str(item) for item in given)
    if isinstance(given, dict):
        return ", ".join(f"{value}" for value in given.values())
    return "" if given is None else str(given)


@register.filter
def cover_theme(name):
    """Тема обложки курса по названию: travel, movie, business… или general."""
    lowered = str(name or "").lower()
    for keywords, theme in COVER_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            # IELTS точнее экзамена: проверяем его раньше общего exam-правила.
            if theme == "exam" and "ielts" in lowered:
                return "ielts"
            return theme
    return "general"


@register.filter
def cover_emoji(theme_or_name):
    """Эмодзи обложки: принимает и тему, и произвольное название курса."""
    key = str(theme_or_name or "").strip().lower()
    if key in COVER_THEMES:
        return COVER_THEMES[key]["emoji"]
    return COVER_THEMES[cover_theme(theme_or_name)]["emoji"]


@register.filter
def cover_label(theme_or_name):
    key = str(theme_or_name or "").strip().lower()
    if key in COVER_THEMES:
        return COVER_THEMES[key]["label"]
    return COVER_THEMES[cover_theme(theme_or_name)]["label"]


@register.filter
def cover_variant(name):
    """Детерминированный номер градиента 1–8 по названию курса.

    Свои курсы получают стабильный цвет: одно и то же название всегда
    выглядит одинаково, разные названия — чаще всего по-разному.
    """
    text = str(name or "")
    total = sum(ord(char) for char in text) + len(text) * 7
    return (total % 8) + 1


@register.filter
def cover_initials(name):
    """Крупные инициалы для обложки своего курса: первые буквы двух слов."""
    words = [word for word in str(name or "").replace("·", " ").split() if word[:1].isalnum()]
    if not words:
        return "EL"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


@register.filter
def card_presets_map(presets):
    """Текст «лицо | оборот | пример» по строкам для каждого шаблона карточек.

    Отдаётся в ``json_script`` на странице карточек: кнопка «Подставить
    в импорт» заполняет textarea массового импорта без перезагрузки.
    """

    def line(card):
        parts = [card.get("front", ""), card.get("back", "")]
        if card.get("example"):
            parts.append(card["example"])
        return " | ".join(parts)

    return {
        preset["id"]: "\n".join(line(card) for card in preset.get("cards", []))
        for preset in presets or []
        if preset.get("id")
    }


@register.filter
def get_item(mapping, key):
    """Безопасный доступ к словарю в шаблоне: наборы карточек темы, счётчики."""
    if not mapping:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def percent_of(value, maximum):
    try:
        value = float(value)
        maximum = float(maximum)
    except (TypeError, ValueError):
        return 0
    if maximum <= 0:
        return 0
    return int(round(100 * value / maximum))
