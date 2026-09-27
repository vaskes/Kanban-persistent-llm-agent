"""
Coverage for the API's error and edge branches.

These are the ways a request can be refused, and they matter more than the
happy path: an agent that gets a 500 or an opaque 403 will retry forever or
give up silently, whereas a 403 that says "read-only key" is actionable.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from board.auth_api import InvalidKey, _lookup, extract_secret, user_for_key
from board.management.commands.create_api_key import mint
from board.models import AgentApiKey, Status, Task
from board.models import TaskDep

pytestmark = pytest.mark.django_db

User = get_user_model()

_seq = {"n": 0}


def _key(scope=AgentApiKey.Scope.WRITE):
    _seq["n"] += 1
    user = User.objects.create_user(f"w{_seq['n']}", password="pw12345")
    token, prefix, secret_hash = mint()
    AgentApiKey.objects.create(
        prefix=prefix, secret_hash=secret_hash, user=user, scope=scope
    )
    return token


def _auth(t):
    return {"HTTP_AUTHORIZATION": f"Bearer {t}"}


def _key_user(token):
    from board.models import AgentApiKey

    return AgentApiKey.objects.get(
        prefix=token.split("_", 2)[1]
    ).user


def _grant_access(token):
    """Give the key's account read access to the default project."""
    from board.bootstrap import get_default_project
    from board.models import ProjectMembership

    ProjectMembership.objects.get_or_create(
        project=get_default_project(),
        user=_key_user(token),
        defaults={"can_write": True},
    )


def place_in_default_project(**kw):
    """Create a task inside the default project (see test_api_keys.py)."""
    from board.bootstrap import get_default_backlog, get_default_project

    p = get_default_project()
    kw.setdefault("project", p)
    kw.setdefault("backlog", get_default_backlog(p))
    return Task.objects.create(**kw)


# --------------------------------------------------------------------------
# header extraction
# --------------------------------------------------------------------------


def test_extract_secret_handles_both_schemes():
    rf = RequestFactory()
    assert extract_secret(rf.get("/x", HTTP_AUTHORIZATION="Bearer abc")) == "abc"
    assert extract_secret(rf.get("/x", HTTP_AUTHORIZATION="Token abc")) == "abc"


def test_extract_secret_returns_none_without_header():
    assert extract_secret(RequestFactory().get("/x")) is None


def test_extract_secret_returns_none_for_empty_value():
    rf = RequestFactory()
    assert extract_secret(rf.get("/x", HTTP_AUTHORIZATION="Bearer ")) is None
    assert extract_secret(rf.get("/x", HTTP_AUTHORIZATION="Bearer")) is None


def test_extract_secret_ignores_unknown_scheme():
    rf = RequestFactory()
    assert extract_secret(rf.get("/x", HTTP_AUTHORIZATION="Basic abc")) is None


def test_token_with_wrong_segment_count_is_rejected():
    with pytest.raises(InvalidKey, match="format"):
        _lookup("kb_short")


def test_token_with_empty_prefix_is_rejected():
    with pytest.raises(InvalidKey, match="format"):
        _lookup("kb__secret")


def test_token_with_empty_secret_is_rejected():
    with pytest.raises(InvalidKey, match="format"):
        _lookup("kb_prefix_")


def test_token_without_kb_prefix_is_rejected():
    with pytest.raises(InvalidKey, match="format"):
        _lookup("nope_abc_def")


def test_inactive_bound_account_is_refused():
    user = User.objects.create_user("gone", password="pw12345", is_active=False)
    _, prefix, secret_hash = mint()
    key = AgentApiKey.objects.create(prefix=prefix, secret_hash=secret_hash, user=user)
    with pytest.raises(InvalidKey, match="inactive"):
        user_for_key(key)


def test_user_for_key_returns_the_account():
    user = User.objects.create_user("live", password="pw12345")
    _, prefix, secret_hash = mint()
    key = AgentApiKey.objects.create(prefix=prefix, secret_hash=secret_hash, user=user)
    assert user_for_key(key) == user


# --------------------------------------------------------------------------
# authentication is enforced on every endpoint
# --------------------------------------------------------------------------

ENDPOINTS = [
    ("get", "/api/v1/me"),
    ("get", "/api/v1/tasks"),
    ("get", "/api/v1/board"),
    ("get", "/api/v1/tasks/abc"),
    ("post", "/api/v1/tasks/claim"),
    ("post", "/api/v1/tasks/abc/heartbeat"),
    ("post", "/api/v1/tasks/abc/review"),
    ("post", "/api/v1/tasks/abc/release"),
    ("post", "/api/v1/tasks/abc/escalate"),
]


@pytest.mark.parametrize("method,url", ENDPOINTS)
def test_every_endpoint_refuses_anonymous(method, url):
    r = getattr(Client(), method)(url, {})
    assert r.status_code == 401, f"{method.upper()} {url} was not refused"


@pytest.mark.parametrize("method,url", ENDPOINTS)
def test_every_endpoint_refuses_a_read_only_key(method, url):
    token = _key(scope=AgentApiKey.Scope.READ)
    r = getattr(Client(), method)(url, {}, **_auth(token))
    if method == "get":
        assert r.status_code in (200, 404), url
    else:
        assert r.status_code == 403, url


# --------------------------------------------------------------------------
# 404 branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,extra",
    [
        ("/api/v1/tasks/nope/heartbeat", {}),
        ("/api/v1/tasks/nope/review", {}),
        ("/api/v1/tasks/nope/release", {}),
        ("/api/v1/tasks/nope/escalate", {"reason": "x"}),
    ],
)
def test_post_on_missing_task_is_404(url, extra):
    token = _key()
    r = Client().post(url, extra, **_auth(token))
    assert r.status_code == 404


# --------------------------------------------------------------------------
# state-machine refusals must surface as 409, not 500
# --------------------------------------------------------------------------


def test_release_refused_when_ownership_is_gone():
    """
    Once a card reaches REVIEW the lease is released, so nobody — not even the
    key that did the work — may release it afterwards.
    """
    token = _key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    c = Client()
    c.post("/api/v1/tasks/claim", {}, **_auth(token))
    c.post(
        f"/api/v1/tasks/{t.pk}/review",
        data=json.dumps({"evidence": {"ok": 1}}),
        content_type="application/json",
        **_auth(token),
    )
    t.refresh_from_db()
    assert t.status == Status.REVIEW
    assert t.claimed_by == ""

    # A key that can see the card but holds no lease gets 403. A key that
    # cannot see it at all gets 404, which test_task_visibility.py covers.
    other = _key()
    _grant_access(other)
    r = c.post(f"/api/v1/tasks/{t.pk}/release", {}, **_auth(other))
    assert r.status_code == 403
    r = c.post(f"/api/v1/tasks/{t.pk}/release", {}, **_auth(token))
    assert r.status_code == 403


def test_release_answers_409_if_the_state_machine_refuses(monkeypatch):
    """
    Every release target is currently a legal edge out of IN_PROGRESS, so this
    branch is unreachable through the state machine as it stands. It is kept
    because it is the difference between a clear 409 and a 500 the moment an
    edge is ever added — and a worker can act on 409, not on a stack trace.
    """
    from board import api as api_module
    from board.state import TransitionError

    token = _key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    c = Client()
    c.post("/api/v1/tasks/claim", {}, **_auth(token))

    def refuse(*args, **kwargs):
        raise TransitionError("simulated refusal")

    monkeypatch.setattr(api_module, "transition", refuse)
    r = c.post(f"/api/v1/tasks/{t.pk}/release", {}, **_auth(token))
    assert r.status_code == 409
    assert r.json()["error"] == "transition_refused"


def test_escalate_answers_409_if_the_state_machine_refuses(monkeypatch):
    from board import api as api_module
    from board.state import TransitionError

    token = _key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    c = Client()
    c.post("/api/v1/tasks/claim", {}, **_auth(token))

    def refuse(*args, **kwargs):
        raise TransitionError("simulated refusal")

    monkeypatch.setattr(api_module, "transition", refuse)
    r = c.post(f"/api/v1/tasks/{t.pk}/escalate", {"reason": "x"}, **_auth(token))
    assert r.status_code == 409


def test_escalate_refused_when_the_edge_does_not_exist():
    token = _key()
    t = place_in_default_project(
        title="w", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    # READY -> NEEDS_HUMAN is deliberately not an edge: escalation is
    # meaningful only for work that was actually attempted
    r = Client().post(
        f"/api/v1/tasks/{t.pk}/escalate", {"reason": "stuck"}, **_auth(token)
    )
    assert r.status_code == 409
    assert r.json()["error"] == "transition_refused"


# --------------------------------------------------------------------------
# filters and body parsing
# --------------------------------------------------------------------------


def test_list_filters_by_kind():
    token = _key()
    place_in_default_project(title="a", acceptance="x", status=Status.READY, kind="ops")
    place_in_default_project(title="b", acceptance="x", status=Status.READY, kind="code")
    r = Client().get("/api/v1/tasks?kind=ops", **_auth(token))
    assert [t["title"] for t in r.json()["tasks"]] == ["a"]


def test_list_default_limit_is_at_most_500():
    token = _key()
    r = Client().get("/api/v1/tasks", **_auth(token))
    assert r.json()["count"] <= 500


def test_json_array_body_is_not_treated_as_an_object():
    token = _key()
    r = Client().post(
        "/api/v1/tasks/claim",
        data="[1,2,3]",
        content_type="application/json",
        **_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["task"] is None


def test_empty_json_body_is_tolerated():
    token = _key()
    place_in_default_project(title="w", acceptance="x", status=Status.READY, autonomy="AUTO")
    r = Client().post(
        "/api/v1/tasks/claim", data=b"", content_type="application/json", **_auth(token)
    )
    assert r.status_code == 200
    assert r.json()["task"] is not None


def test_claim_accepts_kind_filter():
    token = _key()
    place_in_default_project(
        title="code", acceptance="x", status=Status.READY, kind="code", autonomy="AUTO"
    )
    place_in_default_project(
        title="ops", acceptance="x", status=Status.READY, kind="ops", autonomy="AUTO"
    )
    r = Client().post(
        "/api/v1/tasks/claim",
        data=json.dumps({"kinds": ["ops"]}),
        content_type="application/json",
        **_auth(token),
    )
    assert r.json()["task"]["title"] == "ops"


def test_claim_accepts_custom_lease_seconds():
    token = _key()
    place_in_default_project(title="w", acceptance="x", status=Status.READY, autonomy="AUTO")
    r = Client().post(
        "/api/v1/tasks/claim",
        data=json.dumps({"lease_seconds": 60}),
        content_type="application/json",
        **_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["task"]["lease_expires_at"] is not None


def test_board_state_reports_needs_human_and_time():
    token = _key()
    place_in_default_project(
        title="w", acceptance="x", status=Status.NEEDS_HUMAN, needs_human=True
    )
    r = Client().get("/api/v1/board", **_auth(token))
    body = r.json()
    assert body["needs_human"] == 1
    assert "server_time" in body
    assert "total_tokens" in body


def test_full_task_json_includes_structure_and_history():
    token = _key()
    blocker = place_in_default_project(title="b", acceptance="x", status=Status.DONE)
    t = place_in_default_project(title="w", acceptance="x", status=Status.READY)
    TaskDep.objects.create(task=t, depends_on=blocker)
    r = Client().get(f"/api/v1/tasks/{t.pk}", **_auth(token))
    body = r.json()["task"]
    assert body["dependencies"] == [blocker.pk]
    assert "events" in body
    assert "parent_id" in body
    assert body["depth"] == 0


def test_json_content_type_with_truly_empty_body_is_tolerated():
    """
    A worker that sends a JSON header and no payload at all must get a clean
    response, not a 500 from the parser.
    """
    token = _key()
    place_in_default_project(title="w", acceptance="x", status=Status.READY, autonomy="AUTO")
    r = Client().post(
        "/api/v1/tasks/claim",
        data=b"",
        content_type="application/json; charset=utf-8",
        **_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_body_parser_handles_empty_json_body_directly():
    """
    Unit-level check of _body itself.

    The HTTP-level tests cannot reach this branch: the Django test client
    drops the content type when there is no body, so the parser never sees a
    JSON content type with a genuinely empty payload. That combination is real
    though — `curl -X POST -H 'Content-Type: application/json'` with no -d — so
    the request is built by hand here rather than left untested.
    """
    from board.api import _body

    rf = RequestFactory()
    r = rf.post("/x", data=b"", content_type="application/json")
    # the factory drops content_type on an empty body; restore what a real
    # client would have sent
    r.META["CONTENT_TYPE"] = "application/json"
    r.content_type = "application/json"
    assert _body(r) == {}


def test_body_parser_returns_form_fields_for_urlencoded():
    from board.api import _body

    rf = RequestFactory()
    r = rf.post("/x", data="a=1&b=two", content_type="application/x-www-form-urlencoded")
    assert _body(r) == {"a": "1", "b": "two"}


def test_body_parser_rejects_non_object_json():
    from board.api import _body

    rf = RequestFactory()
    r = rf.post("/x", data=b'"just a string"', content_type="application/json")
    assert _body(r) == {}


def test_body_parser_swallows_invalid_json():
    from board.api import _body

    rf = RequestFactory()
    r = rf.post("/x", data=b"{oops", content_type="application/json")
    assert _body(r) == {}


def test_evidence_coercion_paths():
    from board.api import _coerce_evidence

    assert _coerce_evidence({"a": 1}) == {"a": 1}
    assert _coerce_evidence('{"a": 1}') == {"a": 1}
    assert _coerce_evidence("{not json") == {}
    assert _coerce_evidence("[1,2]") == {}
    assert _coerce_evidence("") == {}
    assert _coerce_evidence(None) == {}
    assert _coerce_evidence(42) == {}
