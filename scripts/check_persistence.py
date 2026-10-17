import argparse
import time
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--directory", type=Path, default=Path("work/smoke"))
args = parser.parse_args()
set_id = (args.directory / "last-set-id.txt").read_text().strip()
deadline = time.monotonic() + 60
while True:
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
            client.get("/health/ready").raise_for_status()
            response = client.get(f"/api/v1/sets/{set_id}/result")
            response.raise_for_status()
            assert response.json()["total_questions"] == 4
            assert set_id in {s["id"] for s in client.get("/api/v1/sets").json()["sets"]}
            print("PASS: persisted published set and result survive API/worker restart")
            break
    except httpx.HTTPError:
        if time.monotonic() > deadline:
            raise
        time.sleep(1)
