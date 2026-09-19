from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

urlpatterns = [
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="lms/login.html"),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password/change/",
        auth_views.PasswordChangeView.as_view(
            template_name="lms/password_change.html",
            success_url=reverse_lazy("password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "accounts/password/changed/",
        auth_views.PasswordChangeDoneView.as_view(template_name="lms/password_change_done.html"),
        name="password_change_done",
    ),
    path("", views.dashboard, name="dashboard"),
    path("assignments/", views.student_assignments, name="student_assignments"),
    path("assignments/<int:pk>/", views.assignment_detail, name="assignment_detail"),
    path("teacher/submissions/", views.teacher_submissions, name="teacher_submissions"),
    path(
        "teacher/submissions/<int:pk>/",
        views.teacher_submission_review,
        name="teacher_submission_review",
    ),
    path("files/<path:name>", views.private_file, name="private_file"),
    path("health/live/", views.health_live, name="health_live"),
    path("health/ready/", views.health_ready, name="health_ready"),
]
