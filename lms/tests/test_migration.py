import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class LegacyMigrationTests(SimpleTestCase):
    def run_upgrade(self, grade):
        with tempfile.TemporaryDirectory(prefix="lms-migration-") as directory:
            env = os.environ.copy()
            env.update(
                {
                    "DJANGO_SETTINGS_MODULE": "core.settings",
                    "DJANGO_DEBUG": "True",
                    "DATABASE_URL": f"sqlite:///{directory}/legacy.sqlite3",
                    "DJANGO_MEDIA_ROOT": directory,
                }
            )
            script = """
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
call_command('migrate', 'lms', '0001', verbosity=0)
apps = MigrationExecutor(connection).loader.project_state([('lms', '0001_initial')]).apps
User, Block, Topic, Assignment, Submission, Feedback = [apps.get_model(*name.split('.')) for name in ['auth.User', 'lms.Block', 'lms.Topic', 'lms.Assignment', 'lms.Submission', 'lms.Feedback']]
user = User.objects.create(username='legacy_student')
block = Block.objects.create(name='Block', slug='block')
topic = Topic.objects.create(block=block, title='Topic', slug='topic')
assignment = Assignment.objects.create(topic=topic, title='Old task', description='Task', max_points=40)
attempt = Submission.objects.create(student=user, assignment=assignment, text_answer='Preserve this answer', file_answer='submissions/legacy.txt', status='needs_revision')
feedback = Feedback.objects.create(submission=attempt, grade=GRADE, comment='Preserve feedback')
old_time = attempt.submitted_at
try:
    call_command('migrate', 'lms', verbosity=0)
except RuntimeError:
    assert GRADE > 40
    assert Feedback.objects.get(pk=feedback.pk).grade == GRADE
    print('invalid-grade-preserved-and-upgrade-rejected')
else:
    assert GRADE <= 40
    from lms.models import Submission as NewSubmission
    from lms.models import Feedback as NewFeedback
    from lms.models import Topic as NewTopic, Chapter as NewChapter
    migrated_topic = NewTopic.objects.get(pk=topic.pk)
    assert migrated_topic.chapter.slug == NewChapter.DEFAULT_SLUG and migrated_topic.chapter.block_id == block.pk
    assert migrated_topic.chapter.title == NewChapter.DEFAULT_TITLE
    current = NewSubmission.objects.get(pk=attempt.pk)
    assert current.version == 1 and current.max_points_snapshot == 40
    assert current.text_answer == 'Preserve this answer' and current.file_answer.name == 'submissions/legacy.txt'
    assert current.submitted_at == old_time and current.review_revision == 1
    assert current.events.count() == 2
    assert NewFeedback.objects.get(pk=feedback.pk).decision == 'needs_revision'
    print('legacy-data-preserved')
""".replace("GRADE", str(grade))
            return subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

    def test_upgrade_preserves_ids_files_answers_feedback_and_times(self):
        result = self.run_upgrade(30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy-data-preserved", result.stdout)

    def test_upgrade_never_silently_clamps_invalid_legacy_grades(self):
        result = self.run_upgrade(50)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("invalid-grade-preserved-and-upgrade-rejected", result.stdout)


class AudienceMigrationTests(SimpleTestCase):
    """0012: одиночная «Группа» задания переезжает в множественные «Группы» без потерь."""

    def test_assignment_group_is_copied_into_groups(self):
        with tempfile.TemporaryDirectory(prefix="lms-migration-") as directory:
            env = os.environ.copy()
            env.update(
                {
                    "DJANGO_SETTINGS_MODULE": "core.settings",
                    "DJANGO_DEBUG": "True",
                    "DATABASE_URL": f"sqlite:///{directory}/audience.sqlite3",
                    "DJANGO_MEDIA_ROOT": directory,
                }
            )
            script = """
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
call_command('migrate', 'lms', '0011', verbosity=0)
apps = MigrationExecutor(connection).loader.project_state([('lms', '0011_chapter_and_issued_password')]).apps
Block, Chapter, Topic, Assignment, Group = [apps.get_model('lms', n) for n in ['Block', 'Chapter', 'Topic', 'Assignment', 'Group']]
block = Block.objects.create(name='OGE', slug='oge')
chapter = Chapter.objects.create(block=block, title='General', slug='general')
topic = Topic.objects.create(block=block, chapter=chapter, title='Topic', slug='topic')
group = Group.objects.create(name='OGE-A', slug='oge-a')
restricted = Assignment.objects.create(topic=topic, title='Restricted', description='x', max_points=10, group=group)
open_task = Assignment.objects.create(topic=topic, title='Open', description='x', max_points=10)
call_command('migrate', 'lms', verbosity=0)
from lms.models import Assignment as NewAssignment, Block as NewBlock
new_restricted = NewAssignment.objects.get(pk=restricted.pk)
assert list(new_restricted.groups.values_list('slug', flat=True)) == ['oge-a']
assert not NewAssignment.objects.get(pk=open_task.pk).groups.exists()
assert not NewBlock.objects.get(pk=block.pk).groups.exists()
assert not hasattr(new_restricted, 'group_id')
print('assignment-group-migrated')
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("assignment-group-migrated", result.stdout)
