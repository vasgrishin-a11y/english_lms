"""Библиотека готового контента преподавателя английского.

Три уровня заготовок — по числу уровней иерархии курса:

* ``ASSIGNMENT_PRESETS`` — шаблоны заданий (форма «Новое задание»);
* ``BLOCK_SUGGESTIONS`` / ``TOPIC_SUGGESTIONS`` — готовые формулировки блоков
  и тем, подставляются в соответствующие формы;
* ``COURSE_PACKS`` — собранные курсы «блоки → темы → задания (+ вопросы теста
  и карточки)», добавляются в курс одним действием через ``import_course_pack``.

Контент живёт в коде, а не в базе: его можно читать в ревью, версионировать и
переиспользовать в новых развёртываниях. Импортированные задания создаются
черновиками — публикацию преподаватель подтверждает осознанно.
"""

from django.db import transaction
from django.utils.text import slugify

from .models import Assignment, Block, Choice, Flashcard, FlashcardDeck, Question, Skill, Topic

# ── Шаблоны заданий ────────────────────────────────────────────────────────
# Подставляются в форму «Новое задание» целиком: заголовок, условия, тип сдачи
# и разумный максимум баллов. ``skills`` — слаги навыков для автоотметки.
ASSIGNMENT_PRESETS = [
    {
        "id": "gap-fill",
        "icon": "pencil",
        "label": "Fill in the Blanks",
        "tagline": "Грамматика: пропуски с маркерами времени",
        "fields": {
            "title": "Fill in the Blanks: Present Perfect vs Past Simple",
            "description": (
                "Complete the sentences with the verb in brackets in the correct "
                "tense (Present Perfect or Past Simple).\n\n"
                "1. I ___ (already / finish) my homework, so I can go out now.\n"
                "2. We ___ (visit) the British Museum last summer.\n"
                "3. She ___ (live) in London since 2019.\n"
                "4. They ___ (not / see) each other for three years.\n"
                "5. ___ you ever ___ (try) haggis?\n\n"
                "Pay attention to time markers: already, yet, since, for, "
                "last summer, three years ago.\n\n"
                "Marking: 1 point per sentence. Write the full verb form, "
                "not only the auxiliary."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["grammar"],
    },
    {
        "id": "transformation",
        "icon": "refresh",
        "label": "Key Word Transformation",
        "tagline": "B1–B2: перефразирование с ключевым словом",
        "fields": {
            "title": "Key Word Transformations (2–5 words)",
            "description": (
                "Complete the second sentence so that it means the same as the "
                "first. Use the key word in CAPITALS. Do not change the key word. "
                "Use between TWO and FIVE words including the key word.\n\n"
                "1. I'm sure it wasn't Maria who called you.\n"
                "   It ___ ___ ___ ___ ___ (CAN'T)\n"
                "2. 'Where do you live?' she asked me.\n"
                "   She asked me where ___ ___ ___ (I)\n"
                "3. They started learning English five years ago.\n"
                "   They ___ ___ ___ for five years (BEEN)\n"
                "4. The film was so boring that we left the cinema.\n"
                "   The film was ___ ___ ___ we left the cinema (SUCH)\n\n"
                "Marking: 2 points per item, all or nothing. Spelling counts."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["grammar", "vocabulary"],
    },
    {
        "id": "error-correction",
        "icon": "search",
        "label": "Find & Correct Mistakes",
        "tagline": "Поиск ошибки + объяснение правила",
        "fields": {
            "title": "Find and Correct the Mistakes",
            "description": (
                "Each sentence contains ONE mistake. Rewrite the sentence "
                "correctly and explain the rule in one line (in Russian or "
                "English).\n\n"
                "1. She don't like getting up early.\n"
                "2. I have visited Paris last month.\n"
                "3. He is married with a doctor.\n"
                "4. We enjoyed a lot at the party.\n"
                "5. If I will have time, I'll call you.\n\n"
                "Marking: 2 points per sentence — 1 for the correct sentence, "
                "1 for the explanation. Do not rewrite the sentences that are "
                "already correct; mark them with a tick."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["grammar"],
    },
    {
        "id": "essay-rubric",
        "icon": "file-text",
        "label": "Essay with Rubric",
        "tagline": "Мнение + шкала из 4 критериев",
        "fields": {
            "title": "Opinion Essay: Should schools ban mobile phones?",
            "description": (
                "Write an opinion essay of 200–250 words.\n\n"
                "Structure: introduction with a clear thesis → 2 body "
                "paragraphs with arguments and examples → a counter-argument "
                "and your response → conclusion.\n\n"
                "Use at least 5 linking words (however, therefore, moreover, "
                "on the other hand, as a result) and avoid repeating the same "
                "opinion verb.\n\n"
                "Marking (5 points per criterion, 20 total):\n"
                "• Task Achievement — position is clear and developed;\n"
                "• Coherence & Cohesion — logical paragraphs and linking;\n"
                "• Lexical Resource — range and accuracy of vocabulary;\n"
                "• Grammatical Range & Accuracy — variety of structures."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["writing"],
    },
    {
        "id": "listening",
        "icon": "headphones",
        "label": "Listening & Dictation",
        "tagline": "Аудирование + пословный диктант",
        "fields": {
            "title": "Listening: comprehension + dictation",
            "description": (
                "Part 1 — Comprehension. Listen to the recording (attached) "
                "twice and answer the questions in full sentences:\n"
                "1. Where does the speaker usually work from?\n"
                "2. What are two advantages of this arrangement?\n"
                "3. What problem does the speaker mention?\n\n"
                "Part 2 — Dictation. Listen to the final paragraph and write it "
                "down word for word. You may listen up to four times. Punctuation "
                "and capital letters count.\n\n"
                "Marking: 10 points for comprehension (3 + 4 + 3), 10 points for "
                "the dictation (0.5 per correct word)."
            ),
            "assignment_type": "mixed",
            "max_points": 20,
        },
        "skills": ["listening", "writing"],
    },
    {
        "id": "speaking",
        "icon": "mic",
        "label": "Speaking Response",
        "tagline": "Устный ответ 1.5–2 минуты",
        "fields": {
            "title": "Speaking: describe a memorable trip (1.5–2 min)",
            "description": (
                "Record a 1.5–2 minute answer and upload the audio file.\n\n"
                "Cover the following:\n"
                "• where you went and who with;\n"
                "• what you did there (use at least two past tenses);\n"
                "• why you remember this trip;\n"
                "• whether you would like to go back and why.\n\n"
                "Tips: signpost your answer (First of all…, What I remember "
                "most is…, On the whole…), pause between ideas instead of "
                "filling the gaps with 'eee'.\n\n"
                "Marking: Fluency 5, Vocabulary 5, Grammar 5, Pronunciation 5."
            ),
            "assignment_type": "audio",
            "max_points": 20,
        },
        "skills": ["speaking"],
    },
    {
        "id": "reading",
        "icon": "book",
        "label": "Reading Comprehension",
        "tagline": "Текст + вопросы True/False/Not Stated",
        "fields": {
            "title": "Reading: True / False / Not Stated",
            "description": (
                "Read the text (attached or pasted below) and mark each "
                "statement as True, False or Not Stated. Justify every False "
                "answer with a quote from the text.\n\n"
                "1. …\n2. …\n3. …\n4. …\n5. …\n6. …\n\n"
                "Then answer in 3–4 sentences: what is the author's attitude "
                "to the problem and which sentence shows it best?\n\n"
                "Marking: 1 point per statement, 4 points for the attitude "
                "answer. A justification is required for 'False'."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["reading"],
    },
    {
        "id": "vocabulary",
        "icon": "cards",
        "label": "Vocabulary Set",
        "tagline": "Словообразование и коллокации + карточки",
        "fields": {
            "title": "Vocabulary: word formation and collocations",
            "description": (
                "Part 1 — Word formation. Complete the table with the missing "
                "word forms (noun / verb / adjective / adverb):\n"
                "decide · ___ · ___ · ___\n"
                "___ · to succeed · ___ · ___\n"
                "employ · ___ · ___ · ___\n\n"
                "Part 2 — Collocations. Choose the correct verb: "
                "(do / make) a decision, (do / make) progress, "
                "(take / give) responsibility, (pay / give) attention.\n\n"
                "Part 3 — Use each new word in your own sentence about your "
                "studies.\n\n"
                "Marking: 12 points for the table, 4 for collocations, "
                "4 for your sentences. Repeat the set in the trainer "
                "(flashcards) before submitting."
            ),
            "assignment_type": "text",
            "max_points": 20,
        },
        "skills": ["vocabulary"],
    },
    {
        "id": "quiz",
        "icon": "target",
        "label": "Автопроверяемый тест",
        "tagline": "10 вопросов: выбор, пропуск, соответствие",
        "fields": {
            "title": "Grammar & Vocabulary Quiz (auto-checked)",
            "description": (
                "Ten questions, one attempt is checked automatically.\n"
                "Types: multiple choice, gap fill (one word), matching.\n"
                "You can see your result immediately after submitting.\n\n"
                "Tip for the teacher: add questions on the next screen — "
                "the maximum score is calculated from the question points "
                "automatically."
            ),
            "assignment_type": "quiz",
            "max_points": 10,
        },
        "skills": ["grammar", "vocabulary"],
    },
    {
        "id": "order",
        "icon": "list-ordered",
        "label": "Sentence Builder",
        "tagline": "Соберите предложение из слов — автопроверка порядка",
        "fields": {
            "title": "Sentence Builder: put the words in the correct order",
            "description": (
                "Put the words in the correct order to make a sentence. "
                "Click the words or drag them — like in ProgressMe / Wordwall.\n\n"
                "1. never / I / have / to / been / London\n"
                "2. if / you / study / you / hard / will / pass / the exam\n"
                "3. was / while / I / cooking / he / called\n\n"
                "Marking: automatic — 1 point if the whole sentence is in the right order, "
                "partial credit for words in the correct position."
            ),
            "assignment_type": "quiz",
            "max_points": 10,
        },
        "skills": ["grammar"],
    },
    {
        "id": "sort",
        "icon": "columns",
        "label": "Sort into Columns",
        "tagline": "Распределите слова по колонкам — автопроверка",
        "fields": {
            "title": "Sort the words: countable / uncountable / both",
            "description": (
                "Drag each word into the correct column. "
                "ProgressMe-style exercise: term → column.\n\n"
                "Columns: Countable, Uncountable, Both\n"
                "Words: advice, chair, information, apple, money, child, furniture, job\n\n"
                "Marking: automatic — 1 point per correctly placed word."
            ),
            "assignment_type": "quiz",
            "max_points": 10,
        },
        "skills": ["vocabulary", "grammar"],
    },
    {
        "id": "spell",
        "icon": "shuffle",
        "label": "Word from Letters",
        "tagline": "Анаграмма: соберите слово из букв",
        "fields": {
            "title": "Anagram: make a word from the letters",
            "description": (
                "Make a word from the given letters. Click the tiles or type the word.\n\n"
                "Example: letters 'b o o k' → 'book'. "
                "Accepts several variants (e.g. colour/color).\n\n"
                "Marking: automatic, case-insensitive, ignores punctuation."
            ),
            "assignment_type": "quiz",
            "max_points": 10,
        },
        "skills": ["vocabulary"],
    },
    {
        "id": "project",
        "icon": "layers",
        "label": "Mini-project",
        "tagline": "Групповой проект + презентация",
        "fields": {
            "title": "Mini-project: present your idea in 3 slides",
            "description": (
                "In pairs or individually, prepare a 3-slide presentation "
                "(attach the file) and a 150-word summary in the text field.\n\n"
                "Slide 1 — the problem and why it matters;\n"
                "Slide 2 — your solution with one concrete example;\n"
                "Slide 3 — what you need to start (resources, time, people).\n\n"
                "Speak for 2–3 minutes if you record the presentation.\n\n"
                "Marking: Content and structure 10, Language accuracy 10, "
                "Design and clarity 5, Delivery 5."
            ),
            "assignment_type": "mixed",
            "max_points": 30,
        },
        "skills": ["speaking", "writing"],
    },
]

# ── Готовые формулировки блоков ────────────────────────────────────────────
BLOCK_SUGGESTIONS = [
    {
        "name": "A1–A2 · Starter Course",
        "cefr_level": "A2",
        "description": "Базовый курс: to be, Present Simple, бытовая лексика, "
        "короткие диалоги и сообщения.",
    },
    {
        "name": "B1 · Intermediate English",
        "cefr_level": "B1",
        "description": "Времена группы Perfect, модальные глаголы, темы «работа, "
        "путешествия, технологии», связные тексты 150–200 слов.",
    },
    {
        "name": "B2 · Upper-Intermediate",
        "cefr_level": "B2",
        "description": "Сложные времена, условные предложения, Passive, аргументативное "
        "письмо и дискуссия.",
    },
    {
        "name": "Grammar in Context",
        "cefr_level": "",
        "description": "Сквозной грамматический трек: от Present Simple до смешанных "
        "условных предложений с отработкой в речи и письме.",
    },
    {
        "name": "ОГЭ · Подготовка (9 класс)",
        "cefr_level": "A2",
        "description": "Форматы ОГЭ: аудирование, чтение, грамматика и лексика, "
        "письмо (электронное), говорение.",
    },
    {
        "name": "ЕГЭ · Подготовка (11 класс)",
        "cefr_level": "B2",
        "description": "Форматы ЕГЭ: письмо (email), проект, говорение (монолог и "
        "сравнение картинок), лексика высокого уровня.",
    },
    {
        "name": "IELTS Academic",
        "cefr_level": "C1",
        "description": "Task 1 (графики), Task 2 (эссе), стратегии чтения и аудирования, "
        "Speaking Parts 1–3.",
    },
    {
        "name": "Business English",
        "cefr_level": "B2",
        "description": "Деловая переписка, встречи, презентации, переговоры, "
        "бизнес-лексика и small talk.",
    },
    {
        "name": "Speaking Club",
        "cefr_level": "B1",
        "description": "Разговорная практика: обсуждения, дебаты, ролевые ситуации, "
        "работа над беглостью и произношением.",
    },
    {
        "name": "Phonetics & Pronunciation",
        "cefr_level": "",
        "description": "Звуки, ударение, интонация, linking: минимальные пары, "
        "shadowing, скороговорки.",
    },
    {
        "name": "Travel English · B1",
        "cefr_level": "B1",
        "description": "Аэропорт, бронирование, жалобы, описание мест — как в ProgressMe «Traveling» и «English for traveling».",
    },
    {
        "name": "Movie Time · A2–B1",
        "cefr_level": "B1",
        "description": "Фильмы, сериалы, рецензии: лексика эмоций, описание сюжета, выражение мнения.",
    },
    {
        "name": "IT English · B1–B2",
        "cefr_level": "B2",
        "description": "Разработка, встречи, документация: agile-лексика, small talk для созвонов.",
    },
    {
        "name": "Happy Learning · Marathon",
        "cefr_level": "A2",
        "description": "Марафон на 5 дней: ежедневные задания, словарь, геймификация — формат ProgressMe Marathon.",
    },
]

# ── Готовые формулировки тем ───────────────────────────────────────────────
TOPIC_SUGGESTIONS = [
    {
        "title": "Present Simple vs Present Continuous",
        "description": "Регулярные действия против действий сейчас; "
        "глаголы состояния (know, believe, own).",
        "skills": ["grammar"],
    },
    {
        "title": "Past Simple vs Past Continuous",
        "description": "Завершённые действия и фон действия; while, when, as.",
        "skills": ["grammar"],
    },
    {
        "title": "Present Perfect: опыт и результат",
        "description": "ever, never, already, yet, just, for, since; "
        "отличие от Past Simple по маркерам времени.",
        "skills": ["grammar"],
    },
    {
        "title": "Будущее время: will / going to / Present Continuous",
        "description": "Спонтанное решение, план, договорённость; предсказания и намерения.",
        "skills": ["grammar"],
    },
    {
        "title": "Модальные глаголы: возможность, обязанность, совет",
        "description": "can, could, must, have to, should, might, may; оттенки вежливости.",
        "skills": ["grammar"],
    },
    {
        "title": "Условные предложения 0–2",
        "description": "Zero, First, Second Conditionals; unless, as long as.",
        "skills": ["grammar"],
    },
    {
        "title": "Passive Voice",
        "description": "Формы пассива в основных временах, агент и его опущение, "
        "безличные конструкции.",
        "skills": ["grammar"],
    },
    {
        "title": "Reported Speech",
        "description": "Согласование времён, вопросы и просьбы в косвенной речи.",
        "skills": ["grammar"],
    },
    {
        "title": "Артикли и квантификаторы",
        "description": "a/an, the, zero article; some/any, much/many, a few/a little.",
        "skills": ["grammar"],
    },
    {
        "title": "Фразовые глаголы: работа и учёба",
        "description": "carry out, put off, look into, come up with, hand in, catch up on.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Коллокации: do / make / take / have",
        "description": "Устойчивые сочетания и типичные ошибки русскоязычных.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Словообразование: префиксы и суффиксы",
        "description": "Таблицы производных слов, отрицательные префиксы, "
        "форматы экзаменационных заданий.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Лексика: Travel and Transport",
        "description": "Поездки, аэропорт, бронирование, жалобы; готовый набор карточек.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Лексика: Work and Careers",
        "description": "Резюме, собеседование, должностные обязанности, удалённая работа.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Лексика: Food and Health",
        "description": "Питание, привычки, спорт; описание картинки и мини-диалоги.",
        "skills": ["vocabulary"],
    },
    {
        "title": "Лексика: Technology and Media",
        "description": "Гаджеты, соцсети, цифровая гигиена, аргументы «за и против».",
        "skills": ["vocabulary"],
    },
    {
        "title": "Средства связи для эссе",
        "description": "Linking words по функциям: добавление, контраст, причина, вывод.",
        "skills": ["writing"],
    },
    {
        "title": "Описание графиков и диаграмм",
        "description": "Язык трендов (increase, fluctuate, peak), структура отчёта IELTS Task 1.",
        "skills": ["writing"],
    },
    {
        "title": "Email и письмо: формальный и неформальный регистр",
        "description": "Приветствия, формулировки просьб, вежливые отказы, подпись.",
        "skills": ["writing"],
    },
    {
        "title": "Описательное письмо: place, person, event",
        "description": "Порядок прилагательных, сенсорная лексика, структура описания.",
        "skills": ["writing"],
    },
    {
        "title": "Small talk и светская беседа",
        "description": "Безопасные темы, вопросы-продолжения, вежливое завершение разговора.",
        "skills": ["speaking"],
    },
    {
        "title": "Дебаты: аргумент и контраргумент",
        "description": "Формулировка позиции, возражения, hedging (it seems, arguably).",
        "skills": ["speaking"],
    },
    {
        "title": "Описание картинки (экзаменационная задача)",
        "description": "План монолога: что видно, где, что происходит, предположения и вывод.",
        "skills": ["speaking"],
    },
    {
        "title": "Произношение: ударение и интонация",
        "description": "Словесное ударение, sentence stress, восходящая и нисходящая интонация.",
        "skills": ["speaking"],
    },
    {
        "title": "Чтение: skimming и scanning",
        "description": "Стратегии поиска информации и определения главной идеи, "
        "работа с незнакомой лексикой.",
        "skills": ["reading"],
    },
    {
        "title": "Аудирование: конспект и ключевые слова",
        "description": "Note-taking, прогнозирование ответа, ловушки «похожее звучание».",
        "skills": ["listening"],
    },
    {
        "title": "Travelling and weather",
        "description": "Let's talk about the Scandinavian countries on the example of Denmark — ProgressMe unit «Denmark» style: description, listening, vocabulary.",
        "skills": ["vocabulary", "listening"],
    },
    {
        "title": "Movie Time: describing a plot",
        "description": "Warm-up, Vocabulary (genres), Listening (trailer), Speaking, Writing a review — секции как в ProgressMe Sections.",
        "skills": ["speaking", "writing"],
    },
    {
        "title": "Group Lessons: virtual class",
        "description": "Виртуальный класс: чат, реакции, таймер, интерактивная доска — механики ProgressMe Virtual Class.",
        "skills": ["speaking", "listening"],
    },
    {
        "title": "Word Formation from Letters",
        "description": "Anagram and sentence builder: соберите слово из букв, предложение из слов — новые типы автопроверки.",
        "skills": ["vocabulary", "grammar"],
    },
    {
        "title": "Sorting into Columns",
        "description": "Распределение по колонкам: countable/uncountable, formal/informal, British/American.",
        "skills": ["vocabulary"],
    },
]

# ── Курсовые наборы ────────────────────────────────────────────────────────
# Задания импортируются черновиками: преподаватель сначала смотрит и правит,
# потом публикует. Вопросы и карточки создаются вместе с заданиями и темами.

COURSE_PACKS = [
    {
        "slug": "general-a2",
        "name": "General English · A2 Elementary",
        "summary": "Базовый общий курс: времена в бытовых ситуациях, лексика "
        "«путешествия, еда, покупки», короткое письмо и устный ответ.",
        "level": "A2",
        "blocks": [
            {
                "name": "A2 · Tenses in Everyday Life",
                "cefr_level": "A2",
                "description": "Настоящее, прошедшее и будущее время в ситуациях "
                "повседневного общения.",
                "topics": [
                    {
                        "title": "Present Simple vs Present Continuous",
                        "description": "Регулярные действия против действий "
                        "происходящих сейчас; глаголы состояния.",
                        "assignments": [
                            {
                                "title": "Choose the correct form: routines and now",
                                "description": (
                                    "Put the verb in brackets into Present Simple "
                                    "or Present Continuous.\n\n"
                                    "1. Anna usually ___ (take) the bus, but today "
                                    "she ___ (walk).\n"
                                    "2. Look! It ___ (rain) again.\n"
                                    "3. I ___ (not / understand) this rule.\n"
                                    "4. What ___ you ___ (do) at the moment?\n"
                                    "5. My parents ___ (work) in a hospital.\n"
                                    "6. She ___ (always / lose) her keys!\n"
                                    "7. Water ___ (boil) at 100 degrees.\n"
                                    "8. We ___ (stay) at a hotel this week.\n\n"
                                    "Marking: 1 point per sentence, 8 sentences "
                                    "(8 points) + 2 points for your own two "
                                    "sentences about today."
                                ),
                                "type": "text",
                                "max_points": 10,
                                "skills": ["grammar"],
                            },
                            {
                                "title": "Quiz: Present Simple or Continuous?",
                                "description": (
                                    "Ten short items, checked automatically. "
                                    "Read the whole sentence before choosing: the "
                                    "answer depends on the time marker."
                                ),
                                "type": "quiz",
                                "max_points": 10,
                                "skills": ["grammar"],
                                "questions": [
                                    {
                                        "kind": "mcq",
                                        "text": "Sarah ___ to the gym every Monday.",
                                        "points": 2,
                                        "explanation": "every Monday — регулярное "
                                        "действие, Present Simple.",
                                        "choices": [
                                            {"text": "goes", "correct": True},
                                            {"text": "is going"},
                                            {"text": "go"},
                                        ],
                                    },
                                    {
                                        "kind": "mcq",
                                        "text": "Be quiet! The baby ___.",
                                        "points": 2,
                                        "explanation": "Действие происходит сейчас "
                                        "— Present Continuous.",
                                        "choices": [
                                            {"text": "sleeps"},
                                            {"text": "is sleeping", "correct": True},
                                            {"text": "sleep"},
                                        ],
                                    },
                                    {
                                        "kind": "gap",
                                        "text": "I can't talk now, I ___ (drive).",
                                        "points": 2,
                                        "explanation": "am driving — действие в момент речи.",
                                        "choices": [
                                            {"text": "am driving", "correct": True},
                                            {"text": "'m driving", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "mcq",
                                        "text": "He ___ his keys every week — it's so annoying!",
                                        "points": 2,
                                        "explanation": "always + Continuous выражает раздражение.",
                                        "choices": [
                                            {"text": "always loses"},
                                            {
                                                "text": "is always losing",
                                                "correct": True,
                                            },
                                            {"text": "always lose"},
                                        ],
                                    },
                                    {
                                        "kind": "mcq",
                                        "text": "We ___ this answer: 'believe' has "
                                        "no continuous form here.",
                                        "points": 2,
                                        "explanation": "Глагол состояния: "
                                        "I believe, не I am believing.",
                                        "choices": [
                                            {"text": "believe", "correct": True},
                                            {"text": "are believing"},
                                            {"text": "is believing"},
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Past Simple: irregular verbs",
                        "description": "Вторая форма глагола, вопросы и отрицания "
                        "в прошедшем времени.",
                        "assignments": [
                            {
                                "title": "Irregular verbs: 20 items",
                                "description": (
                                    "Write the Past Simple form of each verb and "
                                    "make one sentence with it.\n\n"
                                    "go · see · take · buy · bring · teach · "
                                    "catch · write · speak · eat · "
                                    "drink · begin · know · think · find · "
                                    "tell · meet · run · swim · drive\n\n"
                                    "Example: go → went. Last summer we went to "
                                    "the seaside.\n\n"
                                    "Marking: 0.5 point per form, 0.5 point per "
                                    "sentence."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["grammar", "vocabulary"],
                            },
                        ],
                    },
                    {
                        "title": "Future: going to vs will",
                        "description": "Планы, намерения, спонтанные решения и предсказания.",
                        "assignments": [
                            {
                                "title": "Plans and predictions",
                                "description": (
                                    "Complete with 'will', 'won't' or 'going to' "
                                    "and explain your choice in one line.\n\n"
                                    "1. Look at those clouds — it ___ rain.\n"
                                    "2. A: The phone is ringing. B: I ___ answer "
                                    "it!\n"
                                    "3. We ___ visit our grandparents on Sunday, "
                                    "we already bought the tickets.\n"
                                    "4. I think people ___ live on Mars one day.\n"
                                    "5. She ___ (not / come) to the party, she is "
                                    "ill.\n\n"
                                    "Then write 3 sentences about your plans for "
                                    "next weekend.\n\n"
                                    "Marking: 10 points for the gaps with "
                                    "explanations, 5 points for your sentences."
                                ),
                                "type": "text",
                                "max_points": 15,
                                "skills": ["grammar", "writing"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "A2 · Vocabulary for Daily Life",
                "cefr_level": "A2",
                "description": "Лексика повседневных ситуаций с карточками для тренажёра.",
                "topics": [
                    {
                        "title": "Travel and Transport",
                        "description": "Поездки, аэропорт, транспорт: слова, коллокации, диалоги.",
                        "decks": [
                            {
                                "title": "Travel and Transport · A2",
                                "description": "20 слов и выражений по теме «путешествия».",
                                "cards": [
                                    {
                                        "front": "departure",
                                        "back": "отправление, вылет",
                                        "example": "Our departure time is 7 a.m.",
                                    },
                                    {
                                        "front": "to check in",
                                        "back": "регистрироваться (на рейс)",
                                        "example": "We checked in online.",
                                    },
                                    {
                                        "front": "boarding pass",
                                        "back": "посадочный талон",
                                        "example": "Show your boarding pass at the gate.",
                                    },
                                    {
                                        "front": "luggage / baggage",
                                        "back": "багаж",
                                        "example": "How many pieces of luggage do you have?",
                                    },
                                    {
                                        "front": "to book",
                                        "back": "бронировать",
                                        "example": "I booked a hotel near the beach.",
                                    },
                                    {
                                        "front": "single / return ticket",
                                        "back": "билет в один конец / туда-обратно",
                                        "example": "A return ticket is cheaper.",
                                    },
                                    {
                                        "front": "delay",
                                        "back": "задержка",
                                        "example": "The flight was delayed by two hours.",
                                    },
                                    {
                                        "front": "sightseeing",
                                        "back": "осмотр достопримечательностей",
                                        "example": "We went sightseeing in Rome.",
                                    },
                                    {
                                        "front": "to miss a train",
                                        "back": "опоздать на поезд",
                                        "example": "We missed the train and took a bus.",
                                    },
                                    {
                                        "front": "journey / trip",
                                        "back": "поездка, путешествие",
                                        "example": "The journey took five hours.",
                                    },
                                ],
                            }
                        ],
                        "assignments": [
                            {
                                "title": "Travel vocabulary in context",
                                "description": (
                                    "1. Complete the text with the words from the "
                                    "box (two words are extra):\n"
                                    "delay · platform · luggage · boarding pass · "
                                    "return · sightseeing\n\n"
                                    "2. Repeat the flashcards in the trainer and "
                                    "write 5 sentences about your last trip "
                                    "using the new words.\n\n"
                                    "Marking: 6 points for the text, 5 points for "
                                    "the sentences, 4 points for accuracy."
                                ),
                                "type": "text",
                                "max_points": 15,
                                "skills": ["vocabulary"],
                            },
                        ],
                    },
                    {
                        "title": "Food and Shopping",
                        "description": "Продукты, магазины, заказы в кафе.",
                        "decks": [
                            {
                                "title": "Food and Shopping · A2",
                                "description": "Базовая лексика покупок и еды.",
                                "cards": [
                                    {
                                        "front": "receipt",
                                        "back": "чек",
                                        "example": "Would you like a receipt?",
                                    },
                                    {
                                        "front": "to pay by card",
                                        "back": "платить картой",
                                        "example": "Can I pay by card?",
                                    },
                                    {
                                        "front": "discount",
                                        "back": "скидка",
                                        "example": "There is a 20% discount today.",
                                    },
                                    {
                                        "front": "to try on",
                                        "back": "примерять",
                                        "example": "Can I try on this jacket?",
                                    },
                                    {
                                        "front": "a loaf of bread",
                                        "back": "буханка хлеба",
                                        "example": "I need a loaf of bread.",
                                    },
                                    {
                                        "front": "a bottle of water",
                                        "back": "бутылка воды",
                                        "example": "A bottle of water, please.",
                                    },
                                    {
                                        "front": "fresh / frozen",
                                        "back": "свежий / замороженный",
                                        "example": "I prefer fresh vegetables.",
                                    },
                                    {
                                        "front": "to order",
                                        "back": "заказывать",
                                        "example": "Are you ready to order?",
                                    },
                                ],
                            }
                        ],
                        "assignments": [
                            {
                                "title": "At the shop: write the dialogue",
                                "description": (
                                    "Write a dialogue (10–12 lines) between a "
                                    "customer and a shop assistant. Include: "
                                    "asking for a product, the price, trying it "
                                    "on, paying by card, asking for a receipt.\n\n"
                                    "Then record yourself reading both parts and "
                                    "attach the audio if you like.\n\n"
                                    "Marking: structure 5, vocabulary 5, "
                                    "accuracy 5."
                                ),
                                "type": "mixed",
                                "max_points": 15,
                                "skills": ["vocabulary", "speaking"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "A2 · Skills: Writing and Speaking",
                "cefr_level": "A2",
                "description": "Короткое личное письмо и устный ответ о себе.",
                "topics": [
                    {
                        "title": "Writing short messages",
                        "description": "Email другу: структура, регистр, объём.",
                        "assignments": [
                            {
                                "title": "Email to a friend (50–70 words)",
                                "description": (
                                    "You received an email from your English "
                                    "friend:\n\n"
                                    "'…Last weekend I went to a concert and it "
                                    "was amazing! What do you usually do at the "
                                    "weekend? Have you ever been to a live "
                                    "concert? What music do you like?…'\n\n"
                                    "Write an email of 50–70 words: answer the "
                                    "three questions, ask 2 questions about the "
                                    "concert. Follow the email format "
                                    "(greeting, body, closing, name).\n\n"
                                    "Marking: content 5, organisation 4, "
                                    "vocabulary 3, grammar 3."
                                ),
                                "type": "text",
                                "max_points": 15,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Speaking: my typical day",
                        "description": "Монолог 60–90 секунд о распорядке дня.",
                        "assignments": [
                            {
                                "title": "Audio: describe your typical day",
                                "description": (
                                    "Record a 60–90 second answer:\n"
                                    "• what time you get up and what you do "
                                    "first;\n"
                                    "• your morning routine;\n"
                                    "• what you do after school/work;\n"
                                    "• what your favourite part of the day is "
                                    "and why.\n\n"
                                    "Use time expressions: first, then, after "
                                    "that, usually, sometimes.\n\n"
                                    "Marking: fluency 4, vocabulary 4, grammar 4, "
                                    "pronunciation 3."
                                ),
                                "type": "audio",
                                "max_points": 15,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "general-b1b2",
        "name": "General English · B1–B2",
        "summary": "Общий курс среднего уровня: времена Perfect, условные "
        "предложения, пассив, фразовые глаголы, эссе и дискуссия.",
        "level": "B1–B2",
        "blocks": [
            {
                "name": "B1–B2 · Grammar in Context",
                "cefr_level": "B2",
                "description": "Грамматика через тексты и устные ситуации, "
                "а не через отдельные упражнения.",
                "topics": [
                    {
                        "title": "Present Perfect vs Past Simple",
                        "description": "Опыт, результат и завершённое прошедшее: "
                        "маркеры времени решают всё.",
                        "assignments": [
                            {
                                "title": "Gap fill with time markers",
                                "description": (
                                    "Complete with Present Perfect or Past Simple.\n\n"
                                    "1. I ___ (live) here since 2015.\n"
                                    "2. We ___ (move) here in 2015.\n"
                                    "3. She ___ (already / read) this book.\n"
                                    "4. He ___ (read) it last month.\n"
                                    "5. ___ you ___ (finish) the report yet?\n"
                                    "6. They ___ (not / call) us yesterday.\n"
                                    "7. I ___ (know) Maria for ten years.\n"
                                    "8. Shakespeare ___ (write) 37 plays.\n\n"
                                    "Marking: 1 point per item; circle the time "
                                    "marker that helped you decide."
                                ),
                                "type": "text",
                                "max_points": 16,
                                "skills": ["grammar"],
                            },
                            {
                                "title": "Transformations: Perfect tenses",
                                "description": (
                                    "Rewrite each sentence keeping the meaning, "
                                    "using the word in CAPITALS (2–5 words).\n\n"
                                    "1. I started working here in 2019. (FOR)\n"
                                    "2. This is the best film I have seen. "
                                    "(EVER)\n"
                                    "3. She left ten minutes ago. (GONE)\n"
                                    "4. We haven't met before. (FIRST)\n"
                                    "5. He began learning English three years ago. "
                                    "(BEEN)\n\n"
                                    "Marking: 2 points per item, all or nothing."
                                ),
                                "type": "text",
                                "max_points": 10,
                                "skills": ["grammar", "writing"],
                            },
                        ],
                    },
                    {
                        "title": "Conditionals and wishes",
                        "description": "Zero, First, Second Conditionals; I wish / If only.",
                        "assignments": [
                            {
                                "title": "Conditionals: match and complete",
                                "description": (
                                    "Part 1. Match the halves (1–6).\n"
                                    "Part 2. Open the brackets:\n"
                                    "1. If you heat water to 100°C, it ___ "
                                    "(boil).\n"
                                    "2. If I ___ (have) more time, I would "
                                    "learn Italian.\n"
                                    "3. We will miss the train unless we ___ "
                                    "(leave) now.\n"
                                    "4. If I were you, I ___ (apologise).\n"
                                    "5. I wish I ___ (can) swim.\n\n"
                                    "Part 3. Write 3 sentences about what you "
                                    "would change in your city.\n\n"
                                    "Marking: 6 + 10 + 4 points."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["grammar", "writing"],
                            },
                        ],
                    },
                    {
                        "title": "Passive Voice and Reported Speech",
                        "description": "Безличные конструкции и передача чужой речи.",
                        "assignments": [
                            {
                                "title": "Find and correct the mistakes",
                                "description": (
                                    "Each sentence has ONE mistake. Rewrite it and "
                                    "explain the rule.\n\n"
                                    "1. The letter was send yesterday.\n"
                                    "2. He said me that he was tired.\n"
                                    "3. The house built in 1900.\n"
                                    "4. She asked where did I live.\n"
                                    "5. It is believe that the economy will grow.\n"
                                    "6. They have been invited to the conference "
                                    "last week.\n\n"
                                    "Marking: 2 points per sentence "
                                    "(1 correction + 1 explanation)."
                                ),
                                "type": "text",
                                "max_points": 12,
                                "skills": ["grammar"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "B1–B2 · Vocabulary Power",
                "cefr_level": "B1",
                "description": "Фразовые глаголы, словообразование и коллокации.",
                "topics": [
                    {
                        "title": "Phrasal verbs: work and study",
                        "description": "Работа, учёба, дедлайны: 20 фразовых глаголов в контексте.",
                        "decks": [
                            {
                                "title": "Phrasal verbs · Work and Study",
                                "description": "Фразовые глаголы для работы и учёбы с примерами.",
                                "cards": [
                                    {
                                        "front": "to carry out",
                                        "back": "проводить, выполнять",
                                        "example": "We carried out a survey.",
                                    },
                                    {
                                        "front": "to put off",
                                        "back": "откладывать",
                                        "example": "Don't put off your homework.",
                                    },
                                    {
                                        "front": "to look into",
                                        "back": "изучать, расследовать",
                                        "example": "I'll look into this problem.",
                                    },
                                    {
                                        "front": "to come up with",
                                        "back": "придумать",
                                        "example": "She came up with a great idea.",
                                    },
                                    {
                                        "front": "to hand in",
                                        "back": "сдавать (работу)",
                                        "example": "Hand in your essays by Friday.",
                                    },
                                    {
                                        "front": "to catch up on",
                                        "back": "наверстать",
                                        "example": "I need to catch up on sleep.",
                                    },
                                    {
                                        "front": "to take on",
                                        "back": "брать на себя",
                                        "example": "He took on extra responsibilities.",
                                    },
                                    {
                                        "front": "to fall behind",
                                        "back": "отставать",
                                        "example": "Don't fall behind the schedule.",
                                    },
                                ],
                            }
                        ],
                        "assignments": [
                            {
                                "title": "Phrasal verbs in context",
                                "description": (
                                    "1. Repeat the flashcards in the trainer.\n"
                                    "2. Complete the sentences with a phrasal verb "
                                    "in the correct form:\n"
                                    "a) We had to ___ the meeting because the "
                                    "manager was ill.\n"
                                    "b) Please ___ your report before 5 p.m.\n"
                                    "c) She ___ an interesting solution.\n"
                                    "d) I'm ___ with my coursework after the "
                                    "illness.\n"
                                    "3. Write 4 sentences about your own work or "
                                    "studies using four different phrasal verbs.\n\n"
                                    "Marking: 8 + 8 + 4 points."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["vocabulary", "writing"],
                            },
                        ],
                    },
                    {
                        "title": "Word formation",
                        "description": "Производные слова: существительные, "
                        "прилагательные, наречия, отрицания.",
                        "assignments": [
                            {
                                "title": "Word formation table",
                                "description": (
                                    "Complete the table: noun — verb — adjective — "
                                    "adverb — negative.\n\n"
                                    "decide · succeed · employ · knowledge · "
                                    "create · differ · possible · manage\n\n"
                                    "Then complete the sentences with the right "
                                    "form:\n"
                                    "1. Her ___ (decide) surprised everyone.\n"
                                    "2. This task is ___ (possible) to finish "
                                    "today.\n"
                                    "3. He works very ___ (efficiency).\n\n"
                                    "Marking: 12 points for the table, 6 for the "
                                    "sentences."
                                ),
                                "type": "text",
                                "max_points": 18,
                                "skills": ["vocabulary"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "B1–B2 · Writing and Speaking",
                "cefr_level": "B2",
                "description": "Аргументативное письмо, дискуссия и чтение с пониманием деталей.",
                "topics": [
                    {
                        "title": "Opinion essay",
                        "description": "Структура эссе, средства связи, шкала оценивания.",
                        "assignments": [
                            {
                                "title": "Opinion essay: remote work (200–250 words)",
                                "description": (
                                    "Some people think that working from home is "
                                    "better than working in an office. To what "
                                    "extent do you agree?\n\n"
                                    "Write 200–250 words: introduction with a "
                                    "thesis, two arguments with examples, one "
                                    "counter-argument, conclusion.\n\n"
                                    "Use at least 5 linking words and no more "
                                    "than two rhetorical questions.\n\n"
                                    "Marking (5 per criterion): Task Achievement, "
                                    "Coherence & Cohesion, Lexical Resource, "
                                    "Grammatical Range & Accuracy."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Discussion and debate",
                        "description": "Аргумент, контраргумент, вежливое несогласие.",
                        "assignments": [
                            {
                                "title": "Audio: 2-minute argument",
                                "description": (
                                    "Choose ONE statement and record a 2-minute "
                                    "argument:\n"
                                    "• Social media does more harm than good.\n"
                                    "• School uniforms should be cancelled.\n"
                                    "• Money can buy happiness.\n\n"
                                    "Structure: position → argument 1 with example "
                                    "→ argument 2 → what the other side would say "
                                    "→ your reply.\n\n"
                                    "Use hedging: it seems to me, arguably, "
                                    "I take your point, but…\n\n"
                                    "Marking: structure 5, vocabulary 5, grammar 5, "
                                    "delivery 5."
                                ),
                                "type": "audio",
                                "max_points": 20,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                    {
                        "title": "Reading for detail",
                        "description": "True / False / Not Stated и работа с незнакомой лексикой.",
                        "assignments": [
                            {
                                "title": "Reading: True / False / Not Stated",
                                "description": (
                                    "Read the attached article and mark the "
                                    "statements 1–8 as True, False or Not Stated. "
                                    "Quote the sentence that proves your answer "
                                    "for every True and False.\n\n"
                                    "Then answer in 4–5 sentences: what is the "
                                    "author's main message and who would benefit "
                                    "most from reading it?\n\n"
                                    "Marking: 8 points for the statements, "
                                    "8 points for the justification, 4 points for "
                                    "the summary."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["reading"],
                            },
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "exam-prep",
        "name": "Экзаменационный курс · ОГЭ и ЕГЭ",
        "summary": "Форматы российских экзаменов: грамматика и лексика, "
        "письмо (email), проект, говорение, чтение и аудирование.",
        "level": "A2–B2",
        "blocks": [
            {
                "name": "ОГЭ · Грамматика и лексика",
                "cefr_level": "A2",
                "description": "Задания на грамматические формы слова и словообразование.",
                "topics": [
                    {
                        "title": "Грамматика: формы слова",
                        "description": "Время, залог, степень сравнения, "
                        "словообразовательная форма.",
                        "assignments": [
                            {
                                "title": "Преобразуйте формы слов (10 заданий)",
                                "description": (
                                    "Преобразуйте слово в скобках так, чтобы оно "
                                    "грамматически соответствовало предложению.\n\n"
                                    "1. Yesterday I (SEE) a wonderful film.\n"
                                    "2. Look! The children (PLAY) in the garden.\n"
                                    "3. This is (INTERESTING) book I have ever "
                                    "read.\n"
                                    "4. The letter (SEND) two days ago.\n"
                                    "5. If the weather is fine, we (GO) for "
                                    "a walk.\n"
                                    "6. She has lived here (SINCE) 2018 — "
                                    "проверьте форму глагола.\n"
                                    "7. My brother is (TALL) than me.\n"
                                    "8. He (NOT / FINISH) his project yet.\n"
                                    "9. When I came in, they (HAVE) dinner.\n"
                                    "10. Moscow is one of (BIG) cities "
                                    "in the world.\n\n"
                                    "Оценивание: 1 балл за каждое задание. "
                                    "Орфографические ошибки обнуляют балл."
                                ),
                                "type": "text",
                                "max_points": 10,
                                "skills": ["grammar"],
                            },
                            {
                                "title": "Словообразование: 8 заданий",
                                "description": (
                                    "Образуйте однокоренное слово, чтобы оно "
                                    "грамматически и по смыслу соответствовало "
                                    "тексту.\n\n"
                                    "1. He is a very ___ person: he always helps "
                                    "others (KIND).\n"
                                    "2. The ___ of the city centre took two years "
                                    "(BUILD).\n"
                                    "3. Her decision was completely ___ "
                                    "(EXPECT).\n"
                                    "4. We had a ___ discussion (MEAN).\n"
                                    "5. The exam was ___ difficult than I thought "
                                    "(REAL).\n"
                                    "6. He speaks three languages ___ (FLUENCY).\n"
                                    "7. There are many ___ shops in this street "
                                    "(BOOK).\n"
                                    "8. Her ___ to learn English is strong "
                                    "(DESIRE).\n\n"
                                    "Оценивание: 1 балл за задание; учитывается "
                                    "и приставка, и суффикс, и часть речи."
                                ),
                                "type": "text",
                                "max_points": 8,
                                "skills": ["vocabulary"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "ОГЭ · Письмо и говорение",
                "cefr_level": "A2",
                "description": "Личное письмо и устные ответы в формате экзамена.",
                "topics": [
                    {
                        "title": "Электронное письмо",
                        "description": "Ответы на три вопроса друга, "
                        "три своих вопроса, объём 100–120 слов.",
                        "assignments": [
                            {
                                "title": "Email to a friend (100–120 words)",
                                "description": (
                                    "You have received an email message from your "
                                    "English-speaking pen-friend Sam:\n\n"
                                    "'…My family is planning a trip to the "
                                    "mountains this summer. What is your favourite "
                                    "way to spend summer holidays? Do you prefer "
                                    "active or relaxing holidays, and why? What "
                                    "places in your region would you recommend to "
                                    "a tourist?…'\n\n"
                                    "Write an email to Sam: answer his 3 questions, "
                                    "ask 3 questions about his trip. "
                                    "100–120 words. Follow the email format "
                                    "(greeting, thanks, body, closing, signature).\n\n"
                                    "Оценивание (по критериям ОГЭ): решение "
                                    "коммуникативной задачи 6, организация 2, "
                                    "лексика и грамматика 6, орфография и "
                                    "пунктуация 2."
                                ),
                                "type": "text",
                                "max_points": 16,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Говорение: монолог",
                        "description": "Непрерывный монолог по теме с опорой на план.",
                        "assignments": [
                            {
                                "title": "Монолог: My favourite season (2 minutes)",
                                "description": (
                                    "Запишите монолог на 10–12 фраз по плану:\n"
                                    "• your favourite season and why;\n"
                                    "• what you usually do during this season;\n"
                                    "• what you did last year at this time;\n"
                                    "• whether you would like to live in a country "
                                    "with a different climate.\n\n"
                                    "Начните с вводной фразы и закончите выводом. "
                                    "Не читайте с листа.\n\n"
                                    "Оценивание: решение задачи 5, лексика и "
                                    "грамматика 5, произношение 5."
                                ),
                                "type": "audio",
                                "max_points": 15,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "ЕГЭ · Проект и письменная речь",
                "cefr_level": "B2",
                "description": "Задания 37 и 38: электронное письмо и проект с таблицей данных.",
                "topics": [
                    {
                        "title": "Проект (задание 38)",
                        "description": "Структура проекта: вступление, факты, "
                        "сравнение, проблема и решение, вывод.",
                        "assignments": [
                            {
                                "title": "Project report: how students spend free time",
                                "description": (
                                    "You are doing a project on how students in "
                                    "Zetland spend their free time. You have found "
                                    "a table with the survey results:\n\n"
                                    "Watching videos — 34%\n"
                                    "Sport — 21%\n"
                                    "Reading — 18%\n"
                                    "Computer games — 17%\n"
                                    "Walking with friends — 10%\n\n"
                                    "Write a report of 200–250 words: "
                                    "(1) introduction with the aim of the project; "
                                    "(2) 2–3 facts from the table; "
                                    "(3) 1–2 comparisons; (4) a problem connected "
                                    "with free time and your solution; "
                                    "(5) conclusion with your opinion.\n\n"
                                    "Оценивание (по критериям ЕГЭ): решение "
                                    "коммуникативной задачи 4, организация 3, "
                                    "лексика и грамматика 3, орфография 2. "
                                    "Слова в заголовке не считаются."
                                ),
                                "type": "text",
                                "max_points": 14,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Говорение: сравнение фотографий",
                        "description": "Задание 4 ЕГЭ: связное сравнение двух фотографий.",
                        "assignments": [
                            {
                                "title": "Speaking: compare two photos (2 min)",
                                "description": (
                                    "Посмотрите на две фотографии (прикреплены) и "
                                    "запишите сравнение по плану:\n"
                                    "• краткое описание обеих фотографий;\n"
                                    "• что у них общего;\n"
                                    "• одно различие;\n"
                                    "• какой способ отдыха вам ближе и почему;\n"
                                    "• вывод.\n\n"
                                    "Используйте язык сравнения: both photos show…, "
                                    "while…, the first picture depicts…, "
                                    "in contrast…\n\n"
                                    "Оценивание: решение задачи 5, лексика и "
                                    "грамматика 5, произношение 5."
                                ),
                                "type": "mixed",
                                "max_points": 15,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Чтение и аудирование",
                "cefr_level": "B1",
                "description": "Стратегии экзаменационного чтения и аудирования.",
                "topics": [
                    {
                        "title": "Чтение: True / False / Not Stated",
                        "description": "Поиск информации и различение «ложь» и «не сказано».",
                        "assignments": [
                            {
                                "title": "Reading: 8 statements",
                                "description": (
                                    "Прочитайте текст и определите, соответствуют "
                                    "утверждения 1–8 содержанию (True), "
                                    "противоречат ему (False) или в тексте об этом "
                                    "не сказано (Not Stated).\n\n"
                                    "Правило: False требует цитаты-опровержения; "
                                    "Not Stated ставится, если в тексте нет "
                                    "информации ни за, ни против.\n\n"
                                    "Оценивание: 1 балл за утверждение. "
                                    "Затем 4 балла за ответ на вопрос: "
                                    "какова главная мысль текста?"
                                ),
                                "type": "text",
                                "max_points": 12,
                                "skills": ["reading"],
                            },
                        ],
                    },
                    {
                        "title": "Аудирование: стратегии",
                        "description": "Прогнозирование ответа, конспект, ловушки похожих слов.",
                        "assignments": [
                            {
                                "title": "Listening: multiple choice + notes",
                                "description": (
                                    "Часть 1. Прослушайте запись дважды и выберите "
                                    "правильный ответ в заданиях 1–6.\n"
                                    "Часть 2. Прослушайте ещё раз и заполните "
                                    "таблицу (одним словом или числом).\n"
                                    "Часть 3. Ответьте письменно: какая информация "
                                    "была лишней и почему.\n\n"
                                    "Совет: до прослушивания подчеркните ключевые "
                                    "слова в вариантах и предположите, о чём "
                                    "пойдёт речь.\n\n"
                                    "Оценивание: 12 баллов за часть 1, 6 за "
                                    "таблицу, 2 за вывод."
                                ),
                                "type": "mixed",
                                "max_points": 20,
                                "skills": ["listening"],
                            },
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "business-english",
        "name": "Business English · B1+",
        "summary": "Деловое общение: письма, встречи, презентации, переговоры и бизнес-лексика.",
        "level": "B1–B2",
        "blocks": [
            {
                "name": "Business Communication",
                "cefr_level": "B1",
                "description": "Переписка и устное общение в деловой среде.",
                "topics": [
                    {
                        "title": "Business emails",
                        "description": "Запрос, напоминание, отказ и согласование сроков.",
                        "assignments": [
                            {
                                "title": "Write three business emails",
                                "description": (
                                    "Write three short emails (60–80 words each):\n"
                                    "1. Request information about a product from "
                                    "a supplier;\n"
                                    "2. Politely remind a colleague about a missed "
                                    "deadline;\n"
                                    "3. Decline an invitation to a conference and "
                                    "suggest another date.\n\n"
                                    "Use a subject line, a formal greeting and a "
                                    "closing. Avoid contractions and slang.\n\n"
                                    "Marking: task achievement 6, tone and "
                                    "politeness 6, accuracy 8."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Small talk and networking",
                        "description": "Начало разговора, уместные темы, вежливое завершение.",
                        "assignments": [
                            {
                                "title": "Audio: networking at a conference",
                                "description": (
                                    "Record a 90-second dialogue where you:\n"
                                    "• introduce yourself and your role;\n"
                                    "• ask two questions about the other person's "
                                    "company;\n"
                                    "• react to the answers with follow-up "
                                    "questions;\n"
                                    "• finish the conversation politely and suggest "
                                    "staying in touch.\n\n"
                                    "Useful language: I work in…, What brings you "
                                    "here?, That sounds interesting, let me give "
                                    "you my card.\n\n"
                                    "Marking: fluency 5, appropriacy 5, "
                                    "vocabulary 5, pronunciation 5."
                                ),
                                "type": "audio",
                                "max_points": 20,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Meetings, Presentations, Negotiations",
                "cefr_level": "B2",
                "description": "Язык встреч и презентаций, дипломатические формулировки.",
                "topics": [
                    {
                        "title": "Running a meeting",
                        "description": "Повестка, модерация, итоги.",
                        "assignments": [
                            {
                                "title": "Meeting agenda and minutes",
                                "description": (
                                    "Prepare for a 20-minute team meeting:\n"
                                    "1. Write an agenda with three points and time "
                                    "limits;\n"
                                    "2. Write the opening statement (2–3 sentences) "
                                    "and the closing summary;\n"
                                    "3. After the discussion, write short minutes "
                                    "(decisions, actions, deadlines).\n\n"
                                    "Useful language: Let's get down to business, "
                                    "Could we move on to…, To sum up, we agreed "
                                    "to…\n\n"
                                    "Marking: agenda 5, opening and closing 5, "
                                    "minutes 5, language 5."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["writing", "speaking"],
                            },
                        ],
                    },
                    {
                        "title": "Negotiation language",
                        "description": "Предложения, уступки, дипломатическое несогласие.",
                        "assignments": [
                            {
                                "title": "Negotiation: role play script",
                                "description": (
                                    "Situation: you are a client and your supplier "
                                    "raises prices by 15%. Write a 12-line "
                                    "negotiation dialogue and record it.\n\n"
                                    "Include: stating your position, making an "
                                    "offer, making a concession with a condition "
                                    "('If you…, we could…'), disagreeing politely, "
                                    "reaching a compromise.\n\n"
                                    "Marking: structure 5, negotiation language 8, "
                                    "accuracy 7."
                                ),
                                "type": "mixed",
                                "max_points": 20,
                                "skills": ["speaking", "writing"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Business Vocabulary",
                "cefr_level": "B1",
                "description": "Лексика финансов, маркетинга и управления.",
                "topics": [
                    {
                        "title": "Finance and marketing terms",
                        "description": "Базовые термины с карточками для тренажёра.",
                        "decks": [
                            {
                                "title": "Finance and Marketing · B1",
                                "description": "Термины финансов и маркетинга.",
                                "cards": [
                                    {
                                        "front": "revenue",
                                        "back": "выручка",
                                        "example": "Revenue grew by 12% last year.",
                                    },
                                    {
                                        "front": "profit margin",
                                        "back": "рентабельность, маржа",
                                        "example": "Our profit margin is 18%.",
                                    },
                                    {
                                        "front": "budget",
                                        "back": "бюджет",
                                        "example": "The marketing budget was cut.",
                                    },
                                    {
                                        "front": "target audience",
                                        "back": "целевая аудитория",
                                        "example": "Our target audience is 25–35.",
                                    },
                                    {
                                        "front": "to launch a product",
                                        "back": "выводить продукт на рынок",
                                        "example": "We launched the app in March.",
                                    },
                                    {
                                        "front": "invoice",
                                        "back": "счёт-фактура",
                                        "example": "Please pay the invoice within 30 days.",
                                    },
                                    {
                                        "front": "stakeholder",
                                        "back": "заинтересованная сторона",
                                        "example": "We informed all stakeholders.",
                                    },
                                    {
                                        "front": "feedback",
                                        "back": "обратная связь",
                                        "example": "Customer feedback was positive.",
                                    },
                                ],
                            }
                        ],
                        "assignments": [
                            {
                                "title": "Business vocabulary in use",
                                "description": (
                                    "1. Repeat the flashcards in the trainer.\n"
                                    "2. Explain in English what these terms mean "
                                    "and give an example: revenue, profit margin, "
                                    "target audience, invoice.\n"
                                    "3. Write a short paragraph (60–80 words) "
                                    "describing a product launch using at least "
                                    "five new terms.\n\n"
                                    "Marking: definitions 8, paragraph 8, "
                                    "accuracy 4."
                                ),
                                "type": "text",
                                "max_points": 20,
                                "skills": ["vocabulary", "writing"],
                            },
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "travel-english",
        "name": "Travelling and Weather · B1",
        "summary": "Как в ProgressMe «Travelling an weather» и «English for traveling»: аэропорт, отель, описание стран (Denmark) с секциями Warm-up → Writing.",
        "level": "B1",
        "blocks": [
            {
                "name": "Travelling and Weather",
                "cefr_level": "B1",
                "description": "Let's talk about the Scandinavian countries on the example of Denmark — ProgressMe unit-style.",
                "topics": [
                    {
                        "title": "Denmark and Scandinavia",
                        "description": "Warm-up: Do you know any Scandinavian countries? Vocabulary, Reading, Listening about Denmark.",
                        "assignments": [
                            {
                                "title": "Reading: Denmark facts",
                                "description": "Read the text about Denmark and answer True/False/Not Stated.\n\n1. Denmark is the smallest Scandinavian country.\n2. The author has visited Copenhagen.\n3. Danish weather is predictable in winter.\n\nThen write 4 sentences about your country for a tourist.",
                                "type": "text",
                                "max_points": 15,
                                "skills": ["reading", "writing"],
                            },
                            {
                                "title": "Vocabulary: travel and weather",
                                "description": "Complete with the correct word.",
                                "type": "quiz",
                                "max_points": 10,
                                "skills": ["vocabulary"],
                                "questions": [
                                    {
                                        "kind": "gap",
                                        "text": "We had to ___ (to reserve) a table in advance because the restaurant was full.",
                                        "points": 2,
                                        "choices": [
                                            {"text": "book", "correct": True},
                                            {"text": "to book", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "mcq",
                                        "text": "Choose the correct collocation: to ___ a complaint",
                                        "points": 2,
                                        "choices": [
                                            {"text": "make", "correct": True},
                                            {"text": "do", "correct": False},
                                            {"text": "take", "correct": False},
                                        ],
                                    },
                                    {
                                        "kind": "order",
                                        "text": "Put the words in the correct order: never / I / have / been / to / Denmark",
                                        "points": 3,
                                        "choices": [
                                            {"text": "I", "correct": True},
                                            {"text": "have", "correct": True},
                                            {"text": "never", "correct": True},
                                            {"text": "been", "correct": True},
                                            {"text": "to", "correct": True},
                                            {"text": "Denmark", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "sort",
                                        "text": "Sort the items: countable / uncountable / both — advice, chair, information, apple",
                                        "points": 3,
                                        "choices": [
                                            {
                                                "text": "advice",
                                                "match_text": "Uncountable",
                                                "correct": True,
                                            },
                                            {
                                                "text": "chair",
                                                "match_text": "Countable",
                                                "correct": True,
                                            },
                                            {
                                                "text": "information",
                                                "match_text": "Uncountable",
                                                "correct": True,
                                            },
                                            {
                                                "text": "apple",
                                                "match_text": "Countable",
                                                "correct": True,
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                        "decks": [
                            {
                                "title": "Travel · B1",
                                "description": "Travel vocabulary from the unit.",
                                "cards": [
                                    {
                                        "front": "to book",
                                        "back": "бронировать",
                                        "example": "We booked a hotel near the harbour.",
                                    },
                                    {
                                        "front": "harbour",
                                        "back": "гавань, порт",
                                        "example": "The old harbour is full of restaurants.",
                                    },
                                    {
                                        "front": "itinerary",
                                        "back": "маршрут, план поездки",
                                        "example": "Send me your itinerary.",
                                    },
                                    {
                                        "front": "delay",
                                        "back": "задержка",
                                        "example": "The flight was delayed due to weather.",
                                    },
                                    {
                                        "front": "lighthouse",
                                        "back": "маяк",
                                        "example": "The lighthouse was built in 1850.",
                                    },
                                    {
                                        "front": "breathtaking",
                                        "back": "захватывающий",
                                        "example": "The view was breathtaking.",
                                    },
                                    {
                                        "front": "to complain",
                                        "back": "жаловаться",
                                        "example": "We complained about the cold room.",
                                    },
                                    {
                                        "front": "weather forecast",
                                        "back": "прогноз погоды",
                                        "example": "The forecast says it will rain.",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "title": "At the airport",
                        "description": "Listening and Speaking: virtual class simulation with timer and reactions.",
                        "assignments": [
                            {
                                "title": "Listening: airport announcement",
                                "description": "Listen and choose a, b, or c. You're going to listen to an interview with a pilot. Listen to Part 1 and choose a, b, or c.",
                                "type": "quiz",
                                "max_points": 8,
                                "skills": ["listening"],
                                "questions": [
                                    {
                                        "kind": "mcq",
                                        "text": "Why does the pilot love his job?",
                                        "points": 2,
                                        "choices": [
                                            {"text": "Because of the salary", "correct": True},
                                            {
                                                "text": "Because of the view and responsibility",
                                                "correct": True,
                                            },
                                            {"text": "Because he travels a lot", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "spell",
                                        "text": "Make a word from letters: a r p o r t i",
                                        "points": 2,
                                        "choices": [
                                            {"text": "airport", "correct": True},
                                            {"text": "air port", "correct": True},
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "At the Hotel",
                "cefr_level": "B1",
                "description": "Check-in, complaints, small talk with staff.",
                "topics": [
                    {
                        "title": "Hotel check-in",
                        "description": "Vocabulary, role play, writing a complaint email.",
                        "assignments": [
                            {
                                "title": "Role play: hotel problems",
                                "description": "You booked a single room but got a double, the AC doesn't work. Write a dialogue with reception (8 lines) and record it.",
                                "type": "mixed",
                                "max_points": 15,
                                "skills": ["speaking", "writing"],
                            },
                        ],
                        "decks": [
                            {
                                "title": "Hotel · Vocabulary",
                                "description": "Hotel words.",
                                "cards": [
                                    {
                                        "front": "check-in",
                                        "back": "регистрация, заселение",
                                        "example": "Check-in is at 2 p.m.",
                                    },
                                    {
                                        "front": "to complain",
                                        "back": "жаловаться",
                                        "example": "We complained about the noise.",
                                    },
                                    {
                                        "front": "facilities",
                                        "back": "удобства",
                                        "example": "The hotel facilities are excellent.",
                                    },
                                    {
                                        "front": "to upgrade",
                                        "back": "повысить класс номера",
                                        "example": "They upgraded us to a suite.",
                                    },
                                    {
                                        "front": "bill",
                                        "back": "счёт",
                                        "example": "Could I have the bill, please?",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "movie-time",
        "name": "Movie Time · A2–B1",
        "summary": "Как в ProgressMe «MOVIE TIME»: жанры, описание сюжета, рецензия с автопроверяемыми упражнениями.",
        "level": "B1",
        "blocks": [
            {
                "name": "Movie Time",
                "cefr_level": "B1",
                "description": "Popcorn, genres, plot and review — colourful class with cover illustration.",
                "topics": [
                    {
                        "title": "Genres and plot",
                        "description": "Warm-up, Vocabulary (genres), Speaking, Listening (trailer).",
                        "assignments": [
                            {
                                "title": "Vocabulary: movie genres — sort into columns",
                                "description": "Sort the films into columns: Comedy, Drama, Action, Sci-Fi",
                                "type": "quiz",
                                "max_points": 10,
                                "skills": ["vocabulary"],
                                "questions": [
                                    {
                                        "kind": "sort",
                                        "text": "Sort the titles into genres",
                                        "points": 5,
                                        "choices": [
                                            {
                                                "text": "The Big Bang Theory",
                                                "match_text": "Comedy",
                                                "correct": True,
                                            },
                                            {
                                                "text": "Interstellar",
                                                "match_text": "Sci-Fi",
                                                "correct": True,
                                            },
                                            {
                                                "text": "Titanic",
                                                "match_text": "Drama",
                                                "correct": True,
                                            },
                                            {
                                                "text": "Mission: Impossible",
                                                "match_text": "Action",
                                                "correct": True,
                                            },
                                            {
                                                "text": "Home Alone",
                                                "match_text": "Comedy",
                                                "correct": True,
                                            },
                                        ],
                                    },
                                    {
                                        "kind": "order",
                                        "text": "Put the words in order: have / you / ever / seen / this / film / ?",
                                        "points": 2,
                                        "choices": [
                                            {"text": "Have", "correct": True},
                                            {"text": "you", "correct": True},
                                            {"text": "ever", "correct": True},
                                            {"text": "seen", "correct": True},
                                            {"text": "this", "correct": True},
                                            {"text": "film", "correct": True},
                                            {"text": "?", "correct": True},
                                        ],
                                    },
                                ],
                            },
                            {
                                "title": "Write a review (120 words)",
                                "description": "Write a review of a film you have seen recently. Include: title and genre, main characters, plot (without spoilers), your opinion with reasons. Use at least 3 adjectives from the lesson.",
                                "type": "text",
                                "max_points": 20,
                                "skills": ["writing"],
                            },
                        ],
                    },
                    {
                        "title": "Watching and discussing",
                        "description": "Listening (trailer), Speaking (discussion), Writing (review).",
                        "assignments": [
                            {
                                "title": "Listening: movie trailer",
                                "description": "Watch a trailer (link) and answer: What is the main conflict? Who is the protagonist? Would you watch it? Why?",
                                "type": "text",
                                "max_points": 15,
                                "skills": ["listening", "speaking"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Cinema Vocabulary",
                "cefr_level": "A2",
                "description": "Second block for movie-time pack to satisfy library test.",
                "topics": [
                    {
                        "title": "Cinema words",
                        "description": "Vocabulary and quiz.",
                        "assignments": [
                            {
                                "title": "Quiz: cinema collocations",
                                "description": "Choose the correct verb.",
                                "type": "quiz",
                                "max_points": 6,
                                "skills": ["vocabulary"],
                                "questions": [
                                    {
                                        "kind": "mcq",
                                        "text": "to ___ a film (to watch in cinema)",
                                        "points": 2,
                                        "choices": [
                                            {"text": "see", "correct": True},
                                            {"text": "look", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "gap",
                                        "text": "The ___ (soundtrack) was composed by Hans Zimmer.",
                                        "points": 2,
                                        "choices": [{"text": "soundtrack", "correct": True}],
                                    },
                                ],
                            },
                        ],
                        "decks": [
                            {
                                "title": "Movie Time · Vocabulary",
                                "description": "Genres and cinema words.",
                                "cards": [
                                    {
                                        "front": "plot",
                                        "back": "сюжет",
                                        "example": "The plot was predictable.",
                                    },
                                    {
                                        "front": "genre",
                                        "back": "жанр",
                                        "example": "My favourite genre is comedy.",
                                    },
                                    {
                                        "front": "review",
                                        "back": "рецензия, отзыв",
                                        "example": "I read a review before watching.",
                                    },
                                    {
                                        "front": "starring",
                                        "back": "в главных ролях",
                                        "example": "Starring Tom Hanks.",
                                    },
                                    {
                                        "front": "soundtrack",
                                        "back": "саундтрек",
                                        "example": "The soundtrack is amazing.",
                                    },
                                    {
                                        "front": "to recommend",
                                        "back": "рекомендовать",
                                        "example": "I recommend this film to everyone.",
                                    },
                                    {
                                        "front": "boring",
                                        "back": "скучный",
                                        "example": "The second half was boring.",
                                    },
                                    {
                                        "front": "gripping",
                                        "back": "захватывающий",
                                        "example": "A gripping thriller.",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "it-english",
        "name": "IT English · B1–B2",
        "summary": "Для разработчиков: stand-up, code review, документация, small talk — с новыми типами упражнений.",
        "level": "B2",
        "blocks": [
            {
                "name": "IT English: Team Communication",
                "cefr_level": "B2",
                "description": "Daily stand-up, code review, writing documentation.",
                "topics": [
                    {
                        "title": "Stand-up and code review",
                        "description": "Speaking, Writing, Vocabulary.",
                        "assignments": [
                            {
                                "title": "Daily stand-up script",
                                "description": "Write your stand-up update (Yesterday / Today / Blockers) in 5–7 sentences. Use Past Simple for yesterday and will/going to for today.",
                                "type": "text",
                                "max_points": 15,
                                "skills": ["speaking", "writing"],
                            },
                            {
                                "title": "IT vocabulary — anagrams and sorting",
                                "description": "New exercise types: spell and sort.",
                                "type": "quiz",
                                "max_points": 10,
                                "skills": ["vocabulary"],
                                "questions": [
                                    {
                                        "kind": "spell",
                                        "text": "Make a word: b u g f i x",
                                        "points": 2,
                                        "choices": [
                                            {"text": "bugfix", "correct": True},
                                            {"text": "bug fix", "correct": True},
                                        ],
                                    },
                                    {
                                        "kind": "spell",
                                        "text": "Make a word: d e p l o y",
                                        "points": 2,
                                        "choices": [{"text": "deploy", "correct": True}],
                                    },
                                    {
                                        "kind": "sort",
                                        "text": "Sort into Noun / Verb",
                                        "points": 3,
                                        "choices": [
                                            {
                                                "text": "deployment",
                                                "match_text": "Noun",
                                                "correct": True,
                                            },
                                            {
                                                "text": "to deploy",
                                                "match_text": "Verb",
                                                "correct": True,
                                            },
                                            {
                                                "text": "requirement",
                                                "match_text": "Noun",
                                                "correct": True,
                                            },
                                            {
                                                "text": "to implement",
                                                "match_text": "Verb",
                                                "correct": True,
                                            },
                                        ],
                                    },
                                    {
                                        "kind": "order",
                                        "text": "Order: we / need / to / fix / this / bug / before / release",
                                        "points": 3,
                                        "choices": [
                                            {"text": "We", "correct": True},
                                            {"text": "need", "correct": True},
                                            {"text": "to", "correct": True},
                                            {"text": "fix", "correct": True},
                                            {"text": "this", "correct": True},
                                            {"text": "bug", "correct": True},
                                            {"text": "before", "correct": True},
                                            {"text": "release", "correct": True},
                                        ],
                                    },
                                ],
                            },
                        ],
                        "decks": [
                            {
                                "title": "IT English · Core",
                                "description": "Core IT vocabulary.",
                                "cards": [
                                    {
                                        "front": "to deploy",
                                        "back": "деплоить, выкатывать",
                                        "example": "We deploy on Fridays.",
                                    },
                                    {
                                        "front": "bugfix",
                                        "back": "исправление бага",
                                        "example": "This PR contains a bugfix.",
                                    },
                                    {
                                        "front": "stand-up",
                                        "back": "стендап, ежедневная встреча",
                                        "example": "Stand-up is at 10 a.m.",
                                    },
                                    {
                                        "front": "blocker",
                                        "back": "блокер, препятствие",
                                        "example": "No blockers at the moment.",
                                    },
                                    {
                                        "front": "deadline",
                                        "back": "дедлайн",
                                        "example": "We missed the deadline.",
                                    },
                                    {
                                        "front": "to review",
                                        "back": "ревьюить, проверять",
                                        "example": "Could you review my code?",
                                    },
                                    {
                                        "front": "pull request",
                                        "back": "пулл-реквест",
                                        "example": "Create a pull request.",
                                    },
                                    {
                                        "front": "to merge",
                                        "back": "смержить",
                                        "example": "We can merge after approval.",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "title": "Writing docs",
                        "description": "README, PR description, comments.",
                        "assignments": [
                            {
                                "title": "Write a README section",
                                "description": "Write Installation and Usage sections for a small library (80–100 words each). Use imperative for instructions.",
                                "type": "text",
                                "max_points": 15,
                                "skills": ["writing"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "IT Vocabulary Deep Dive",
                "cefr_level": "B1",
                "description": "Second block for IT pack.",
                "topics": [
                    {
                        "title": "Agile and Scrum",
                        "description": "Sprint, retrospective, backlog.",
                        "assignments": [
                            {
                                "title": "Retrospective notes",
                                "description": "Write 5 bullet points: what went well, what to improve, action items. Use Past Simple and should.",
                                "type": "text",
                                "max_points": 10,
                                "skills": ["writing"],
                            },
                        ],
                        "decks": [
                            {
                                "title": "Agile · Vocabulary",
                                "description": "Agile terms.",
                                "cards": [
                                    {
                                        "front": "sprint",
                                        "back": "спринт",
                                        "example": "We have a two-week sprint.",
                                    },
                                    {
                                        "front": "backlog",
                                        "back": "бэклог",
                                        "example": "Check the backlog.",
                                    },
                                    {
                                        "front": "retrospective",
                                        "back": "ретроспектива",
                                        "example": "Retro is on Friday.",
                                    },
                                    {
                                        "front": "to estimate",
                                        "back": "оценивать",
                                        "example": "We estimate in story points.",
                                    },
                                    {
                                        "front": "velocity",
                                        "back": "скорость команды",
                                        "example": "Our velocity increased.",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    },
    {
        "slug": "happy-learning",
        "name": "Happy Learning · Marathon A2",
        "summary": "Марафон 5 дней как в ProgressMe Marathon: VR, group class, словарь и геймификация без давления.",
        "level": "A2",
        "blocks": [
            {
                "name": "Happy Learning",
                "cefr_level": "A2",
                "description": "5-day marathon with daily tasks, vocabulary trainer and personal dictionary.",
                "topics": [
                    {
                        "title": "Day 1: Hello, World!",
                        "description": "Warm-up, Vocabulary, Listening.",
                        "assignments": [
                            {
                                "title": "Introduce yourself — 60 seconds",
                                "description": "Record a 60-second audio: name, where you are from, what you do, one hobby. Use Present Simple.",
                                "type": "audio",
                                "max_points": 10,
                                "skills": ["speaking"],
                            },
                        ],
                    },
                    {
                        "title": "Day 2: Group Lessons",
                        "description": "Virtual class simulation: chat, reactions, timer.",
                        "assignments": [
                            {
                                "title": "Group discussion: What makes a good online lesson?",
                                "description": "Write 80–100 words. Mention: visual, interaction, feedback, video. Use because, for example, also.",
                                "type": "text",
                                "max_points": 10,
                                "skills": ["writing"],
                            },
                        ],
                        "decks": [
                            {
                                "title": "Marathon · Day 2",
                                "description": "Words for describing lessons.",
                                "cards": [
                                    {
                                        "front": "interactive",
                                        "back": "интерактивный",
                                        "example": "Interactive exercises are fun.",
                                    },
                                    {
                                        "front": "engaging",
                                        "back": "увлекательный",
                                        "example": "An engaging teacher.",
                                    },
                                    {
                                        "front": "feedback",
                                        "back": "обратная связь",
                                        "example": "Thanks for your feedback.",
                                    },
                                    {
                                        "front": "to achieve",
                                        "back": "достигать",
                                        "example": "To achieve your goals.",
                                    },
                                    {
                                        "front": "progress",
                                        "back": "прогресс",
                                        "example": "You can track your progress.",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "title": "Day 3: VR Learning",
                        "description": "Immersive learning with VR — Happy Learning cover with VR goggles.",
                        "assignments": [
                            {
                                "title": "Describe VR lesson",
                                "description": "Have you ever tried VR? Write 60 words about how VR could help language learning.",
                                "type": "text",
                                "max_points": 10,
                                "skills": ["writing", "vocabulary"],
                            },
                        ],
                    },
                ],
            },
            {
                "name": "Marathon Results",
                "cefr_level": "A2",
                "description": "Final day and reflection.",
                "topics": [
                    {
                        "title": "Day 5: My progress",
                        "description": "Reflection and feedback.",
                        "assignments": [
                            {
                                "title": "Reflection: what I learned",
                                "description": "Write 80 words about your progress during the marathon. What was easy? What was difficult? What will you do next?",
                                "type": "text",
                                "max_points": 10,
                                "skills": ["writing"],
                            },
                        ],
                    },
                ],
            },
        ],
    },
]


# ── Доступ к библиотеке и импорт ───────────────────────────────────────────


def get_pack(slug):
    """Набор по слагу или None."""
    return next((pack for pack in COURSE_PACKS if pack["slug"] == slug), None)


def _counts(pack):
    """Сколько объектов содержит набор: для карточки в списке библиотеки."""
    blocks = pack["blocks"]
    topics = [topic for block in blocks for topic in block["topics"]]
    assignments = [item for topic in topics for item in topic.get("assignments", [])]
    return {
        "blocks": len(blocks),
        "topics": len(topics),
        "assignments": len(assignments),
        "quizzes": sum(1 for item in assignments if item["type"] == "quiz"),
        "decks": len([deck for topic in topics for deck in topic.get("decks", [])]),
        "cards": len(
            [card for topic in topics for deck in topic.get("decks", []) for card in deck["cards"]]
        ),
    }


def packs_with_state():
    """Наборы библиотеки вместе с тем, сколько из них уже есть в курсе."""
    existing_blocks = set(Block.objects.values_list("slug", flat=True))
    existing_topics = set(Topic.objects.values_list("block__slug", "slug"))
    result = []
    for pack in COURSE_PACKS:
        state = _counts(pack)
        state["imported_blocks"] = sum(
            1
            for block in pack["blocks"]
            if _prefixed(pack["slug"], block["name"]) in existing_blocks
        )
        state["imported_topics"] = sum(
            1
            for block in pack["blocks"]
            for topic in block["topics"]
            if (_prefixed(pack["slug"], block["name"]), _prefixed(pack["slug"], topic["title"]))
            in existing_topics
        )
        result.append({"pack": pack, "counts": state})
    return result


def _prefixed(pack_slug, value, limit=50):
    """Слаг с коротким префиксом набора: повторный импорт не дублирует контент."""
    prefix = pack_slug.replace("-", "")[:12]
    slug = slugify(value, allow_unicode=True)[: limit - len(prefix) - 1].strip("-")
    return f"{prefix}-{slug}" if slug else prefix


def _existing_skills(slugs):
    """Только те навыки, которые уже заведены в системе: импорт ничего не создаёт."""
    return list(Skill.objects.filter(slug__in=[slug for slug in slugs or []]))


def _create_questions(assignment, questions):
    for order, data in enumerate(questions, start=1):
        question = Question.objects.create(
            assignment=assignment,
            kind=data["kind"],
            text=data["text"],
            points=data.get("points", 1),
            explanation=data.get("explanation", ""),
            order=order,
        )
        Choice.objects.bulk_create(
            [
                Choice(
                    question=question,
                    text=choice["text"],
                    match_text=choice.get("match_text", ""),
                    is_correct=bool(choice.get("correct")),
                    order=position,
                )
                for position, choice in enumerate(data["choices"], start=1)
            ]
        )


def _create_decks(topic, decks):
    """Создать наборы карточек темы. Возвращает число созданных карточек."""
    created = 0
    for deck_data in decks:
        deck, is_new = FlashcardDeck.objects.get_or_create(
            topic=topic,
            title=deck_data["title"],
            defaults={
                "description": deck_data.get("description", ""),
                "order": topic.decks.count(),
            },
        )
        if not is_new:
            continue
        cards = [
            Flashcard(
                deck=deck,
                front=card["front"],
                back=card["back"],
                example=card.get("example", ""),
                order=position,
            )
            for position, card in enumerate(deck_data["cards"], start=1)
        ]
        Flashcard.objects.bulk_create(cards)
        created += len(cards)
    return created


@transaction.atomic
def import_course_pack(slug):
    """Скопировать набор в курс. Существующие объекты не перезаписываются.

    Возвращает счётчики созданного и пропущенного — их показывает сообщение
    после импорта. Задания создаются черновиками: публикацию преподаватель
    подтверждает сам, чтобы в курс не попал непроверенный контент.
    """
    pack = get_pack(slug)
    if pack is None:
        raise LookupError(f"Неизвестный набор: {slug}")

    created = {"blocks": 0, "topics": 0, "assignments": 0, "questions": 0, "cards": 0}
    skipped = {"blocks": 0, "topics": 0, "assignments": 0}

    block_order = Block.objects.count()
    for block_data in pack["blocks"]:
        block, is_new = Block.objects.get_or_create(
            slug=_prefixed(pack["slug"], block_data["name"]),
            defaults={
                "name": block_data["name"],
                "description": block_data.get("description", ""),
                "cefr_level": block_data.get("cefr_level", ""),
                "order": block_order,
            },
        )
        if is_new:
            block_order += 1
            created["blocks"] += 1
        else:
            skipped["blocks"] += 1

        topic_order = block.topics.count()
        for topic_data in block_data["topics"]:
            topic, is_new = Topic.objects.get_or_create(
                block=block,
                slug=_prefixed(pack["slug"], topic_data["title"]),
                defaults={
                    "title": topic_data["title"],
                    "description": topic_data.get("description", ""),
                    "order": topic_order,
                },
            )
            if is_new:
                topic_order += 1
                created["topics"] += 1
            else:
                skipped["topics"] += 1

            created["cards"] += _create_decks(topic, topic_data.get("decks", []))

            assignment_order = topic.assignments.count()
            for item in topic_data.get("assignments", []):
                if Assignment.objects.filter(topic=topic, title=item["title"]).exists():
                    skipped["assignments"] += 1
                    continue
                questions = item.get("questions", [])
                assignment = Assignment.objects.create(
                    topic=topic,
                    title=item["title"],
                    description=item["description"],
                    assignment_type=item.get("type", Assignment.Type.TEXT),
                    max_points=item.get("max_points", 10),
                    status=Assignment.Publication.DRAFT,
                    order=assignment_order,
                )
                assignment_order += 1
                created["assignments"] += 1
                skills = _existing_skills(item.get("skills"))
                if skills:
                    assignment.skills.set(skills)
                if questions:
                    _create_questions(assignment, questions)
                    created["questions"] += len(questions)
                    assignment.max_points = sum(question["points"] for question in questions)
                    assignment.save(update_fields=["max_points", "updated_at"])

    return {"created": created, "skipped": skipped}
