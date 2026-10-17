import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def jst_iso(value: datetime) -> str:
    # SQLite test storage drops timezone information; application writes are JST.
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return value.astimezone(ZoneInfo("Asia/Tokyo")).isoformat()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        read_json(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Invalid artifact path")
    return path


def safe_error(exc: Exception) -> str:
    # Provider errors may contain requests and credentials: never persist those.
    if exc.__class__.__module__.startswith("google."):
        code = getattr(exc, "code", "unknown")
        return f"Gemini API error ({code})"
    return str(exc)[:2000]
