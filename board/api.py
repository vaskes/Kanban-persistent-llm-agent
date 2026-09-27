"""
Agent-facing HTTP API.

This is the surface a headless worker uses: claim a card, heartbeat while
working, submit evidence for review, release it. It exists so the runtime in
part 2 never has to shell out to a management command or import Django models
across a process boundary.

CSRF and why these views are exempt
-----------------------------------
The usual reason for CSRF protection is that browsers attach cookies
*automatically* to any request to a given origin, so a malicious page can make
a user's browser perform an authenticated action it never intended. That attack
needs an ambient credential.

A bearer token is the opposite: the calling code puts it in the header
deliberately, and no browser will attach it by itself. These endpoints are
therefore marked `csrf_exempt` — not carelessly, but because the credential is
non-ambient. Session-authenticated views (the board itself) keep full CSRF
protection, since for them the cookie *is* ambient.

The exemption is per-view rather than global on purpose.
"""

from __future__ import annotations

import json

from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Actor, Status, Task
from .state import Ctx, TransitionError, claim, heartbeat, transition


def _unauthorised():
    # Deliberately identical for unknown key, wrong secret and inactive key:
    # distinguishing them would let an unauthenticated caller probe for
    # valid prefixes.
    return JsonResponse(
        {"error": "unauthorised", "detail": "valid API key required"},
        status=401,
    )


def _forbidden(detail: str):
    return JsonResponse({"error": "forbidden", "detail": detail}, status=403)


def _bad_request(detail: str):
    return JsonResponse({"error": "bad_request", "detail": detail}, status=400)


def _transition_error(exc: TransitionError):
    # 409, not 400: the request was well-formed, the card's current state is
    # what refuses it. Distinguishing this makes a worker's retry logic sane.
    return JsonResponse(
        {"error": "transition_refused", "detail": str(exc)}, status=409
    )


def _body(request) -> dict:
    """
    Read a request body as a dict.

    JSON is the intended format for agents. Form-encoded is also accepted so
    that `curl -d step=...` works without ceremony, which matters when an
    operator is debugging the API by hand.
    """
    if request.content_type and "application/json" in request.content_type:
        if not request.body:
            return {}
        try:
            data = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    # form-encoded or multipart
    return {k: v for k, v in request.POST.items()}


def _coerce_evidence(value) -> dict:
    """
    Accept evidence as an object, or as a JSON string.

    Form-encoded bodies cannot carry nested structures — Django flattens
    {"evidence": {"exit_code": 0}} into a stringified dict. Rather than pretend
    otherwise, a form caller may pass evidence as a JSON string and get the same
    behaviour as a JSON caller. A non-JSON string is treated as no evidence at
    all, because the state machine must not accept a Python repr by accident.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _task_json(task: Task, *, full: bool = False) -> dict:
    out = {
        "id": task.id,
        "title": task.title,
        "goal": task.goal,
        "kind": task.kind,
        "status": task.status,
        "priority": task.priority,
        "autonomy": task.autonomy,
        "acceptance": task.acceptance,
        "verifier_kind": task.verifier_kind,
        "verifier_cmd": task.verifier_cmd,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "tokens_used": task.tokens_used,
        "max_tokens": task.max_tokens,
        "stuck_score": task.stuck_score,
        "needs_human": task.needs_human,
        "attention_reason": task.attention_reason,
        "current_step": task.current_step,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }
    if full:
        out["evidence"] = task.evidence
        out["root_id"] = task.root_id
        out["parent_id"] = task.parent_id
        out["depth"] = task.depth
        out["claimed_by"] = task.claimed_by
        out["lease_expires_at"] = (
            task.lease_expires_at.isoformat() if task.lease_expires_at else None
        )
        out["dependencies"] = [d.depends_on_id for d in task.dependencies.all()]
        out["events"] = [
            {
                "ts": e.ts.isoformat(),
                "actor": e.actor,
                "event": e.event,
                "from": e.from_status,
                "to": e.to_status,
            }
            for e in task.events.order_by("-ts")[:50]
        ]
    return out


def _auth(request):
    """
    Returns (key, None) on success or (None, response) on failure.

    The token was already resolved once by ApiKeyMiddleware; re-parsing the
    header per view would mean a second DB round trip per endpoint.
    """
    key = getattr(request, "api_key", None)
    if key is None:
        return None, _unauthorised()
    return key, None


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["GET"])
def me(request):
    key, err = _auth(request)
    if err:
        return err
    return JsonResponse(
        {
            "ok": True,
            "key_prefix": key.prefix,
            "label": key.label,
            "scope": key.scope,
            "acts_as": {
                "username": key.user.username,
                "is_staff": key.user.is_staff,
                "is_superuser": key.user.is_superuser,
            },
        }
    )


@csrf_exempt
@require_http_methods(["GET"])
def list_tasks(request):
    key, err = _auth(request)
    if err:
        return err
    qs = Task.objects.all()
    status = request.GET.get("status")
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        qs = qs.filter(status__in=wanted)
    kind = request.GET.get("kind")
    if kind:
        qs = qs.filter(kind=kind)
    limit = min(int(request.GET.get("limit", "100") or 100), 500)
    tasks = qs.order_by("-priority", "created_at")[:limit]
    return JsonResponse(
        {"ok": True, "count": len(tasks), "tasks": [_task_json(t) for t in tasks]}
    )


@csrf_exempt
@require_http_methods(["GET"])
def get_task(request, task_id):
    key, err = _auth(request)
    if err:
        return err
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse({"ok": True, "task": _task_json(task, full=True)})


@csrf_exempt
@require_http_methods(["GET"])
def board_state(request):
    key, err = _auth(request)
    if err:
        return err
    rows = Task.objects.values("status").annotate(n=Count("id"))
    return JsonResponse(
        {
            "ok": True,
            "by_status": {r["status"]: r["n"] for r in rows},
            "needs_human": Task.objects.filter(needs_human=True).count(),
            "total_tokens": sum(Task.objects.values_list("tokens_used", flat=True)),
            "server_time": timezone.now().isoformat(),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def claim_task(request):
    key, err = _auth(request)
    if err:
        return err
    if not key.can_write:
        return _forbidden("this key is read-only")

    from django.conf import settings

    data = _body(request)
    # Worker identity comes from the key, never from the request body. Lease
    # ownership is enforced by comparing claimed_by against this string, so a
    # caller-supplied identity would be a way to claim a card as someone else
    # and to make another key look like the lease holder.
    worker = f"api:{key.prefix}"
    task = claim(
        worker=worker,
        lease_seconds=int(data.get("lease_seconds") or settings.TASK_LEASE_SECONDS),
        allowed_kinds=data.get("kinds"),
    )
    if task is None:
        # Not an error: an empty queue is a normal outcome for a worker, and it
        # must not look like a failure or the worker will start retrying loudly.
        return JsonResponse({"ok": True, "task": None, "detail": "no work available"})
    return JsonResponse({"ok": True, "task": _task_json(task, full=True)})


@csrf_exempt
@require_http_methods(["POST"])
def task_heartbeat(request, task_id):
    key, err = _auth(request)
    if err:
        return err
    if not key.can_write:
        return _forbidden("this key is read-only")
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    if task.claimed_by != f"api:{key.prefix}":
        return _forbidden("this key does not hold the lease on that card")
    heartbeat(task, _body(request).get("step", ""))
    task.refresh_from_db()
    return JsonResponse({"ok": True, "heartbeat_at": task.heartbeat_at.isoformat()})


@csrf_exempt
@require_http_methods(["POST"])
def submit_review(request, task_id):
    """
    Executor hands work to the verifier.

    Evidence is mandatory and is the only thing that lets a card into REVIEW.
    An agent that claims to be done without proof is refused by the state
    machine, not by a prompt.
    """
    key, err = _auth(request)
    if err:
        return err
    if not key.can_write:
        return _forbidden("this key is read-only")
    data = _body(request)
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    if task.claimed_by != f"api:{key.prefix}":
        return _forbidden("this key does not hold the lease on that card")
    if data.get("tokens_used"):
        task.tokens_used = min(int(data["tokens_used"]), task.max_tokens)
        task.save(update_fields=["tokens_used"])
    try:
        transition(
            task,
            Status.REVIEW,
            Ctx(
                actor=Actor.AGENT,
                evidence=_coerce_evidence(data.get("evidence")),
                reason=data.get("summary", ""),
            ),
            event="submit_review",
        )
    except TransitionError as exc:
        return _transition_error(exc)
    task.refresh_from_db()
    return JsonResponse({"ok": True, "task": _task_json(task, full=True)})


@csrf_exempt
@require_http_methods(["POST"])
def release_task(request, task_id):
    """Give a card back without finishing it — used when work is not ready."""
    key, err = _auth(request)
    if err:
        return err
    if not key.can_write:
        return _forbidden("this key is read-only")
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    if task.claimed_by != f"api:{key.prefix}":
        return _forbidden("this key does not hold the lease on that card")
    target = _body(request).get("status") or Status.READY
    if target not in {Status.READY, Status.BLOCKED, Status.NEEDS_HUMAN}:
        return _bad_request(f"cannot release to {target}")
    try:
        transition(
            task,
            target,
            Ctx(actor=Actor.AGENT, reason=_body(request).get("reason", "released")),
            event="release",
        )
    except TransitionError as exc:
        return _transition_error(exc)
    task.refresh_from_db()
    return JsonResponse({"ok": True, "task": _task_json(task, full=True)})


@csrf_exempt
@require_http_methods(["POST"])
def escalate(request, task_id):
    """Agent says: I cannot proceed, a human must look."""
    key, err = _auth(request)
    if err:
        return err
    if not key.can_write:
        return _forbidden("this key is read-only")
    data = _body(request)
    reason = (data.get("reason") or "").strip()
    if not reason:
        return _bad_request("a reason is required to escalate")
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    try:
        transition(
            task,
            Status.NEEDS_HUMAN,
            Ctx(actor=Actor.AGENT, reason=reason),
            event="escalate",
        )
    except TransitionError as exc:
        return _transition_error(exc)
    task.refresh_from_db()
    return JsonResponse({"ok": True, "task": _task_json(task, full=True)})
