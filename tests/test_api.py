from datetime import timedelta

from fastapi.testclient import TestClient

from medbase.api import create_app
from medbase.db import ExamSet, Job
from medbase.identity import current_user
from medbase.storage import now
from medbase.worker import claim_job, process_job, run_once
from tests.conftest import submit


def test_http_pipeline_publication_and_identity(client, factory, settings, sample):
    response = submit(client, sample)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    assert run_once(factory, settings)
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed", job
    assert job["created_at"].endswith("+09:00") and job["updated_at"].endswith("+09:00")
    set_id = job["results"][0]["set_id"]
    result = client.get(f"/api/v1/sets/{set_id}/result").json()
    assert result["author"] == "Test author" and result["total_questions"] == 4
    assert client.get("/api/v1/sets").json() == {"sets": []}
    assert len(client.get("/api/v1/sets?include_drafts=true").json()["sets"]) == 1
    frontend = client.get(f"/api/v1/sets/{set_id}/questions").json()
    assert frontend["q_and_a"][1]["answer"] == "600"
    assert frontend["q_and_a"][1]["options"] == {}
    assert frontend["q_and_a"][3]["depends_on"] == [3]
    src = frontend["q_and_a"][2]["images"][0]["src"]
    assert client.get(src).content.startswith(b"\x89PNG")
    assert client.get(f"/api/v1/sets/{set_id}/pdf").content.startswith(b"%PDF")
    assert client.get(f"/api/v1/sets/{set_id}/download").content.startswith(b"PK")
    assert client.post(f"/api/v1/sets/{set_id}/publish", json={"visibility": "private"}).status_code == 200
    assert len(client.get("/api/v1/sets").json()["sets"]) == 1
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409

    # A new API instance reads the same persisted job, set and assets.
    with TestClient(create_app(settings, factory)) as second:
        assert second.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"
        assert second.get(f"/api/v1/sets/{set_id}/result").status_code == 200

    client.app.dependency_overrides[current_user] = lambda: "other-user"
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/v1/sets/{set_id}/result").status_code == 404
    assert client.get(src).status_code == 404
    assert client.get("/api/v1/sets?include_drafts=true").json() == {"sets": []}
    assert client.post(f"/api/v1/sets/{set_id}/publish", json={"visibility": "shared"}).status_code == 404
    client.app.dependency_overrides.pop(current_user)
    assert client.post(f"/api/v1/sets/{set_id}/publish", json={"visibility": "shared"}).status_code == 200
    client.app.dependency_overrides[current_user] = lambda: "other-user"
    assert client.get(f"/api/v1/sets/{set_id}/result").status_code == 200
    assert client.get(src).status_code == 200
    assert len(client.get("/api/v1/sets").json()["sets"]) == 1


def test_upload_validation_and_no_client_owner_override(client, settings, sample):
    assert submit(client, sample, author="  ").status_code == 422
    assert client.post("/api/v1/jobs", data={"author": "a"}, files={"files": ("a.exe", b"bad")}).status_code == 415
    assert client.post("/api/v1/jobs", data={"author": "a"}, files={"files": ("a.pdf", b"bad")}).status_code == 422
    settings.max_file_mb = 1
    assert client.post("/api/v1/jobs", data={"author": "a"}, files={"files": ("a.pdf", b"x" * 1_048_577)}).status_code == 413
    assert list((settings.storage_root / "jobs").iterdir()) == []


def test_real_mode_missing_key_is_clear_503(settings, factory, sample):
    settings.llm_mode = "gemini"
    with TestClient(create_app(settings, factory)) as client:
        response = submit(client, sample)
        assert response.status_code == 503 and "GEMINI_API_KEY" in response.text


def test_lease_reclaims_interrupted_worker_and_old_token_cannot_commit(client, factory, settings, sample):
    job_id = submit(client, sample).json()["id"]
    first = claim_job(factory, settings)
    assert first[0] == job_id and claim_job(factory, settings) is None
    with factory.begin() as session:
        job = session.get(Job, job_id)
        job.lease_until = now() - timedelta(seconds=1)
    second = claim_job(factory, settings)
    assert second[0] == job_id and second[1] != first[1]
    process_job(factory, settings, *first)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "running"
    process_job(factory, settings, *second)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"


def test_publish_rejects_failed_set(client, factory, settings, sample):
    job_id = submit(client, sample).json()["id"]
    run_once(factory, settings)
    set_id = client.get(f"/api/v1/jobs/{job_id}").json()["results"][0]["set_id"]
    with factory.begin() as session:
        session.get(ExamSet, set_id).error_count = 1
    assert client.post(f"/api/v1/sets/{set_id}/publish", json={"visibility": "shared"}).status_code == 409


def test_health_and_input_log_owner(client, factory, settings, sample):
    assert client.get("/health/ready").status_code == 200
    job_id = submit(client, sample).json()["id"]
    run_once(factory, settings)
    assert client.get(f"/api/v1/jobs/{job_id}/logs/input_log.json").status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}/logs/.env").status_code == 404
    client.app.dependency_overrides[current_user] = lambda: "other-user"
    assert client.get(f"/api/v1/jobs/{job_id}/logs/input_log.json").status_code == 404
