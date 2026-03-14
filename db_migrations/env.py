from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from libs.infra.postgres import resolve_postgres_dsn, to_sqlalchemy_url

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    url = _resolve_sqlalchemy_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_sqlalchemy_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


def _resolve_sqlalchemy_url() -> str:
    raw_url = resolve_postgres_dsn() or config.get_main_option("sqlalchemy.url") or ""
    if not raw_url:
        raise RuntimeError(
            "Set APP_POSTGRES_DSN or APP_POSTGRES_HOST/PORT/DB/USER/PASSWORD"
        )
    return to_sqlalchemy_url(raw_url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
