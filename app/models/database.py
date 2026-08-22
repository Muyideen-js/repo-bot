from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class User(Base):
    """A Telegram user who has set up the bot."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, nullable=False)
    github_token_encrypted = Column(String, nullable=True)   # encrypted PAT
    github_username = Column(String, nullable=True)
    setup_complete = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Repo(Base):
    """A repo a user has registered with the bot."""
    __tablename__ = "repos"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, nullable=False)             # owner
    full_name = Column(String, nullable=False)               # e.g. yusuf/my-repo
    webhook_id = Column(String, nullable=True)               # GitHub webhook ID
    merge_strategy = Column(String, default="merge")         # always regular merge
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PRLog(Base):
    """Audit log of every PR decision the bot made."""
    __tablename__ = "pr_logs"

    id = Column(Integer, primary_key=True)
    repo_full_name = Column(String, nullable=False)
    pr_number = Column(Integer, nullable=False)
    pr_title = Column(String, nullable=True)
    contributor = Column(String, nullable=True)
    decision = Column(String, nullable=False)                # MERGED | COMMENTED | SKIPPED
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReviewJob(Base):
    """Durable, idempotent work item for one exact PR commit."""
    __tablename__ = "review_jobs"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "pr_number", "head_sha", name="uq_review_commit"),
    )

    id = Column(Integer, primary_key=True)
    repo_full_name = Column(String, nullable=False, index=True)
    pr_number = Column(Integer, nullable=False)
    head_sha = Column(String, nullable=False)
    status = Column(String, nullable=False, default="QUEUED", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
