from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views, views_student, views_teacher

urlpatterns = [
    # Аутентификация
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
    # Защищённые файлы: скачивание и inline-превью изображений/аудио
    path("files/<path:name>", views.private_file, name="private_file"),
    path("preview/<path:name>", views.media_preview, name="media_preview"),
    path("health/live/", views.health_live, name="health_live"),
    path("health/ready/", views.health_ready, name="health_ready"),
    # ── Ученик ────────────────────────────────────────────────────────────
    path("my/", views_student.student_home, name="student_home"),
    path("assignments/", views_student.student_assignments, name="student_assignments"),
    path("assignments/<int:pk>/", views_student.assignment_detail, name="assignment_detail"),
    path("assignments/<int:pk>/draft/", views_student.save_draft, name="student_save_draft"),
    path("upcoming/", views_student.upcoming, name="student_upcoming"),
    path("trainer/", views_student.trainer, name="student_trainer"),
    path("trainer/<int:pk>/", views_student.trainer_session, name="trainer_session"),
    path("grades/", views_student.student_grades, name="student_grades"),
    # ── Консоль преподавателя ─────────────────────────────────────────────
    path("teacher/", views_teacher.console_home, name="teacher_home"),
    path("teacher/review/", views_teacher.review_queue, name="teacher_review_queue"),
    path("teacher/review/<int:pk>/", views_teacher.review_detail, name="teacher_submission_review"),
    # Обратная совместимость: прежние адреса очереди и проверки
    path("teacher/submissions/", views_teacher.review_queue, name="teacher_submissions"),
    path(
        "teacher/submissions/<int:pk>/",
        views_teacher.review_detail,
        name="teacher_submission_review_legacy",
    ),
    path("teacher/curriculum/", views_teacher.curriculum, name="teacher_curriculum"),
    path("teacher/curriculum/blocks/new/", views_teacher.block_form, name="teacher_block_new"),
    path(
        "teacher/curriculum/blocks/<int:pk>/", views_teacher.block_form, name="teacher_block_edit"
    ),
    path(
        "teacher/curriculum/blocks/<int:pk>/move/",
        views_teacher.block_move,
        name="teacher_block_move",
    ),
    path(
        "teacher/curriculum/blocks/<int:pk>/delete/",
        views_teacher.block_delete,
        name="teacher_block_delete",
    ),
    path(
        "teacher/curriculum/blocks/<int:pk>/publish/",
        views_teacher.block_publish,
        name="teacher_block_publish",
    ),
    path("teacher/curriculum/topics/new/", views_teacher.topic_form, name="teacher_topic_new"),
    path(
        "teacher/curriculum/topics/<int:pk>/", views_teacher.topic_form, name="teacher_topic_edit"
    ),
    path(
        "teacher/curriculum/topics/<int:pk>/move/",
        views_teacher.topic_move,
        name="teacher_topic_move",
    ),
    path(
        "teacher/curriculum/topics/<int:pk>/delete/",
        views_teacher.topic_delete,
        name="teacher_topic_delete",
    ),
    path(
        "teacher/curriculum/topics/<int:pk>/publish/",
        views_teacher.topic_publish,
        name="teacher_topic_publish",
    ),
    path(
        "teacher/curriculum/assignments/new/",
        views_teacher.assignment_form,
        name="teacher_assignment_new",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/",
        views_teacher.assignment_form,
        name="teacher_assignment_form",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/duplicate/",
        views_teacher.assignment_duplicate,
        name="teacher_assignment_duplicate",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/publish/",
        views_teacher.assignment_publish,
        name="teacher_assignment_publish",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/delete/",
        views_teacher.assignment_delete,
        name="teacher_assignment_delete",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/preview/",
        views_teacher.assignment_preview,
        name="teacher_assignment_preview",
    ),
    path(
        "teacher/curriculum/assignments/<int:pk>/questions/",
        views_teacher.questions,
        name="teacher_questions",
    ),
    path(
        "teacher/curriculum/questions/<int:pk>/",
        views_teacher.question_form,
        name="teacher_question_edit",
    ),
    path(
        "teacher/curriculum/questions/<int:pk>/delete/",
        views_teacher.question_delete,
        name="teacher_question_delete",
    ),
    path("teacher/curriculum/decks/new/", views_teacher.deck_form, name="teacher_deck_new"),
    path("teacher/curriculum/decks/<int:pk>/", views_teacher.deck_form, name="teacher_deck_edit"),
    path(
        "teacher/curriculum/decks/<int:pk>/cards/",
        views_teacher.deck_cards,
        name="teacher_deck_cards",
    ),
    path(
        "teacher/curriculum/decks/<int:pk>/delete/",
        views_teacher.deck_delete,
        name="teacher_deck_delete",
    ),
    path(
        "teacher/curriculum/cards/<int:pk>/delete/",
        views_teacher.card_delete,
        name="teacher_card_delete",
    ),
    path("teacher/students/", views_teacher.students_list, name="teacher_students"),
    path("teacher/students/create/", views_teacher.student_create, name="teacher_student_create"),
    path("teacher/students/<int:pk>/", views_teacher.student_detail, name="teacher_student_detail"),
    path(
        "teacher/students/<int:pk>/edit/", views_teacher.student_edit, name="teacher_student_edit"
    ),
    path(
        "teacher/students/<int:pk>/delete/",
        views_teacher.student_delete,
        name="teacher_student_delete",
    ),
    path(
        "teacher/students/<int:pk>/reset-password/",
        views_teacher.student_reset_password,
        name="teacher_student_reset_password",
    ),
    path("teacher/groups/", views_teacher.groups_list, name="teacher_groups"),
    path("teacher/groups/new/", views_teacher.group_form, name="teacher_group_new"),
    path("teacher/groups/<int:pk>/", views_teacher.group_form, name="teacher_group_edit"),
    path(
        "teacher/groups/<int:pk>/delete/", views_teacher.group_delete, name="teacher_group_delete"
    ),
    path("teacher/analytics/", views_teacher.analytics, name="teacher_analytics"),
    path(
        "teacher/analytics/export.csv",
        views_teacher.analytics_export,
        name="teacher_analytics_export",
    ),
    path("teacher/settings/", views_teacher.console_settings, name="teacher_settings"),
    path("teacher/settings/snippets/new/", views_teacher.snippet_form, name="teacher_snippet_new"),
    path(
        "teacher/settings/snippets/<int:pk>/",
        views_teacher.snippet_form,
        name="teacher_snippet_edit",
    ),
    path(
        "teacher/settings/snippets/<int:pk>/delete/",
        views_teacher.snippet_delete,
        name="teacher_snippet_delete",
    ),
]
