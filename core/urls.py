from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("lms.urls")),
]
# Never expose MEDIA_ROOT through django.views.static or a public reverse-proxy alias.
