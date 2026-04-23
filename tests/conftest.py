# conftest.py — Shared test fixtures that ensure proper DB setup
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Import models FIRST so Base.metadata is populated
import app.models  # noqa: F401 — registers ORM models
from app.database import Base, get_db
from app.main import app

TEST_DB_URL = "sqlite:///./data/test_shared.db"

engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionFactory()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True, scope="function")
def clean_db():
    """Create all tables before each test, drop+recreate after."""
    Base.metadata.create_all(bind=engine)
    # Also ensure indexes exist
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_events_store_ts
            ON events (store_id, timestamp)
        """))
        conn.commit()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = TestingSessionFactory()
    yield session
    session.close()


@pytest.fixture
def client():
    return TestClient(app)
