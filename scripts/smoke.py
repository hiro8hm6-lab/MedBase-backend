"""Exercise the real HTTP API + worker + DB, with explicitly synthetic LLM answers."""
import argparse
import io
import time
import zipfile
from pathlib import Path

import httpx

from medbase.fixtures import create_fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--directory", type=Path, default=Path("work/smoke"))
    args = parser.parse_args()
    sample = create_fixture(args.directory)
    with httpx.Client(base_url=args.url, timeout=60) as client:
        deadline = time.monotonic() + 120
        while True:
            try:
                response = client.get("/health/ready")
                response.raise_for_status()
                break
            except (httpx.HTTPError, OSError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)
        assert client.get("/health/live").json()["llm_mode"] == "mock", "Smoke test requires LLM_MODE=mock"
        with sample.open("rb") as stream:
            response = client.post("/api/v1/jobs", data={"author": "Smoke test"}, files={"files": ("smoke.pdf", stream, "application/pdf")})
        response.raise_for_status()
        job_id = response.json()["id"]
        deadline = time.monotonic() + 180
        while True:
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"completed", "completed_with_errors", "failed"}:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(job)
            time.sleep(1)
        assert job["status"] == "completed", job
        set_id = job["results"][0]["set_id"]
        response = client.get(f"/api/v1/sets/{set_id}/result")
        response.raise_for_status()
        result = response.json()
        assert result["total_questions"] == 4 and all(q["status"] == "ok" for q in result["questions"])
        assert result["questions"][1]["answer_text"] == "600"
        assert result["questions"][2]["series_total"] == 2
        for q in result["questions"]:
            for image in q.get("images", []):
                assert (client.get(f"/api/v1/sets/{set_id}/images/{q['q_id']}/0").content).startswith(b"\x89PNG")
        bundle = client.get(f"/api/v1/sets/{set_id}/download")
        bundle.raise_for_status()
        archive = zipfile.ZipFile(io.BytesIO(bundle.content))
        assert {"result.json", "merged.pdf", "merge_log.txt", "clips/q3_f0.png"} <= set(archive.namelist())
        assert set_id not in {s["id"] for s in client.get("/api/v1/sets").json()["sets"]}
        published = client.post(f"/api/v1/sets/{set_id}/publish", json={"visibility": "private"})
        published.raise_for_status()
        assert set_id in {s["id"] for s in client.get("/api/v1/sets").json()["sets"]}
        frontend = client.get(f"/api/v1/sets/{set_id}/questions").json()
        assert len(frontend["q_and_a"]) == 4 and frontend["q_and_a"][1]["options"] == {}
        (args.directory / "last-set-id.txt").write_text(set_id)
        print(f"PASS: HTTP upload -> worker -> 4 questions -> PNG/PDF/ZIP -> private publication; set={set_id}")


if __name__ == "__main__":
    main()
