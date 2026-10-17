import logging
import signal
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from medbase.config import Settings, get_settings
from medbase.db import ExamSet, Job, session_factory
from medbase.pipeline import Pipeline
from medbase.storage import now, safe_error

logger = logging.getLogger("medbase.worker")


def claim_job(factory, settings: Settings) -> tuple[str, str] | None:
    with factory.begin() as session:
        job = session.scalar(select(Job).where(or_(
            Job.status == "queued", and_(Job.status == "running", Job.lease_until < now())
        ), Job.llm_mode == settings.llm_mode).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        job.lease_token = str(uuid.uuid4())
        job.lease_until = now() + timedelta(seconds=settings.worker_lease_seconds)
        job.updated_at = now()
        return job.id, job.lease_token


def process_job(factory, settings: Settings, job_id: str, lease_token: str, provider=None) -> None:
    stopped = threading.Event()
    lost_lease = threading.Event()

    def heartbeat():
        while not stopped.wait(max(1, settings.worker_lease_seconds / 4)):
            try:
                with factory.begin() as session:
                    updated = session.execute(update(Job).where(
                        Job.id == job_id, Job.lease_token == lease_token, Job.status == "running"
                    ).values(lease_until=now() + timedelta(seconds=settings.worker_lease_seconds), updated_at=now()))
                    if updated.rowcount != 1:
                        lost_lease.set()
                        return
            except Exception:
                logger.warning("Heartbeat unavailable for job %s", job_id)

    def progress(stage, **values):
        if lost_lease.is_set():
            raise RuntimeError("Job lease was lost")
        with factory.begin() as session:
            updated = session.execute(update(Job).where(
                Job.id == job_id, Job.lease_token == lease_token, Job.status == "running"
            ).values(stage=stage, progress=values, updated_at=now()))
            if updated.rowcount != 1:
                raise RuntimeError("Job lease was lost")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with factory() as session:
            job = session.get(Job, job_id)
            author, inputs, owner_id, retry = job.author, job.inputs, job.owner_id, job.attempts > 1
        root = settings.storage_root.resolve() / "jobs" / job_id
        results = Pipeline(settings, root, provider=provider, progress=progress).run(inputs, author, retry=retry)
        with factory.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job.lease_token != lease_token:
                raise RuntimeError("Job lease was lost")
            for result in results:
                exam = session.get(ExamSet, result["set_id"])
                if exam is None:
                    exam = ExamSet(id=result["set_id"], job_id=job_id, owner_id=owner_id,
                                   title=result["title"], directory=str(Path(result["directory"]).relative_to(
                                       settings.storage_root.resolve())), question_count=result["question_count"],
                                   error_count=result["error_count"], llm_mode=settings.llm_mode)
                    session.add(exam)
                else:
                    exam.error_count = result["error_count"]
                    exam.question_count = result["question_count"]
                    exam.state = "draft"
            job.results = [{k: v for k, v in r.items() if k != "directory"} for r in results]
            job.status = "completed_with_errors" if any(r["error_count"] for r in results) else "completed"
            job.stage, job.error = "done", None
            job.lease_until, job.lease_token, job.updated_at = None, None, now()
    except Exception as exc:
        with factory.begin() as session:
            session.execute(update(Job).where(Job.id == job_id, Job.lease_token == lease_token).values(
                status="failed", error=safe_error(exc), lease_until=None, lease_token=None, updated_at=now()))
        logger.error("Job %s failed: %s", job_id, safe_error(exc))
    finally:
        stopped.set()
        thread.join(timeout=5)


def run_once(factory, settings, provider=None) -> bool:
    claimed = claim_job(factory, settings)
    if claimed is None:
        return False
    process_job(factory, settings, *claimed, provider=provider)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    factory = session_factory(settings.database_url)
    shutdown = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown.set())
    while not shutdown.is_set():
        try:
            if not run_once(factory, settings):
                shutdown.wait(settings.worker_poll_seconds)
        except Exception:
            logger.exception("Worker database poll failed")
            shutdown.wait(5)


if __name__ == "__main__":
    main()
