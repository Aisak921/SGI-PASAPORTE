"""Motor async de PostgreSQL y sesiones."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    from . import models  # noqa: F401  (registra las tablas)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
