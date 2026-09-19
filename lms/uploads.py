from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class LimitedUploadHandler(FileUploadHandler):
    """Bound multipart streams as well as forms; the proxy must also cap bodies."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.bytes_received = 0

    def receive_data_chunk(self, raw_data, start):
        self.bytes_received += len(raw_data)
        total = getattr(self.request, "_lms_upload_bytes", 0) + len(raw_data)
        self.request._lms_upload_bytes = total
        if max(self.bytes_received, total) > settings.LMS_MAX_FILE_BYTES:
            self.request._lms_upload_too_large = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None
