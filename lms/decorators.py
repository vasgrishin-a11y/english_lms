from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import Profile


def get_user_role(user):
    if user.is_superuser:
        return Profile.Role.TEACHER
    profile = getattr(user, "profile", None)
    role = profile.role if profile else Profile.Role.STUDENT
    if role not in Profile.Role.values:
        raise PermissionDenied
    return role


def role_required(role, redirect_name):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if get_user_role(request.user) != role:
                if request.method not in {"GET", "HEAD"}:
                    raise PermissionDenied
                return redirect(redirect_name)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


student_required = role_required(Profile.Role.STUDENT, "teacher_submissions")
teacher_required = role_required(Profile.Role.TEACHER, "student_assignments")
