from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import settings


def _build_engine() -> AsyncEngine:
    connect_args: dict[str, object] = {}
    # check_same_thread é exclusivo do driver síncrono do SQLite; aiosqlite ignora,
    # mas passamos explicitamente para não gerar warning em alguns setups.
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    return create_async_engine(
        settings.database_url,
        echo=settings.is_development,
        connect_args=connect_args,
    )


engine: AsyncEngine = _build_engine()

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
