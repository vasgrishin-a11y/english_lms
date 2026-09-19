from django import template

from lms.models import Submission

register = template.Library()


@register.filter
def status_label(value):
    return dict(Submission.Status.choices).get(value, "Не отправлено")
