import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select, text, update

from medbase.adapters import frontend_document
from medbase.config import Settings, get_settings
from medbase.db import ExamSet, Job, session_factory
from medbase.fixtures import prepare_mock
from medbase.identity import current_user
from medbase.pdfs import ALLOWED_EXTENSIONS
from medbase.schemas import validate_final
from medbase.storage import inside, jst_iso, now, read_json


def db_session(request: Request):
    with request.app.state.factory() as session:
        yield session


def create_app(settings: Settings | None = None, factory=None) -> FastAPI:
    settings = settings or get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app):
        app.state.factory = factory or session_factory(settings.database_url)
        yield

    app = FastAPI(title="MedBase backend", version="0.1.0", lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.middleware("http")
    async def content_limit(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/api/v1/jobs":
            length = request.headers.get("content-length")
            if length:
                try:
                    if int(length) > settings.max_upload_mb * 1024 * 1024 + 1_000_000:
                        from fastapi.responses import JSONResponse
                        return JSONResponse({"detail": "Upload exceeds MAX_UPLOAD_MB"}, status_code=413)
                except ValueError:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        return await call_next(request)

    def job_owned(session, job_id, owner):
        job = session.get(Job, str(job_id))
        if job is None or job.owner_id != owner:
            raise HTTPException(404, "Job not found")
        return job

    def set_readable(session, set_id, owner, write=False):
        exam = session.get(ExamSet, str(set_id))
        if exam is None or (exam.owner_id != owner and (write or exam.state != "published" or exam.visibility != "shared")):
            raise HTTPException(404, "Set not found")
        return exam

    def set_dir(exam):
        return inside(settings.storage_root, exam.directory)

    def meta(exam):
        return {"source_file": exam.id, "id": exam.id, "title": exam.title, "exam_name": exam.title,
                "count": exam.question_count, "error_count": exam.error_count, "state": exam.state,
                "visibility": exam.visibility, "llm_mode": exam.llm_mode,
                "pdf_url": f"/api/v1/sets/{exam.id}/pdf"}

    def serialize_job(job):
        return {"id": job.id, "status": job.status, "stage": job.stage, "progress": job.progress,
                "results": job.results, "error": job.error, "author": job.author,
                "created_at": jst_iso(job.created_at), "updated_at": jst_iso(job.updated_at),
                "attempts": job.attempts, "llm_mode": job.llm_mode}

    @app.get("/health/live")
    def live():
        return {"status": "ok", "llm_mode": settings.llm_mode}

    @app.get("/health/ready")
    def ready(session=Depends(db_session)):
        try:
            session.execute(text("SELECT 1"))
            session.execute(select(Job.id).limit(1))
        except Exception:
            raise HTTPException(503, "Database or migrations unavailable") from None
        return {"status": "ready"}

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(
        files: Annotated[list[UploadFile], File()], author: Annotated[str, Form()],
        owner: str = Depends(current_user), session=Depends(db_session),
    ):
        author = author.strip()
        if not author:
            raise HTTPException(422, "作者名を入力してください")
        if len(author) > 512:
            raise HTTPException(422, "作者名は512文字以内にしてください")
        if settings.llm_mode == "gemini" and not settings.gemini_api_key.get_secret_value():
            raise HTTPException(503, "GEMINI_API_KEYを環境変数に設定してください")
        job_id = str(uuid.uuid4())
        root = settings.storage_root.resolve() / "jobs" / job_id
        incoming = root / "inputs"
        incoming.mkdir(parents=True)
        inputs, total = [], 0
        try:
            for upload in files:
                name = (upload.filename or "").replace("\\", "/").split("/")[-1]
                suffix = Path(name).suffix.lower()
                if suffix not in ALLOWED_EXTENSIONS:
                    raise HTTPException(415, f"Unsupported file: {name}")
                document_id = str(uuid.uuid4())
                target = incoming / (document_id + suffix)
                length = 0
                with target.open("wb") as stream:
                    while block := await upload.read(1024 * 1024):
                        length += len(block)
                        total += len(block)
                        if length > settings.max_file_mb * 1024 * 1024 or total > settings.max_upload_mb * 1024 * 1024:
                            raise HTTPException(413, "Upload exceeds configured file/total size limit")
                        stream.write(block)
                inputs.append({"document_id": document_id, "name": name, "path": target.relative_to(root).as_posix()})
            if settings.llm_mode == "mock":
                try:
                    prepare_mock(root, inputs)
                except Exception:
                    raise HTTPException(422, "Mock mode requires the synthetic smoke.pdf fixture") from None
            job = Job(id=job_id, owner_id=owner, author=author, inputs=inputs, llm_mode=settings.llm_mode)
            session.add(job)
            session.commit()
        except Exception:
            # The server constructed this exact UUID path; never accept a client path here.
            if not root.is_relative_to(settings.storage_root.resolve() / "jobs"):
                raise RuntimeError("Refusing cleanup outside job storage") from None
            shutil.rmtree(root)
            raise
        finally:
            for upload in files:
                await upload.close()
        return {"id": job_id, "status": "queued", "url": f"/api/v1/jobs/{job_id}"}

    @app.get("/api/v1/jobs")
    def list_jobs(owner: str = Depends(current_user), session=Depends(db_session),
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        jobs = session.scalars(select(Job).where(Job.owner_id == owner).order_by(Job.created_at.desc())
                               .offset(offset).limit(limit))
        return {"jobs": [serialize_job(j) for j in jobs]}

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        return serialize_job(job_owned(session, job_id, owner))

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        job = session.scalar(select(Job).where(Job.id == str(job_id)).with_for_update())
        if not job or job.owner_id != owner:
            raise HTTPException(404, "Job not found")
        if job.status not in {"failed", "completed_with_errors"}:
            raise HTTPException(409, "Only failed or partially failed jobs can be retried")
        if job.llm_mode != settings.llm_mode:
            raise HTTPException(409, "Restore the job's LLM_MODE before retrying")
        job.status, job.error, job.stage, job.updated_at = "queued", None, "queued", now()
        session.execute(update(ExamSet).where(ExamSet.job_id == job.id).values(state="draft"))
        session.commit()
        return {"id": job.id, "status": job.status}

    @app.get("/api/v1/jobs/{job_id}/logs/{name}")
    def job_log(job_id: uuid.UUID, name: str, owner: str = Depends(current_user), session=Depends(db_session)):
        job_owned(session, job_id, owner)
        if name not in {"input_log.json", "classification.json", "llm_usage.jsonl"}:
            raise HTTPException(404, "Log not found")
        path = settings.storage_root.resolve() / "jobs" / str(job_id) / name
        if not path.is_file():
            raise HTTPException(404, "Log not available yet")
        return FileResponse(path)

    @app.get("/api/v1/sets")
    def list_sets(include_drafts: bool = False, owner: str = Depends(current_user), session=Depends(db_session)):
        visible = or_(and_(ExamSet.owner_id == owner, ExamSet.state == "published"),
                      and_(ExamSet.state == "published", ExamSet.visibility == "shared"))
        if include_drafts:
            visible = or_(visible, ExamSet.owner_id == owner)
        return {"sets": [meta(e) for e in session.scalars(select(ExamSet).where(visible).order_by(ExamSet.created_at))]}

    class PublishRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        visibility: Literal["private", "shared"]

    @app.post("/api/v1/sets/{set_id}/publish")
    def publish(set_id: uuid.UUID, body: PublishRequest, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner, write=True)
        # Retrying a job and publishing its old results must not race.
        job = session.scalar(select(Job).where(Job.id == exam.job_id).with_for_update())
        if job is None or job.status not in {"completed", "completed_with_errors"} or exam.error_count:
            raise HTTPException(409, "Complete the job and resolve all errors before publishing")
        directory = set_dir(exam)
        result = read_json(directory / "result.json")
        validate_final(result, directory)
        if any(q["status"] != "ok" for q in result["questions"]):
            raise HTTPException(409, "Set contains errors")
        exam.state, exam.visibility = "published", body.visibility
        session.commit()
        return meta(exam)

    @app.get("/api/v1/sets/{set_id}/result")
    def get_result(set_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner)
        return FileResponse(set_dir(exam) / "result.json", media_type="application/json", filename="result.json")

    @app.get("/api/v1/sets/{set_id}/download")
    def download(set_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner)
        return FileResponse(set_dir(exam) / "bundle.zip", media_type="application/zip", filename=f"medbase-{exam.id}.zip")

    @app.get("/api/v1/sets/{set_id}/questions")
    def questions(set_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner)
        return frontend_document(read_json(set_dir(exam) / "result.json"), exam.id)

    @app.get("/api/v1/sets/{set_id}/pdf")
    def pdf(set_id: uuid.UUID, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner)
        return FileResponse(set_dir(exam) / "merged.pdf", media_type="application/pdf")

    @app.get("/api/v1/sets/{set_id}/images/{q_id}/{index}")
    def image(set_id: uuid.UUID, q_id: int, index: int, owner: str = Depends(current_user), session=Depends(db_session)):
        exam = set_readable(session, set_id, owner)
        directory = set_dir(exam)
        result = read_json(directory / "result.json")
        q = next((q for q in result["questions"] if q["q_id"] == q_id), None)
        if q is None or index < 0 or index >= len(q.get("images", [])):
            raise HTTPException(404, "Image not found")
        relative = q["images"][index].get("file", "")
        if not relative or not inside(directory, relative).is_file():
            raise HTTPException(404, "Image unavailable")
        return FileResponse(inside(directory, relative), media_type="image/png")

    return app


app = create_app()
