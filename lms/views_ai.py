"""ИИ-помощник преподавателя: файл или текст → черновики курса.

Отдельный модуль представлений: логика разбора и импорта живёт в ``lms.ai``,
здесь только HTTP-обвязка, права доступа и сообщения преподавателю.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import ai
from .decorators import teacher_required
from .forms import AIMaterialForm
from .models import Assignment, Topic

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
    """Окно ИИ-помощника: загрузка материала, разбор и превью структуры."""
    mode = ai.ai_mode()
    initial = {
        "target": request.GET.get("target", "mixed"),
        "target_topic": request.GET.get("topic") or None,
    }
    form = AIMaterialForm(request.POST or None, request.FILES or None, initial=initial)
    material = request.session.get(SESSION_MATERIAL)
    meta = request.session.get(SESSION_META) or {}

    if request.method == "POST":
        if "reset" in request.POST:
            _clear_session(request)
            messages.info(request, "Черновик материала очищен.")
            return redirect("teacher_ai")
        if mode == "off":
            messages.error(request, ai.ai_mode_label(mode))
            _clear_session(request)
            material, meta = None, {}
        elif form.is_valid():
            try:
                material, meta = ai.build_material(
                    text=form.cleaned_data["text"],
                    prompt=form.cleaned_data["prompt"],
                    target=form.cleaned_data["target"],
                    upload=form.cleaned_data["upload"],
                )
            except ai.AiError as exc:
                _clear_session(request)
                messages.error(request, str(exc))
                material, meta = None, {}
            else:
                target_topic = form.cleaned_data["target_topic"]
                meta["target_topic"] = target_topic.pk if target_topic else None
                meta["target_topic_label"] = str(target_topic) if target_topic else ""
                request.session[SESSION_MATERIAL] = material
                request.session[SESSION_META] = meta
                for note in meta.get("notes") or []:
                    messages.warning(request, note)
                if meta.get("result") == "online":
                    messages.success(request, "ИИ разобрал материал — проверьте структуру ниже.")
                else:
                    messages.info(request, "Материал разобран офлайн — проверьте структуру ниже.")
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
            "limits": ai.limits(),
            "max_upload_mb": ai.max_upload_bytes() // (1024 * 1024),
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

    topic = None
    if meta.get("target_topic"):
        topic = Topic.objects.filter(pk=meta["target_topic"]).first()
    try:
        created = ai.import_material(material, target_topic=topic)
    except ai.AiError as exc:
        messages.error(request, str(exc))
        return redirect("teacher_ai")

    _clear_session(request)
    message = (
        "Черновики созданы: блоков {blocks}, глав {chapters}, тем {topics}, заданий {assignments}, "
        "вопросов {questions}, карточек {cards}.".format(
            blocks=created.get('blocks', 0),
            chapters=created.get('chapters', 0),
            topics=created.get('topics', 0),
            assignments=created.get('assignments', 0),
            questions=created.get('questions', 0),
            cards=created.get('cards', 0),
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
