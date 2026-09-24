"""Двуязычный интерфейс: русские подписи меню и действий получают английские пары.

Учебное содержимое остаётся английским в любом режиме, а интерфейс переводится
ровно там, где это просили: названия меню и функциональных действий. Словарь
ключуется русской подписью из шаблона, поэтому ``{% t "Консоль" %}`` всегда
работает: неизвестная строка просто остаётся как есть, без падений и пустых мест.
"""

from __future__ import annotations

#: Языки интерфейса в порядке переключателя: русский по умолчанию, первым.
LANGUAGES = (
    ("ru", "RU"),
    ("eng", "ENG"),
)
DEFAULT_LANGUAGE = "ru"
COOKIE_NAME = "lms_lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

#: Английские пары для подписей меню и действий. Значение — то, что вернёт
#: ``{% t %}`` в режиме ENG; в режиме RU возвращается сам ключ.
TEXTS_EN = {
    # Пошаговая проверка пунктов.
    "Отправить преподавателю": "Send to teacher",
    "Пройти заново": "Start over",
    "Результаты по пунктам": "Results by item",
    # Меню и навигация.
    "Консоль": "Console",
    "Проверка": "Review",
    "Курс": "Course",
    "ИИ-помощник": "AI assistant",
    "Архив": "Archive",
    "Ученики": "Students",
    "Группы": "Groups",
    "Аналитика": "Analytics",
    "Настройки": "Settings",
    "Главная": "Home",
    "Тренажёр": "Trainer",
    "Мой словарь": "My dictionary",
    "Оценки": "Grades",
    "Сроки": "Deadlines",
    "Меню": "Menu",
    "Свернуть меню": "Collapse menu",
    "Развернуть меню": "Expand menu",
    "Основная навигация": "Main navigation",
    "Навигация на маленьком экране": "Compact navigation",
    "Перейти к содержимому": "Skip to content",
    "Язык интерфейса": "Interface language",
    "Войти": "Sign in",
    "Выйти": "Sign out",
    "Карта курса": "Curriculum map",
    "К карте курса": "To curriculum map",
    "К заданиям": "To assignments",
    "К курсу": "To course",
    "К очереди": "To review queue",
    "Библиотека": "Library",
    "Из библиотеки": "From library",
    "Журнал": "Gradebook",
    "Карточки": "Flashcards",
    "Задание": "Assignment",
    "Блок": "Block",
    "Тема": "Topic",
    "Вопросы": "Questions",
    "Вернуться в кабинет": "Back to dashboard",
    "Вернуться к курсу": "Back to course",
    "Вернуться ко входу": "Back to sign in",
    # Действия.
    "Создать блок": "Create block",
    "Новый блок": "New block",
    "Создать задание": "Create assignment",
    "Новое задание": "New assignment",
    "Добавить задание": "Add assignment",
    "Добавить тему": "Add topic",
    "Добавить вопрос": "Add question",
    "Добавить карточку": "Add card",
    "Добавить в курс": "Add to course",
    "Добавить сразу": "Add now",
    "Сохранить": "Save",
    "Сохранить блок": "Save block",
    "Сохранить тему": "Save topic",
    "Сохранить вопрос": "Save question",
    "Сохранить группу": "Save group",
    "Сохранить настройки": "Save settings",
    "Сохранить профиль": "Save profile",
    "Сохранить проверку": "Save review",
    "Сохранить шаблон": "Save template",
    "Сохранить и к следующей": "Save and go to next",
    "Сохранить тест и перейти к вопросам": "Save quiz and open questions",
    "Сохранить новый пароль": "Save new password",
    "Отмена": "Cancel",
    "Сбросить": "Reset",
    "Сбросить пароль": "Reset password",
    "Сменить пароль": "Change password",
    "Найти": "Search",
    "Открыть": "Open",
    "Открыть вопросы отдельно": "Open questions separately",
    "Открыть как ученик": "Open as student",
    "Открыть карту": "Open map",
    "Открыть последнюю попытку": "Open last attempt",
    "Показать": "Show",
    "Показать все задания": "Show all assignments",
    "Посмотреть карту курса": "View curriculum map",
    "Предпросмотр глазами ученика": "Student preview",
    "Предпросмотр с ответами": "Preview with answers",
    "Редактировать": "Edit",
    "Редактировать профиль": "Edit profile",
    "Изменить": "Change",
    "Изменить задание": "Edit assignment",
    "Изменить тему": "Edit topic",
    "Настройки задания": "Assignment settings",
    "Дублировать": "Duplicate",
    "Создать копию": "Duplicate",
    "Создать первый шаблон": "Create first template",
    "Новый шаблон": "New template",
    "Удалить": "Delete",
    "Удалить всё": "Delete all",
    "Удалить блок целиком": "Delete block with contents",
    "Удалить тему целиком": "Delete topic with contents",
    "Удалить задание": "Delete assignment",
    "Удалить задание навсегда": "Delete assignment permanently",
    "Удалить навсегда": "Delete permanently",
    "Удалить вопрос": "Delete question",
    "Удалить ученика": "Delete student",
    "Удалить шаблон": "Delete template",
    "Архивировать блок": "Archive block",
    "Архивировать тему": "Archive topic",
    "Архивировать задание": "Archive assignment",
    "В архив": "To archive",
    "Восстановить": "Restore",
    "В импорт": "To import",
    "В словарь": "To dictionary",
    "Импортировать список": "Import list",
    "Импортировать черновиками": "Import as drafts",
    "Опубликовать": "Publish",
    # Быстрые действия на карте курса и правка задания с ИИ.
    "В черновики": "To drafts",
    "Опубликовать всё": "Publish all",
    "В черновики всё": "All to drafts",
    "Опубликовать все задания темы": "Publish all assignments in topic",
    "Все задания темы в черновики": "Move all assignments in topic to drafts",
    "Переименовать": "Rename",
    "Перетащить задание": "Drag to reorder assignment",
    "Перетащить тему": "Drag to reorder topic",
    "Выше": "Move up",
    "Ниже": "Move down",
    "Править с ИИ": "Edit with AI",
    "Правка с ИИ": "AI-assisted edit",
    "Подготовить версию": "Draft a version",
    "Сгенерировать заново": "Regenerate",
    "Сбросить версию": "Discard version",
    "Применить правки": "Apply changes",
    "Назад к заданию": "Back to assignment",
    "Текущая версия глазами ученика": "Current version as students see it",
    "Открыть задание": "Open assignment",
    "Выгрузить XLSX": "Download XLSX",
    "Скачать материалы": "Download materials",
    "Скачать файл ответа": "Download answer file",
    "Собрать материал": "Build material",
    "Собрать материал для темы": "Build material for topic",
    "Собрать блок из материала": "Build block from material",
    "Собрать блок из файла": "Build block from file",
    "Собрать задание из материала": "Build assignment from material",
    "Собрать тест с вопросами": "Build quiz with questions",
    "Очистить результат": "Clear result",
    "Отправить на проверку": "Submit for review",
    "Отправить и проверить": "Submit and check",
    "Тренировать": "Practice",
    "Тренировать словарь": "Practice vocabulary",
    "Все наборы": "All sets",
    "К списку наборов": "To set list",
    "Вся очередь": "Whole queue",
    "Все оценки": "All grades",
    "Все сроки": "All deadlines",
    "Взять готовый курс": "Use a ready-made course",
    "Карточка ученика": "Student card",
    "Работы ученика": "Student's work",
    "Работы учеников": "Students' work",
    "Работы этого задания": "Work for this assignment",
    "Новая сессия": "New session",
    "Обновить": "Refresh",
    "Назад": "Back",
    "Далее": "Next",
    "Готово": "Done",
    "Выбрать файл": "Choose a file",
    "Убрать файл": "Remove file",
    "Файл не выбран": "No file selected",
    "Легко": "Easy",
    "С трудом": "Hard",
    "Помню": "Remembered",
    "Не помню": "Forgotten",
    # Курс целиком и счётчики
    "Курс целиком": "Whole course",
    "Классов": "Classes",
    "Тем": "Topics",
    "Заданий": "Assignments",
    "С карточками": "With flashcards",
    "Черновиков": "Drafts",
    "Ждут проверки": "Awaiting review",
    "Классов:": "Classes:",
    "Тем:": "Topics:",
    "Заданий:": "Assignments:",
    "С карточками:": "With flashcards:",
    "Черновиков:": "Drafts:",
    "Ждут проверки:": "Awaiting review:",
}


def normalize(value):
    """Привести код языка к известному значению или вернуть пустую строку."""
    candidate = (value or "").strip().lower()
    if candidate in ("ru", "rus"):
        return "ru"
    if candidate in ("eng", "en", "english"):
        return "eng"
    return ""


def resolve(get_value):
    """Выбрать язык: параметр ``?lang=``, затем cookie, затем русский по умолчанию."""
    return normalize(get_value("lang")) or normalize(get_value("lms_lang")) or DEFAULT_LANGUAGE


def translate(text, language):
    """Перевести подпись на английский; в RU и для незнакомых строк — как есть."""
    if language == "eng":
        return TEXTS_EN.get(str(text), str(text))
    return str(text)


__all__ = [
    "COOKIE_MAX_AGE",
    "COOKIE_NAME",
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "TEXTS_EN",
    "normalize",
    "resolve",
    "translate",
]
