from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

from .models import Profile


def get_user_role(user):
    profile = getattr(user, "profile", None)
    if profile:
        return profile.role
    return Profile.Role.STUDENT


def student_required(view_func):
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if get_user_role(request.user) != Profile.Role.STUDENT:
            return redirect("teacher_submissions")
        return view_func(request, *args, **kwargs)

    return _wrapped


def teacher_required(view_func):
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if get_user_role(request.user) != Profile.Role.TEACHER:
            return redirect("student_assignments")
        return view_func(request, *args, **kwargs)

    return _wrapped