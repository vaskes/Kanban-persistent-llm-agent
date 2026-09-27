# Deployment

Target host: `llmhost2` (Ubuntu 24.04, Python 3.12, x86_64, ROCm). The repo is
not pushed to `llmhost1` — that machine is production and is left alone.

## 1. Database

Postgres 16 with pgvector, in Docker, on loopback only.

```bash
docker run -d --name kanban-pg \
  -e POSTGRES_USER=kanban -e POSTGRES_PASSWORD=kanban -e POSTGRES_DB=kanban \
  -p 127.0.0.1:5433:5432 \
  -v kanban-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

docker exec -i kanban-pg psql -U kanban -d kanban -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Back up with `pg_dump -U kanban kanban | gzip > backup.sql.gz`; restore with
`gunzip -c backup.sql.gz | docker exec -i kanban-pg psql -U kanban -d kanban`.

## 2. Application

```bash
cd /opt/mavis-agent
git clone http://<user>:<token>@127.0.0.1:3000/vaskes/Kanban-persistent-llm-agent.git kanban-agent
cd kanban-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit
.venv/bin/python manage.py migrate
```

## 3. Service

`deploy/kanban-web.service` runs gunicorn on `127.0.0.1:8901`.

```bash
sudo install -m 644 deploy/kanban-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kanban-web
```

Access is loopback-only on purpose. Expose through the existing reverse proxy if
remote access is needed; do not bind `0.0.0.0`.

## 4. Verify

```bash
bash scripts/smoke.sh              # healthz, board, admin, reports
.venv/bin/python -m pytest --cov   # must report 100%
```

## 5. Git

The working copy is owned by `git:git`. The GitHub key lives in
`/root/.ssh/id_ed25519` and is not readable by the `git` user, by design.
Push as root and restore ownership:

```bash
sudo env GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes" \
  git push github main
sudo chown -R git:git /opt/mavis-agent/kanban-agent
```

## Configuration

All settings come from the environment; see `.env.example`. The values that
matter most:

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_PROVIDER` | `fake` | `fake` / `local_llama` / `local_vllm` / `minimax` / `qwen` |
| `TASK_LEASE_SECONDS` | `900` | how long a claim survives without a heartbeat |
| `TASK_HEARTBEAT_SECONDS` | `10` | liveness refresh interval during work |
| `TASK_DEFAULT_MAX_ATTEMPTS` | `3` | per-card attempt cap |
| `TASK_DEFAULT_MAX_TOKENS` | `200000` | per-card token cap |
| `MAX_DEPTH` | `4` | maximum decomposition depth |

`TASK_LEASE_SECONDS` must stay comfortably above `TASK_HEARTBEAT_SECONDS`;
a test asserts this, because a lease shorter than a heartbeat interval makes
healthy work look dead.
