from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="lms/login.html"),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    path("", views.dashboard, name="dashboard"),
    path("assignments/", views.student_assignments, name="student_assignments"),
    path(
        "assignments/<int:pk>/",
        views.assignment_detail,
        name="assignment_detail",
    ),
    path(
        "teacher/submissions/",
        views.teacher_submissions,
        name="teacher_submissions",
    ),
    path(
        "teacher/submissions/<int:pk>/",
        views.teacher_submission_review,
        name="teacher_submission_review",
    ),
]