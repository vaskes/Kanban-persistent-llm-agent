"""
Tests for the model-reachability fields and the relaxed status property.

The bug this exists to prevent: REACHABLE_WINDOW_SECONDS = 120 works for a
worker daemon that heartbeats every few seconds, but the model's probe is
a one-shot test. After 2 minutes, last_seen_at is "stale" and the column
flips to unreachable even though the model is still answering.

The model_checked_at / model_check_ok / model_check_error fields, with a
24h window, fix that — and keep the daemon heartbeat independent for cases
where both signals matter.
"""

import pytest

from board.models import Agent


pytestmark = pytest.mark.django_db


@pytest.fixture
def boot(django_user_model):
    django_user_model.objects.create_user("boot", password="pw12345")


@pytest.fixture
def llm(boot):
    return Agent.objects.create(
        name="m",
        model_provider="local_llama",
        model_name="n",
        model_base_url="http://x/v1",
    )


# --- reachability windows -------------------------------------------------


def test_unprobed_agent_is_not_reachable(llm):
    assert llm.model_reachable is False
    assert llm.status == Agent.Status.UNREACHABLE
    assert llm.is_reachable is False


def test_fresh_successful_probe_makes_the_model_reachable(llm):
    llm.model_checked_at = __import__("django").utils.timezone.now()
    llm.model_check_ok = True
    assert llm.model_reachable is True
    assert llm.status == Agent.Status.REACHABLE


def test_old_successful_probe_eventually_expires(llm):
    from datetime import timedelta
    from django.utils import timezone
    llm.model_checked_at = timezone.now() - timedelta(
        seconds=Agent.MODEL_REACHABLE_WINDOW_SECONDS + 60,
    )
    llm.model_check_ok = True
    # outside the window — must read as unreachable
    assert llm.model_reachable is False
    assert llm.status == Agent.Status.UNREACHABLE


def test_recent_failure_is_recorded_as_unreachable(llm):
    from django.utils import timezone
    llm.model_checked_at = timezone.now()
    llm.model_check_ok = False
    llm.model_check_error = "ConnectError: refused"
    assert llm.model_reachable is False
    assert llm.status == Agent.Status.UNREACHABLE


# --- the two signals stay independent ------------------------------------


def test_daemon_heartbeat_alone_makes_the_status_reachable(llm):
    """
    A model-backed agent that has its own worker daemon still uses
    last_seen_at as the 'presence' signal — its model fields are untouched.
    """
    from django.utils import timezone
    llm.last_seen_at = timezone.now()
    llm.model_checked_at = None
    assert llm.status == Agent.Status.REACHABLE
    assert llm.model_reachable is False  # model was never probed


def test_probe_alone_makes_the_status_reachable_without_heartbeat(llm):
    """A model that answered a probe is reachable even without a daemon."""
    from django.utils import timezone
    llm.last_seen_at = None
    llm.model_checked_at = timezone.now()
    llm.model_check_ok = True
    assert llm.status == Agent.Status.REACHABLE


def test_daemon_stale_but_probe_recent_still_reachable(llm):
    """The combined status follows the freshest signal, not the first one."""
    from datetime import timedelta
    from django.utils import timezone
    llm.last_seen_at = timezone.now() - timedelta(seconds=600)  # stale
    llm.model_checked_at = timezone.now()
    llm.model_check_ok = True
    assert llm.status == Agent.Status.REACHABLE


def test_daemon_recent_but_probe_failed_still_reachable(llm):
    """A live daemon's heartbeat counts even if its model is currently down."""
    from django.utils import timezone
    llm.last_seen_at = timezone.now()
    llm.model_checked_at = timezone.now()
    llm.model_check_ok = False
    llm.model_check_error = "HTTP 503"
    # daemon says "I'm here"; model says "I can't answer" — status reflects
    # the daemon signal, model_reachable reflects the model signal
    assert llm.status == Agent.Status.REACHABLE
    assert llm.model_reachable is False
