"""ИИ-помощник преподавателя: файл или текст → черновики курса.

Отдельный модуль представлений: логика разбора и импорта живёт в ``lms.ai``,
здесь только HTTP-обвязка, права доступа и сообщения преподавателю.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import ai
from .decorators import teacher_required
from .forms import AIMaterialForm
from .models import Topic

logger = logging.getLogger("lms.ai")

SESSION_MATERIAL = "ai_material"
SESSION_META = "ai_meta"


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
        "Черновики созданы: блоков {blocks}, тем {topics}, заданий {assignments}, "
        "вопросов {questions}, карточек {cards}.".format(**created)
    )
    if created.get("skipped"):
        message += f" Повторов пропущено: {created['skipped']}."
    messages.success(request, f"{message} Проверьте черновики и опубликуйте, когда готовы.")
    return redirect("teacher_curriculum")
