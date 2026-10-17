"""Run real Gemini sample acceptance against a running API (uses billable API calls)."""
import argparse
import contextlib
import json
import time
import zipfile
from pathlib import Path

import httpx

from medbase.cli import check_sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("work/live-sample"))
    parser.add_argument("--job-id", help="Resume monitoring an already submitted job")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    names = ["tp240424-01a_01.pdf", "tp240424-01a_02.pdf", "tp240424-01seitou.pdf"]
    with httpx.Client(base_url=args.url, timeout=600) as client:
        if args.job_id:
            job_id = args.job_id
        else:
            assert client.get("/health/live").json()["llm_mode"] == "gemini"
            with contextlib.ExitStack() as stack:
                uploads = [("files", (name, stack.enter_context((args.directory / name).open("rb")), "application/pdf"))
                           for name in names]
                response = client.post("/api/v1/jobs", data={"author": args.author}, files=uploads)
            response.raise_for_status()
            job_id = response.json()["id"]
            (args.output / "job-id.txt").write_text(job_id)
        print("Live sample job:", job_id, flush=True)
        deadline = time.monotonic() + 7200
        previous = None
        while True:
            response = client.get(f"/api/v1/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            state = (job["status"], job["stage"], json.dumps(job["progress"], sort_keys=True))
            if state != previous:
                print("Progress:", *state, flush=True)
                previous = state
            if job["status"] in {"completed", "completed_with_errors", "failed"}:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Job still running; resume with --job-id " + job_id)
            time.sleep(5)
        (args.output / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        if job["status"] == "failed":
            raise RuntimeError(job["error"])
        assert len(job["results"]) == 1, "Expected exactly one A booklet set"
        set_id = job["results"][0]["set_id"]
        bundle_path = args.output / "bundle.zip"
        with client.stream("GET", f"/api/v1/sets/{set_id}/download") as response:
            response.raise_for_status()
            with bundle_path.open("wb") as stream:
                for block in response.iter_bytes():
                    stream.write(block)
        target = args.output / "result"
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(bundle_path) as archive:
            for info in archive.infolist():
                resolved = (target / info.filename).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise ValueError("Invalid bundle member")
            archive.extractall(target)
        verdict = check_sample(target / "result.json", args.directory / names[2])
        (args.output / "acceptance.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        print("LIVE ACCEPTANCE:", verdict, flush=True)
        print("Set remains a draft for human review:", set_id, flush=True)


if __name__ == "__main__":
    main()
