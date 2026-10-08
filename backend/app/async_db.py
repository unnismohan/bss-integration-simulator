from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import API_DB_MAX_OVERFLOW, API_DB_POOL_SIZE, DATABASE_URL

if DATABASE_URL.startswith("postgresql+psycopg://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    raise RuntimeError("DATABASE_URL must use PostgreSQL with psycopg or the postgresql scheme")

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=API_DB_POOL_SIZE,
    max_overflow=API_DB_MAX_OVERFLOW,
    pool_timeout=10,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
