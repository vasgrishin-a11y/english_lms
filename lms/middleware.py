from django.conf import settings
from django.http import HttpResponse
from django.utils.deprecation import MiddlewareMixin

from . import ui_text


class UploadLimitMiddleware(MiddlewareMixin):
    def process_request(self, request):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return HttpResponse("Некорректная длина запроса.", status=400)
        if length > settings.LMS_MAX_FILE_BYTES + settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            return HttpResponse("Файл слишком большой.", status=413)
        return None

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method == "POST" and request.content_type == "multipart/form-data":
            # Parse before the view can save a truncated upload as an empty answer.
            _ = request.POST
            if getattr(request, "_lms_upload_too_large", False):
                return HttpResponse("Файл слишком большой.", status=413)
        return None


class UILanguageMiddleware(MiddlewareMixin):
    """Запомнить выбранный язык интерфейса: ``?lang=eng`` плюс cookie на год.

    Параметр не делает редирект: страница сразу отрисуется на выбранном языке,
    а cookie запомнит выбор для следующих переходов и POST-запросов.
    """

    def process_request(self, request):
        requested = ui_text.normalize(request.GET.get("lang"))
        request.ui_lang = requested or ui_text.resolve(request.COOKIES.get)
        request.ui_lang_switched = bool(requested)
        return None

    def process_response(self, request, response):
        if getattr(request, "ui_lang_switched", False):
            response.set_cookie(
                ui_text.COOKIE_NAME,
                request.ui_lang,
                max_age=ui_text.COOKIE_MAX_AGE,
                samesite="Lax",
            )
        return response
