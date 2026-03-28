import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from supervisor.events import _classify_evolution_commit_kind
from supervisor.queue import _evolution_policy_snapshot


def test_classify_growth_when_tool_changes():
    kind = _classify_evolution_commit_kind(
        'v6.83.6: add new audit tool',
        ['ouroboros/tools/audit_trace.py', 'tests/test_audit_trace.py'],
    )
    assert kind == 'growth'


def test_classify_hygiene_for_release_tail_only():
    kind = _classify_evolution_commit_kind(
        'v6.83.6: resync release metadata',
        ['VERSION', 'README.md', 'pyproject.toml', 'tests/test_version_artifacts.py'],
    )
    assert kind == 'hygiene'


def test_classify_maintenance_for_non_tool_fix():
    kind = _classify_evolution_commit_kind(
        'v6.83.6: fix reflection fallback import',
        ['ouroboros/reflection.py', 'tests/test_reflection.py'],
    )
    assert kind == 'maintenance'


def test_evolution_policy_snapshot_green_only_without_blockers():
    green, blockers = _evolution_policy_snapshot({
        'budget_drift_alert': False,
        'resume_needed': False,
        'restart_notify_pending': False,
        'evolution_consecutive_failures': 0,
        'no_commit_streak': 0,
    })
    assert green is True
    assert blockers == []


def test_evolution_policy_snapshot_marks_recent_failure_as_blocker():
    green, blockers = _evolution_policy_snapshot({
        'budget_drift_alert': False,
        'resume_needed': False,
        'restart_notify_pending': False,
        'evolution_consecutive_failures': 1,
        'no_commit_streak': 0,
    })
    assert green is False
    assert 'recent_evolution_failure' in blockers
