"""
Database connection and session handling.

One synchronous engine, deliberately. The backend's long jobs — `_sim_job` and
`_campaign_job` — run in background threads, and a thread that owns a plain
session for its duration is far easier to reason about than an async session
juggled across an event loop it does not own. Nothing here is connection-bound:
a campaign issues a handful of writes over several minutes while ns-3 does the
work.

The URL comes from the environment and there is no working default: the password
belongs in `.env`, which is gitignored, and a credential committed once is
committed forever. `.env.example` documents the shape.

Port 5433, not 5432. The cluster was created while something else held 5432, so
Debian's cluster manager assigned the next free port. Leaving it there is
deliberate — it keeps this database unambiguous if a container ever publishes
5432 again, which is exactly the confusion that would have two processes writing
to two different databases that both answer on localhost.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def _load_env_file() -> None:
    """Read .env without adding a dependency, and without overriding a value the
    environment already supplies — deployment wins over the developer's file."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

DATABASE_URL = os.environ.get("NSG_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "NSG_DATABASE_URL is not set. Copy .env.example to .env and fill in the "
        "password you gave the 'nsg' role."
    )

# pool_pre_ping because the container can be restarted underneath a long-lived
# backend — without it the first query after a restart fails on a stale
# connection instead of transparently reconnecting.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction that commits on success and rolls back on any exception."""
    s = SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def healthy() -> bool:
    """Used by preflight: is the database reachable at all?"""
    from sqlalchemy import text
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False
