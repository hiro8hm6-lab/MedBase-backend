import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from medbase.config import Settings
from medbase.db import Job, make_engine, session_factory
from medbase.worker import claim_job


@pytest.mark.postgres
def test_real_migrations_and_skip_locked_claims(monkeypatch, tmp_path):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL for PostgreSQL integration tests")
    schema_name = "medbase_test_" + uuid.uuid4().hex
    admin_engine = make_engine(url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
    isolated_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema_name}"}).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", isolated_url)
    from medbase.config import get_settings
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        factory = session_factory(isolated_url)
        with factory.begin() as session:
            session.add_all([Job(id=str(i), owner_id="test", author="test", inputs=[]) for i in range(6)])
        settings = Settings(_env_file=None, database_url=isolated_url, storage_root=tmp_path)
        with ThreadPoolExecutor(max_workers=6) as executor:
            claims = list(executor.map(lambda _: claim_job(factory, settings), range(6)))
        assert len({claim[0] for claim in claims if claim}) == 6
        assert claim_job(factory, settings) is None
        command.downgrade(Config("alembic.ini"), "base")
        command.upgrade(Config("alembic.ini"), "head")
        factory.kw["bind"].dispose()
    finally:
        get_settings.cache_clear()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema_name}" CASCADE')
        admin_engine.dispose()
