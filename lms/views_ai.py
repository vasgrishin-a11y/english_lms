"""ИИ-помощник преподавателя: файл или текст → черновики курса.

Отдельный модуль представлений: логика разбора и импорта живёт в ``lms.ai``,
здесь только HTTP-обвязка, права доступа и сообщения преподавателю.
"""

import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import ai
from .decorators import teacher_required
from .forms import AIMaterialForm
from .models import Assignment, Chapter, Submission, Topic

logger = logging.getLogger("lms.ai")

SESSION_MATERIAL = "ai_material"
SESSION_META = "ai_meta"
SESSION_REVISION = "ai_revision"


def _clear_session(request):
    request.session.pop(SESSION_MATERIAL, None)
    request.session.pop(SESSION_META, None)


@teacher_required
@require_http_methods(["GET", "POST"])
def ai_assistant(request):
    """Окно ИИ-помощника: загрузка, подробный предпросмотр и правка до импорта."""
    mode = ai.ai_mode()
    available_targets = {value for value, _label in ai.TARGETS}
    requested_target = request.GET.get("target", "mixed")
    initial = {
        "target": requested_target if requested_target in available_targets else "mixed",
        "structure_topic": request.GET.get("topic") or None,
    }
    initial_topic = None
    if initial["structure_topic"]:
        initial_topic = (
            Topic.objects.select_related("block", "chapter")
            .filter(pk=initial["structure_topic"])
            .first()
        )
        if initial_topic:
            initial["structure_block"] = initial_topic.block_id
            initial["structure_chapter"] = initial_topic.chapter_id
    revising = request.method == "POST" and "revise_material" in request.POST
    form = AIMaterialForm(
        None if revising else request.POST or None,
        None if revising else request.FILES or None,
        initial=initial,
    )
    material = request.session.get(SESSION_MATERIAL)
    meta = request.session.get(SESSION_META) or {}
    revision_instruction = meta.get("revision_instruction", "")

    if request.method == "POST":
        if "reset" in request.POST:
            _clear_session(request)
            messages.info(request, "Черновик материала очищен.")
            return redirect("teacher_ai")
        if revising:
            instruction = request.POST.get("revision_instruction", "").strip()[:4000]
            revision_instruction = instruction
            if mode != "online":
                messages.error(
                    request,
                    "Правка текущей версии доступна, когда подключена модель. "
                    "Офлайн-разбор умеет только собирать новый материал.",
                )
            elif not material:
                messages.error(request, "Текущая версия не найдена — соберите материал заново.")
            elif not instruction:
                messages.error(request, "Опишите, что изменить в текущей версии материала.")
            else:
                try:
                    revised, revision_meta = ai.revise_material(
                        material, instruction, structure=meta.get("structure")
                    )
                except ai.AiError as exc:
                    messages.error(request, str(exc))
                else:
                    material = revised
                    meta.update(revision_meta)
                    try:
                        revision_count = int(meta.get("revision_count", 0))
                    except (TypeError, ValueError):
                        revision_count = 0
                    meta["revision_count"] = revision_count + 1
                    meta["revision_instruction"] = instruction
                    request.session[SESSION_MATERIAL] = material
                    request.session[SESSION_META] = meta
                    messages.success(
                        request,
                        "ИИ обновил текущую версию — проверьте подробный предпросмотр. "
                        "В курс пока ничего не импортировано.",
                    )
        elif mode == "off":
            messages.error(request, ai.ai_mode_label(mode))
            _clear_session(request)
            material, meta = None, {}
        elif form.is_valid():
            try:
                uploads = request.FILES.getlist("upload")
                prompt = form.cleaned_data["prompt"]
                if form.cleaned_data.get("text"):
                    prompt = (prompt + "\n\n" + form.cleaned_data["text"]).strip()
                # Все выбранные файлы сохраняются в одном запросе: первый идёт
                # как вложение провайдеру, остальные добавляются извлечённым текстом.
                # Это не позволяет второму выбору затереть первый и работает также
                # для PDF/DOCX/XLSX в офлайн-режиме.
                for extra in uploads[1:]:
                    try:
                        extra_text = ai.extract_text(extra.name, extra.read())
                    except ai.AiError as exc:
                        prompt += f"\n\nМатериал из файла {extra.name}: {exc}"
                    else:
                        prompt += f"\n\nМатериал из файла {extra.name}:\n{extra_text}"
                material, meta = ai.build_material(
                    text="",
                    prompt=prompt,
                    target=form.cleaned_data.get("target") or "mixed",
                    upload=uploads[0] if uploads else None,
                    structure=form.structure(),
                )
            except ai.AiError as exc:
                _clear_session(request)
                messages.error(request, str(exc))
                material, meta = None, {}
            else:
                request.session[SESSION_MATERIAL] = material
                request.session[SESSION_META] = meta
                for note in meta.get("notes") or []:
                    messages.warning(request, note)
                if meta.get("result") == "online":
                    messages.success(
                        request, "ИИ разобрал материал — проверьте подробный предпросмотр ниже."
                    )
                else:
                    messages.info(
                        request, "Материал разобран офлайн — проверьте подробный предпросмотр ниже."
                    )
        else:
            material, meta = None, {}

    summary = ai.material_summary(material) if material else None
    return render(
        request,
        "lms/teacher_ai.html",
        {
            "form": form,
            "mode": mode,
            "mode_label": ai.ai_mode_label(mode),
            "provider": ai.provider_spec(),
            "provider_hint": ai.provider_hint(),
            "material": material,
            "meta": meta,
            "summary": summary,
            "revision_instruction": revision_instruction,
            "limits": ai.limits(),
            "max_upload_mb": ai.max_upload_bytes() // (1024 * 1024),
            "structure_tree": {
                "chapters": {
                    str(pk): block_id
                    for pk, block_id in Chapter.objects.filter(
                        is_active=True, block__is_active=True
                    ).values_list("pk", "block_id")
                },
                "topics": {
                    str(pk): {"block": block_id, "chapter": chapter_id}
                    for pk, block_id, chapter_id in Topic.objects.filter(
                        is_active=True, chapter__is_active=True, block__is_active=True
                    ).values_list("pk", "block_id", "chapter_id")
                },
            },
            "workspace": "ai",
        },
    )


@teacher_required
@require_POST
def ai_import(request):
    """Импорт разобранного материала: только черновики, публикует человек."""
    material = request.session.get(SESSION_MATERIAL)
    meta = request.session.get(SESSION_META) or {}
    if not material:
        messages.error(request, "Материал не найден: загрузите файл заново.")
        return redirect("teacher_ai")

    try:
        created = ai.import_material(material, structure=meta.get("structure"))
    except ai.AiError as exc:
        messages.error(request, str(exc))
        return redirect("teacher_ai")

    _clear_session(request)
    message = (
        "Черновики созданы: блоков {blocks}, глав {chapters}, тем {topics}, заданий {assignments}, "
        "вопросов {questions}, карточек {cards}.".format(
            blocks=created.get("blocks", 0),
            chapters=created.get("chapters", 0),
            topics=created.get("topics", 0),
            assignments=created.get("assignments", 0),
            questions=created.get("questions", 0),
            cards=created.get("cards", 0),
        )
    )
    if created.get("skipped"):
        message += f" Повторов пропущено: {created['skipped']}."
    messages.success(request, f"{message} Проверьте черновики и опубликуйте, когда готовы.")
    return redirect("teacher_curriculum")


@teacher_required
@require_http_methods(["GET", "POST"])
def assignment_ai(request, pk):
    """Правка готового задания с ИИ: инструкция → версия-предложение к применению."""
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block", "topic__chapter"), pk=pk
    )
    mode = ai.ai_mode()
    stored = request.session.get(SESSION_REVISION)
    if stored and stored.get("assignment") != assignment.pk:
        stored = None
    revision = stored.get("revision") if stored else None
    instruction = stored.get("instruction", "") if stored else ""

    if request.method == "POST":
        if "reset" in request.POST:
            request.session.pop(SESSION_REVISION, None)
            messages.info(request, "Версия ИИ сброшена — задание не менялось.")
            return redirect("teacher_assignment_ai", pk=assignment.pk)
        instruction = request.POST.get("instruction", "").strip()[:4000]
        if mode != "online":
            messages.error(
                request,
                "Правка с ИИ доступна, когда подключена модель. Сейчас работает только "
                "офлайн-разбор новых материалов — измените задание вручную.",
            )
        elif not instruction:
            messages.error(request, "Опишите, что изменить: например, «сделай вопросы сложнее».")
        else:
            try:
                revision, _meta = ai.revise_assignment(assignment, instruction)
            except ai.AiError as exc:
                messages.error(request, str(exc))
                revision = None
            else:
                request.session[SESSION_REVISION] = {
                    "assignment": assignment.pk,
                    "instruction": instruction,
                    "revision": revision,
                }
                messages.success(
                    request, "ИИ подготовил новую версию — проверьте её и примените внизу."
                )

    return render(
        request,
        "lms/teacher_assignment_ai.html",
        {
            "assignment": assignment,
            "mode": mode,
            "mode_label": ai.ai_mode_label(mode),
            "provider": ai.provider_spec(),
            "provider_hint": ai.provider_hint(),
            "revision": revision,
            "instruction": instruction,
            "question_count": assignment.questions.count() if assignment.is_quiz else 0,
            "card_count": assignment.cards.count() if assignment.is_flashcards else 0,
            "submissions_count": assignment.submissions.count(),
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def assignment_ai_apply(request, pk):
    """Применить версию из предпросмотра: заменить содержимое задания."""
    assignment = get_object_or_404(Assignment, pk=pk)
    stored = request.session.get(SESSION_REVISION)
    if not stored or stored.get("assignment") != assignment.pk:
        messages.error(request, "Нет подготовленной версии — сгенерируйте её заново.")
        return redirect("teacher_assignment_ai", pk=pk)
    try:
        summary = ai.apply_revision(assignment, stored["revision"])
    except ai.AiError as exc:
        messages.error(request, str(exc))
        return redirect("teacher_assignment_ai", pk=pk)
    request.session.pop(SESSION_REVISION, None)
    parts = [f"Задание обновлено по версии ИИ: {assignment.title}."]
    if summary["questions"]:
        parts.append(f"Вопросов теперь: {summary['questions']}.")
    if summary["cards"]:
        parts.append(f"Карточек теперь: {summary['cards']}.")
    messages.success(request, " ".join(parts))
    return redirect("teacher_assignment_form", pk=pk)


def _is_latest_attempt(submission):
    return not Submission.objects.filter(
        student_id=submission.student_id,
        assignment_id=submission.assignment_id,
        version__gt=submission.version,
    ).exists()


@teacher_required
@require_POST
def review_ai_grade_text(request, pk):
    """ИИ проверяет свободный текстовый ответ ученика: балл, комментарий, подсказки в тексте.

    Результат не сохраняется сам по себе — он приходит в интерфейс проверки, где
    преподаватель может его поправить и сохранить обычной кнопкой «Сохранить проверку».
    """
    submission = get_object_or_404(
        Submission.objects.select_related("assignment__topic__block", "assignment__topic__chapter"),
        pk=pk,
    )
    if not _is_latest_attempt(submission):
        return JsonResponse(
            {"ok": False, "error": "Это не последняя попытка — откройте актуальную."}, status=409
        )
    try:
        result = ai.grade_text_answer(submission)
    except ai.AiError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    logger.info("ai_grade_text submission=%s teacher=%s", submission.pk, request.user.pk)
    return JsonResponse({"ok": True, **result})


@teacher_required
@require_POST
def review_ai_grade_audio(request, pk):
    """ИИ прослушивает и проверяет аудиоответ ученика, если модель это умеет."""
    submission = get_object_or_404(
        Submission.objects.select_related("assignment__topic__block", "assignment__topic__chapter"),
        pk=pk,
    )
    if not _is_latest_attempt(submission):
        return JsonResponse(
            {"ok": False, "error": "Это не последняя попытка — откройте актуальную."}, status=409
        )
    try:
        result = ai.grade_audio_answer(submission)
    except ai.AiError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    logger.info("ai_grade_audio submission=%s teacher=%s", submission.pk, request.user.pk)
    return JsonResponse({"ok": True, **result})
