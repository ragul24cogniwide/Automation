import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Fallback to local SQLite if no DATABASE_URL is configured
    DATABASE_URL = "sqlite:///./agent.db"

# SQLAlchemy requires postgresql+psycopg2:// prefix when using psycopg2 driver
if DATABASE_URL.startswith("postgresql://"):
    SQLALCHEMY_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    SQLALCHEMY_DATABASE_URL = DATABASE_URL

# Engine configuration
engine_args = {
    "pool_pre_ping": True,  # Checks connection liveness (crucial for Neon serverless pooler)
}

# Add pool sizing for PostgreSQL connections
if "sqlite" not in SQLALCHEMY_DATABASE_URL:
    engine_args.update({
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 300,
    })

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initializes all tables defined in models if they don't already exist."""
    import models  # Ensure models are imported so Base has metadata
    Base.metadata.create_all(bind=engine)
