from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import Settings, settings


def build_engine(configuration: Settings) -> AsyncEngine:
    """Build the configured local async engine without opening a connection."""
    database_url = configuration.database_url
    driver = make_url(database_url).drivername

    # check_same_thread é exclusivo do driver síncrono do SQLite; aiosqlite ignora,
    # mas passamos explicitamente para não gerar warning em alguns setups.
    if driver == "sqlite+aiosqlite":
        return create_async_engine(
            database_url,
            echo=configuration.database_echo,
            connect_args={"check_same_thread": False},
        )

    if driver == "postgresql+asyncpg":
        return create_async_engine(
            database_url,
            echo=configuration.database_echo,
            pool_pre_ping=True,
            pool_size=configuration.database_pool_size,
            max_overflow=configuration.database_max_overflow,
            pool_timeout=configuration.database_pool_timeout_seconds,
            connect_args={
                "timeout": configuration.database_connect_timeout_seconds,
                "command_timeout": configuration.database_command_timeout_seconds,
                "server_settings": {"application_name": "automation-foundry"},
            },
        )

    raise ValueError("unsupported async database driver")


engine: AsyncEngine = build_engine(settings)

# expire_on_commit=False evita que atributos expirem após commit em contexto async,
# o que causaria MissingGreenlet ao tentar acessá-los fora da sessão.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base declarativa compartilhada por todos os models."""


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency do FastAPI: fornece uma sessão async por request, com
    commit automático em caso de sucesso e rollback em caso de exceção.

    Uso:
        @router.get("/")
        async def endpoint(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
