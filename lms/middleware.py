from django.conf import settings
from django.http import HttpResponse
from django.utils.deprecation import MiddlewareMixin


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
