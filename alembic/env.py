"""
Alembic environment.

The connection URL is NOT read from alembic.ini. It comes from nsg_ai.db, which
reads .env — so there is exactly one place a credential lives, and alembic.ini
stays committable. The placeholder left in alembic.ini by `alembic init` is
ignored on purpose.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the application's metadata so `--autogenerate` can diff the models
# against the live database.
from nsg_ai.db import DATABASE_URL
from nsg_ai.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this a column changing type is silently missed, which is a
            # live risk here: the config schema evolves constantly and its
            # JSON columns are the ones most likely to move.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
