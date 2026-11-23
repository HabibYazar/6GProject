# NSG platform API (FastAPI + the ns-O-RAN translation layer).
#
# Deliberately NOT a builder/runtime split: psycopg is pinned as psycopg-binary
# in requirements.txt, so every dependency arrives as a wheel and there is
# nothing to compile — a second stage would add a Dockerfile to maintain and
# save nothing. curl is present only so compose can health-check this service.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so editing application code does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY nsg_ai/ ./nsg_ai/
COPY tools/ ./tools/

# Never bake nsg_ai/.env into the image — it carries the OpenRouter key. It is
# excluded in .dockerignore, and llm_agent's _load_env_file does not override a
# variable the environment already has, so passing OPENROUTER_API_KEY through
# compose works with no file present.

EXPOSE 8000

# Migrate, then serve. uvicorn runs from nsg_ai/ because main.py imports its
# siblings as top-level modules (`import store`), exactly as the dev command
# does; alembic runs from /app where alembic.ini lives. `exec` hands PID 1 to
# uvicorn so a `docker compose stop` reaches it as SIGTERM.
CMD ["sh", "-c", "alembic upgrade head && cd /app/nsg_ai && exec python -m uvicorn main:app --host 0.0.0.0 --port 8000"]
