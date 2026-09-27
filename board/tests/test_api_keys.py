"""
Tests for agent API-key authentication and the agent HTTP API.

The guarantee under test: a headless worker authenticates with one header and
needs nothing else — no session, no browser, no login round trip — while the
rules it is subject to stay exactly the same as the ones a human obeys.
"""

import hashlib
import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from board.management.commands.create_api_key import mint
from board.models import AgentApiKey, Status, Task

pytestmark = pytest.mark.django_db

User = get_user_model()


_seq = {"n": 0}


def make_key(user=None, scope=AgentApiKey.Scope.WRITE, label="test"):
    if user is None:
        # unique per call: several tests need two distinct keys, and lease
        # ownership is keyed to the API key's prefix, not the account
        _seq["n"] += 1
        user = User.objects.create_user(f"worker{_seq['n']}", password="pw12345")
    token, prefix, secret_hash = mint()
    AgentApiKey.objects.create(
        prefix=prefix,
        secret_hash=secret_hash,
        label=label,
        scope=scope,
        user=user,
    )
    return token, user


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
def _grant(user):
    """Give a non-admin user read access to the default project."""
    from board.bootstrap import get_default_project
    from board.models import ProjectMembership

    ProjectMembership.objects.get_or_create(
        project=get_default_project(), user=user, defaults={"can_write": True}
    )


def place_in_default_project(**kw):
    """
    Create a task inside the default project.

    A task with no project belongs to nothing, and since task visibility was
    scoped to project access, an unprivileged key can no longer see it. Tests
    that exercise the API as a non-admin need the task to live somewhere real.
    """
    from board.bootstrap import get_default_backlog, get_default_project

    p = get_default_project()
    kw.setdefault("project", p)
    kw.setdefault("backlog", get_default_backlog(p))
    return Task.objects.create(**kw)


def _client():
    from django.test import Client

    return Client()


def json_post(client, url, payload, token):
    return client.post(
        url, data=json.dumps(payload), content_type="application/json", **auth(token)
    )


# --------------------------------------------------------------------------
# token shape and hashing
# --------------------------------------------------------------------------


def test_minted_token_has_expected_shape():
    token, prefix, secret_hash = mint()
    assert token.startswith("kb_")
    assert f"kb_{prefix}_" in token
    assert len(secret_hash) == 64
    assert token != secret_hash


def test_prefix_is_unique_per_key():
    seen = {mint()[1] for _ in range(20)}
    assert len(seen) == 20


def test_secret_is_never_stored_in_plaintext():
    token, prefix, secret_hash = mint()
    AgentApiKey.objects.create(
        prefix=prefix, secret_hash=secret_hash, user=User.objects.create_user("w")
    )
    row = AgentApiKey.objects.get(prefix=prefix)
    secret = token.split("_", 2)[2]
    assert row.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in row.secret_hash


def test_check_secret_rejects_wrong_secret():
    token, prefix, secret_hash = mint()
    k = AgentApiKey.objects.create(
        prefix=prefix,
        secret_hash=secret_hash,
        user=User.objects.create_user("w2"),
    )
    assert k.check_secret(token.split("_", 2)[2]) is True
    assert k.check_secret("wrong") is False


# --------------------------------------------------------------------------
# authentication behaviour
# --------------------------------------------------------------------------


def test_valid_token_authenticates(client):
    token, _ = make_key()
    r = client.get("/api/v1/me", **auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["acts_as"]["username"].startswith("worker")
    assert body["key_prefix"].startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f"))


def test_missing_token_is_401(client):
    assert client.get("/api/v1/me").status_code == 401


def test_malformed_token_is_401(client):
    r = client.get("/api/v1/me", HTTP_AUTHORIZATION="Bearer nonsense")
    assert r.status_code == 401


def test_wrong_secret_is_401(client):
    token, prefix, _ = mint()
    AgentApiKey.objects.create(
        prefix=prefix,
        secret_hash=hashlib.sha256(b"different").hexdigest(),
        user=User.objects.create_user("w3"),
    )
    r = client.get("/api/v1/me", **auth(token))
    assert r.status_code == 401


def test_unknown_prefix_is_401(client):
    r = client.get("/api/v1/me", **auth("kb_deadbeef_whatever"))
    assert r.status_code == 401


def test_inactive_key_is_401(client):
    token, _ = make_key()
    AgentApiKey.objects.update(is_active=False)
    assert client.get("/api/v1/me", **auth(token)).status_code == 401


def test_raw_token_scheme_also_accepted(client):
    token, _ = make_key()
    r = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Token {token}")
    assert r.status_code == 200


def test_invalid_key_and_unknown_key_are_indistinguishable(client):
    """A 401 body must not let an attacker enumerate valid prefixes."""
    make_key()
    a = client.get("/api/v1/me", HTTP_AUTHORIZATION="Bearer kb_deadbeef_x")
    b = client.get("/api/v1/me", **auth("kb_deadbeef_x"))
    assert a.json() == b.json()


def test_key_use_is_counted(client):
    token, _ = make_key()
    before = AgentApiKey.objects.first().use_count
    client.get("/api/v1/me", **auth(token))
    assert AgentApiKey.objects.first().use_count == before + 1
    assert AgentApiKey.objects.first().last_used_at is not None


def test_session_login_does_not_grant_api_access(client, django_user_model):
    """Board session must not leak into the API surface."""
    u = django_user_model.objects.create_user("op", password="pw12345")
    client.force_login(u)
    assert client.get("/api/v1/me").status_code == 401


def test_api_token_does_not_create_a_board_session(client):
    token, _ = make_key()
    client.get("/api/v1/me", **auth(token))
    assert client.session.get("_auth_user_id") is None


# --------------------------------------------------------------------------
# scope enforcement
# --------------------------------------------------------------------------


def test_read_key_cannot_claim(client):
    token, _ = make_key(scope=AgentApiKey.Scope.READ)
    place_in_default_project(
        title="t", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    r = client.post("/api/v1/tasks/claim", {}, **auth(token))
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"


def test_read_key_can_list(client):
    token, _ = make_key(scope=AgentApiKey.Scope.READ)
    place_in_default_project(title="visible", acceptance="x", status=Status.READY)
    r = client.get("/api/v1/tasks", **auth(token))
    assert r.status_code == 200
    assert r.json()["count"] == 1


# --------------------------------------------------------------------------
# the agent workflow over HTTP
# --------------------------------------------------------------------------


def test_full_claim_heartbeat_review_cycle(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="work",
        acceptance="a report exists",
        status=Status.READY,
        autonomy="AUTO",
    )

    r = client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    assert r.status_code == 200
    claimed = r.json()["task"]
    assert claimed["id"] == t.pk
    assert claimed["status"] == Status.IN_PROGRESS
    assert claimed["attempts"] == 1

    r = client.post(
        f"/api/v1/tasks/{t.pk}/heartbeat", {"step": "шаг 2/5"}, **auth(token)
    )
    assert r.status_code == 200

    t.refresh_from_db()
    assert "шаг 2/5" in t.current_step

    r = json_post(
        client,
        f"/api/v1/tasks/{t.pk}/review",
        {"evidence": {"exit_code": 0, "file": "out.md"}, "summary": "wrote report"},
        token,
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.status == Status.REVIEW
    assert t.evidence["exit_code"] == 0


def test_review_without_evidence_is_refused(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="work", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))

    r = client.post(f"/api/v1/tasks/{t.pk}/review", {"summary": "done!"}, **auth(token))
    assert r.status_code == 409
    assert r.json()["error"] == "transition_refused"
    t.refresh_from_db()
    assert t.status == Status.IN_PROGRESS


def test_claim_on_empty_queue_is_success_not_error(client):
    """A worker polling an empty board must not see a failure."""
    token, _ = make_key()
    r = client.post("/api/v1/tasks/claim", {}, **auth(token))
    assert r.status_code == 200
    assert r.json()["task"] is None


def test_claim_returns_highest_priority_first(client):
    token, _ = make_key()
    place_in_default_project(title="low", acceptance="x", status=Status.READY, priority=2)
    place_in_default_project(title="high", acceptance="x", status=Status.READY, priority=9)
    r = client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    assert r.json()["task"]["title"] == "high"


def test_manual_card_is_not_claimable_over_api(client):
    token, _ = make_key()
    place_in_default_project(
        title="manual",
        acceptance="x",
        status=Status.READY,
        autonomy="MANUAL",
    )
    r = client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    assert r.json()["task"] is None


def test_card_with_unmet_dependency_is_not_claimable(client):
    token, _ = make_key()
    from board.models import TaskDep

    blocker = place_in_default_project(
        title="blocker", acceptance="x", status=Status.IN_PROGRESS
    )
    waiter = place_in_default_project(
        title="waiter", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    TaskDep.objects.create(task=waiter, depends_on=blocker)
    r = client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    assert r.json()["task"] is None


def test_only_lease_holder_may_heartbeat(client):
    """
    A second key that can *see* the card but does not hold its lease gets 403.

    403, not 404: the caller is allowed to know the card exists, it just may not
    act on it. A key that cannot see the card at all gets 404 — see
    test_task_visibility.py, which covers that boundary.
    """
    from board.models import ProjectMembership

    t1, u1 = make_key(label="one")
    t2, u2 = make_key(label="two")
    _grant(u2)
    task = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(t1))
    r = client.post(f"/api/v1/tasks/{task.pk}/heartbeat", {"step": "x"}, **auth(t2))
    assert r.status_code == 403


def test_only_lease_holder_may_submit_review(client):
    from board.models import ProjectMembership

    t1, u1 = make_key(label="one")
    t2, u2 = make_key(label="two")
    _grant(u2)
    task = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(t1))
    r = client.post(
        f"/api/v1/tasks/{task.pk}/review", {"evidence": {"ok": 1}}, **auth(t2)
    )
    assert r.status_code == 403


def test_release_returns_card_to_ready(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/release", {"reason": "not ready yet"}, **auth(token)
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.status == Status.READY
    assert t.claimed_by == ""


def test_release_rejects_absurd_target(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/release", {"status": "DONE"}, **auth(token)
    )
    assert r.status_code == 400


def test_escalate_requires_a_reason(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    r = client.post(f"/api/v1/tasks/{t.pk}/escalate", {}, **auth(token))
    assert r.status_code == 400


def test_escalate_flags_the_card_for_the_operator(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/escalate",
        {"reason": "cannot tell which style is intended"},
        **auth(token),
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.status == Status.NEEDS_HUMAN
    assert t.needs_human is True
    assert "style" in t.attention_reason


def test_tokens_used_is_recorded_from_the_worker(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w",
        acceptance="x",
        status=Status.READY,
        autonomy="AUTO",
        max_tokens=1000,
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    client.post(
        f"/api/v1/tasks/{t.pk}/review",
        {"evidence": {"ok": 1}, "tokens_used": 250},
        **auth(token),
    )
    t.refresh_from_db()
    assert t.tokens_used == 250


def test_get_task_returns_audit_trail(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    r = client.get(f"/api/v1/tasks/{t.pk}", **auth(token))
    body = r.json()["task"]
    assert body["id"] == t.pk
    assert any(e["event"] == "claim" for e in body["events"])


def test_board_state_counts(client):
    token, _ = make_key()
    place_in_default_project(title="a", acceptance="x", status=Status.READY)
    place_in_default_project(title="b", acceptance="x", status=Status.BACKLOG)
    r = client.get("/api/v1/board", **auth(token))
    body = r.json()
    assert body["by_status"][Status.READY] == 1
    assert body["by_status"][Status.BACKLOG] == 1


def test_list_filters_by_status(client):
    token, _ = make_key()
    place_in_default_project(title="a", acceptance="x", status=Status.READY)
    place_in_default_project(title="b", acceptance="x", status=Status.DONE)
    r = client.get(f"/api/v1/tasks?status={Status.READY}", **auth(token))
    assert [t["title"] for t in r.json()["tasks"]] == ["a"]


def test_list_limit_is_capped(client):
    token, _ = make_key()
    for i in range(3):
        place_in_default_project(title=f"t{i}", acceptance="x", status=Status.READY)
    r = client.get("/api/v1/tasks?limit=2", **auth(token))
    assert r.json()["count"] == 2


def test_missing_task_returns_404(client):
    token, _ = make_key()
    assert client.get("/api/v1/tasks/nope", **auth(token)).status_code == 404


def test_csrf_token_not_required_for_api(client):
    """
    Token auth is non-ambient, so CSRF does not apply. If a future refactor
    puts these views behind session auth, this test is the thing that should
    start failing.
    """
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    # no CSRF cookie, no CSRF header, no Referer
    r = client.post("/api/v1/tasks/claim", {"worker": "w1"}, **auth(token))
    assert r.status_code == 200


def test_board_ui_still_requires_session(client, django_user_model):
    """Adding API auth must not accidentally open the board."""
    token, _ = make_key()
    r = client.get("/", **auth(token))
    assert r.status_code == 302


def test_claim_route_is_not_shadowed_by_task_id_pattern():
    """
    Regression guard for URL ordering.

    'api/v1/tasks/claim' and 'api/v1/tasks/<str:task_id>' overlap, and Django
    matches in declaration order. When the parameterised pattern came first,
    'claim' was captured as a task id and every claim returned 405.
    """
    from django.urls import resolve

    assert resolve("/api/v1/tasks/claim").url_name == "api_claim"
    assert resolve("/api/v1/tasks/abc123").url_name == "api_get_task"
    assert resolve("/api/v1/tasks/abc123/review").url_name == "api_review"


# --------------------------------------------------------------------------
# body parsing — agents send JSON, operators type curl -d
# --------------------------------------------------------------------------


def test_json_body_is_accepted(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = json_post(client, f"/api/v1/tasks/{t.pk}/heartbeat", {"step": "json step"}, token)
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.current_step == "json step"


def test_form_body_is_accepted(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = client.post(f"/api/v1/tasks/{t.pk}/heartbeat", {"step": "form step"}, **auth(token))
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.current_step == "form step"


def test_malformed_json_body_is_ignored_not_crashed(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/heartbeat",
        data="{not json",
        content_type="application/json",
        **auth(token),
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.current_step == ""


def test_json_review_with_evidence_over_http(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = json_post(
        client,
        f"/api/v1/tasks/{t.pk}/review",
        {"evidence": {"exit_code": 0, "path": "/tmp/out.md"}, "summary": "ok"},
        token,
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.status == Status.REVIEW
    assert t.evidence["exit_code"] == 0


def test_caller_supplied_worker_identity_is_ignored(client):
    """
    Worker identity must come from the key, not the request body.

    Lease ownership is enforced by comparing claimed_by with api:<key prefix>.
    Accepting a caller-supplied worker name would let a key claim a card under
    another identity and make the ownership check pass for the wrong key.
    """
    token, _ = make_key()
    place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    r = client.post(
        "/api/v1/tasks/claim", {"worker": "someone-else"}, **auth(token)
    )
    assert r.status_code == 200
    claimed_by = r.json()["task"]["claimed_by"]
    assert claimed_by.startswith("api:")
    assert "someone-else" not in claimed_by


def test_evidence_may_be_a_json_string_in_a_form_body(client):
    """
    Form-encoded bodies cannot nest, so evidence is accepted as a JSON string.
    Anything unparseable counts as no evidence — the state machine must not
    receive a Python repr and mistake it for proof.
    """
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/review",
        {"evidence": json.dumps({"exit_code": 0, "path": "/tmp/x"})},
        **auth(token),
    )
    assert r.status_code == 200
    t.refresh_from_db()
    assert t.evidence["exit_code"] == 0
    assert t.evidence["path"] == "/tmp/x"


def test_unparseable_evidence_string_is_refused_as_missing(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/review",
        {"evidence": "{'exit_code': 0}"},  # python repr, not json
        **auth(token),
    )
    assert r.status_code == 409
    t.refresh_from_db()
    assert t.status == Status.IN_PROGRESS


def test_evidence_json_string_that_is_not_an_object_is_refused(client):
    token, _ = make_key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    client.post("/api/v1/tasks/claim", {}, **auth(token))
    r = client.post(
        f"/api/v1/tasks/{t.pk}/review", {"evidence": "[1,2,3]"}, **auth(token)
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------
# the minting command
# --------------------------------------------------------------------------


def test_create_api_key_command_mints_a_usable_key():
    from django.core.management import call_command

    User.objects.create_user("runner", password="pw12345")
    out = StringIO()
    call_command("create_api_key", "--user", "runner", "--label", "dreamline",
                 stdout=out)
    text = out.getvalue()
    token = next(
        line.strip() for line in text.splitlines() if line.strip().startswith("kb_")
    )
    assert "shown once" in text

    # and it actually works against the API
    r = _client().get("/api/v1/me", **auth(token))
    assert r.status_code == 200
    assert r.json()["acts_as"]["username"] == "runner"


def test_create_api_key_command_rejects_unknown_user():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="no such user"):
        call_command("create_api_key", "--user", "ghost", stdout=StringIO())


def test_create_api_key_command_rejects_inactive_user():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    User.objects.create_user("dead", password="pw12345", is_active=False)
    with pytest.raises(CommandError, match="inactive"):
        call_command("create_api_key", "--user", "dead", stdout=StringIO())


def test_create_api_key_command_rejects_duplicate_prefix():
    from django.core.management import call_command
    from django.core.management.base import CommandError

    User.objects.create_user("a", password="pw12345")
    call_command("create_api_key", "--user", "a", "--prefix", "fixed", stdout=StringIO())
    with pytest.raises(CommandError, match="already in use"):
        call_command("create_api_key", "--user", "a", "--prefix", "fixed", stdout=StringIO())


def test_create_api_key_command_honours_read_scope():
    from django.core.management import call_command

    User.objects.create_user("ro", password="pw12345")
    out = StringIO()
    call_command("create_api_key", "--user", "ro", "--scope", "read", stdout=out)
    assert "read" in out.getvalue()
    assert AgentApiKey.objects.get().scope == AgentApiKey.Scope.READ


def test_api_key_str_and_can_write():
    user = User.objects.create_user("k", password="pw12345")
    token, prefix, secret_hash = mint()
    k = AgentApiKey.objects.create(
        prefix=prefix, secret_hash=secret_hash, user=user,
        label="dreamline regen", scope=AgentApiKey.Scope.WRITE,
    )
    assert prefix in str(k)
    assert "dreamline regen" in str(k)
    assert k.can_write is True
    k.scope = AgentApiKey.Scope.READ
    assert k.can_write is False


def test_unlabelled_key_string_still_reads():
    user = User.objects.create_user("k2", password="pw12345")
    token, prefix, secret_hash = mint()
    k = AgentApiKey.objects.create(prefix=prefix, secret_hash=secret_hash, user=user)
    assert "unlabelled" in str(k)
