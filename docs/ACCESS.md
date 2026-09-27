# Access control

Two ways in: a **session** for the operator using the browser, and an **API key**
for a headless agent. They are separate surfaces and neither can reach the
other's endpoints.

---

## Registration, and why the first account is an administrator

Anyone can register at `/register/`. An account grants nothing: it sees no
project, no tasks, not even the default one. It becomes useful only once an
administrator grants it read access to something. The board shows an explicit
"No projects yet" state rather than an empty grid, so it is obvious that this
is an access decision and not a fault.

**The first account to register becomes the instance administrator.** The rule
fires exactly once — only when the row being inserted is the very first in the
table. Every later registration is an ordinary user with no access.

This is a bootstrap convenience and it is a real trust boundary: whoever
registers first on a fresh instance owns it. Do not expose a new instance to an
untrusted network before registering yourself.

```bash
python manage.py createsuperuser    # same effect, without the web flow
```

There is **no password reset**. Recovering a forgotten password is done at the
shell:

```bash
python manage.py changepassword <username>
```

---

## Granting admin via SQL

Django stores accounts in `auth_user`, not `users`, and its column names follow
from that schema. The equivalents of the SQL you sketched:

```sql
-- make a user an administrator
UPDATE auth_user
   SET is_staff = true,
       is_superuser = true
 WHERE username = 'vaskes';
```

```sql
-- check who is what
SELECT id, username, is_staff, is_superuser, is_active FROM auth_user ORDER BY id;
```

```sql
-- make a plain operator (may use the board, not the admin site)
UPDATE auth_user SET is_staff = true, is_superuser = false WHERE username = 'op';
```

```sql
-- disable without deleting, so the audit trail keeps its actor names
UPDATE auth_user SET is_active = false WHERE username = 'gone';
```

Notes:

- `is_staff` alone gives access to `/admin/`. `is_superuser` additionally grants
  everything, including user management.
- Disabling (`is_active = false`) is preferred to deleting: `task_events.actor`
  stores the actor as a string, and a deleted account leaves names in the audit
  log that no longer resolve to anyone.
- The API key table is `board_agent_api_keys`, keyed to a user via `user_id`
  (the Django FK column name, `<field>_id`).

Applying a change this way works immediately — no restart. The ORM does not
cache users across requests beyond the session.

---

## API keys for agents

An agent is a headless process: no browser, no session, nobody to type a
password. It authenticates with a bearer token in a header and nothing else.

### Minting one

```bash
python manage.py create_api_key --user worker --label "dreamline regen"
python manage.py create_api_key --user dashboard --scope read
```

The token is printed **once** and is not recoverable. Only its SHA-256 hash is
stored, so a leaked database row does not yield usable credentials. Lost a
token? Mint another.

Tokens look like `kb_<prefix>_<secret>`. The prefix is public and safe to log;
it is what makes a leaked log line identifiable without being usable.

### Using one

```bash
curl -H "Authorization: Bearer kb_a1b2c3d4_xxxxx" \
     http://192.168.10.7:8901/api/v1/me
```

```python
import httpx

client = httpx.Client(
    base_url="http://192.168.10.7:8901",
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=30,
)
task = client.post("/api/v1/tasks/claim", json={}).json()["task"]
```

### Scopes

| Scope | Can |
|---|---|
| `read` | list tasks, read the board, fetch a card |
| `write` | the above, plus claim, heartbeat, review, release, escalate |

A `read` key attempting a write gets `403` with `"this key is read-only"` — a
message a worker can act on rather than retry blindly.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/me` | who this key is, and which account it acts as |
| `GET` | `/api/v1/tasks?status=READY&kind=ops&limit=50` | list |
| `GET` | `/api/v1/tasks/<id>` | one card, with dependencies and audit trail |
| `GET` | `/api/v1/board` | counts by status, needs-human count, token burn |
| `POST` | `/api/v1/tasks/claim` | take the highest-priority ready card |
| `POST` | `/api/v1/tasks/<id>/heartbeat` | liveness + current step, during work |
| `POST` | `/api/v1/tasks/<id>/review` | submit evidence; enters REVIEW |
| `POST` | `/api/v1/tasks/<id>/release` | give the card back, unfinished |
| `POST` | `/api/v1/tasks/<id>/escalate` | "a human must look", with a reason |

### A worker's loop

```python
r = client.post("/api/v1/tasks/claim", json={})
task = r.json()["task"]
if task is None:
    time.sleep(10); continue          # empty queue is a 200, not a failure

while working:
    client.post(f"/api/v1/tasks/{task['id']}/heartbeat",
                json={"step": "генерирую embeddings"})

client.post(f"/api/v1/tasks/{task['id']}/review", json={
    "evidence": {"exit_code": 0, "path": "/out/report.md"},
    "summary": "wrote the report",
    "tokens_used": 12400,
})
```

### Status codes a worker must handle

| Code | Meaning | What to do |
|---|---|---|
| `200` | done — for `claim`, `task` is `null` when the queue was empty | act |
| `401` | key missing, malformed, unknown, inactive | stop; the credential is wrong |
| `403` | read-only key attempting a write, or you do not hold the lease | stop retrying; someone else owns it |
| `404` | no such card | it was deleted or the id is wrong |
| `409` | the state machine refused the transition | read `detail`; usually evidence is missing |
| `400` | bad request body | fix the client |

## Board login

The browser UI is session-based, not key-based. Sign in at `/login/`, or
register at `/register/`. The session lasts 14 days.

`401` bodies are identical for unknown key, wrong secret and inactive key, so
the API cannot be used to enumerate valid prefixes.

---

## Two rules that are easy to get wrong

**Worker identity comes from the key, not the request body.** A `worker` field
in the claim body is ignored, because lease ownership is enforced by comparing
`claimed_by` against `api:<key prefix>`. Accepting a caller-supplied name would
let one key claim a card as another and satisfy the ownership check with the
wrong credential.

**Leaving `IN_PROGRESS` releases the lease.** A card that returns to `READY`
has its `claimed_by` and lease cleared, so a released card never displays as
busy. Accountability is preserved in `task_events`, not by leaving a stale
holder on the row.

---

## CSRF

Session-authenticated pages keep full CSRF protection. The API endpoints are
`csrf_exempt` per view, deliberately:

- A session cookie is sent **automatically** by the browser to any request to
  its origin. That is what makes it forgeable by another site, and what CSRF
  defends against.
- A bearer token is attached **explicitly** by the calling code. No browser
  attaches it on its own, so the cross-site scenario does not exist.

The exemption is per-view, not global. If a future refactor puts these views
behind session auth, the exemption must go with it — `test_csrf_token_not_required_for_api`
is the test that should make that visible.

---

## The agent API cannot see the board UI, and vice versa

- A board session does **not** grant API access: `GET /api/v1/me` with only a
  session cookie returns `401`.
- An API key does **not** grant board access: `GET /` with only a bearer token
  redirects to `/login/`.

Each surface authorises on its own credential. Both are tested.
