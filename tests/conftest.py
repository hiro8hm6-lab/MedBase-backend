import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from medbase.api import create_app
from medbase.config import Settings
from medbase.db import Base, session_factory
from medbase.fixtures import create_fixture


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, app_env="test", llm_mode="mock", storage_root=tmp_path / "storage",
                    database_url=f"sqlite:///{tmp_path / 'test.db'}", gemini_concurrency=1)


@pytest.fixture
def factory(settings):
    settings.storage_root.mkdir(parents=True)
    factory = session_factory(settings.database_url)
    Base.metadata.create_all(factory.kw["bind"])
    yield factory
    factory.kw["bind"].dispose()


@pytest.fixture
def client(settings, factory):
    with TestClient(create_app(settings, factory)) as client:
        yield client


@pytest.fixture
def sample(tmp_path):
    return create_fixture(tmp_path / "fixture")


@pytest.fixture(autouse=True)
def require_poppler():
    assert shutil.which("pdftoppm"), "Install poppler-utils or put Poppler bin on PATH"


def submit(client, sample: Path, author=" Test author "):
    with sample.open("rb") as stream:
        return client.post("/api/v1/jobs", data={"author": author}, files={"files": (sample.name, stream, "application/pdf")})
