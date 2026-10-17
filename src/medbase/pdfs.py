import hashlib
import io
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import img2pdf
from PIL import Image, ImageChops, ImageOps
from pypdf import PdfReader, PdfWriter

from medbase.storage import atomic_json, digest, read_json

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".heif"}
ALLOWED_EXTENSIONS = {".pdf", *IMAGE_EXTENSIONS}


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def render_page(pdf: Path, page: int, dpi: int = 200) -> Image.Image:
    # PDF viewers use CropBox. Print-registration marks outside it are not page
    # content and must not shift the normalized image coordinates.
    command = ["pdftoppm", "-cropbox", "-f", str(page), "-l", str(page), "-r", str(dpi), "-singlefile", "-png", str(pdf)]
    result = subprocess.run(command, capture_output=True, timeout=180, check=False)
    if result.returncode or not result.stdout:
        raise ValueError(f"PDF render failed on page {page}")
    with Image.open(io.BytesIO(result.stdout)) as image:
        return image.convert("RGB")


def footer_only(image: Image.Image, text: str) -> bool:
    gray = image.convert("L")
    ink = ImageChops.difference(gray, Image.new("L", gray.size, 255)).point(lambda p: 255 if p > 30 else 0)
    if ink.getbbox() is None:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    known_footer = bool(lines) and all(
        re.fullmatch(r"[\d\sA-Z\-−—–:/.()]+", line) or re.search(r"\.indd|DKIX", line, re.I)
        for line in lines
    )
    if not known_footer:
        return False
    body = ink.crop((0, round(image.height * .04), image.width, round(image.height * .92)))
    if body.getbbox() is None:
        return True
    # Some print PDFs have no CropBox: registration crosses and InDesign
    # timestamps remain in MediaBox. Accept only a tiny, recognized lower-right
    # page-code line after excluding the outside registration-mark band.
    if not any("DKIX" in line for line in lines):
        return False
    inset_x, inset_y = round(image.width * .06), round(image.height * .06)
    interior = ink.crop((inset_x, inset_y, image.width - inset_x, image.height - inset_y))
    bounds = interior.getbbox()
    if bounds is None:
        return True
    x0, y0, x1, y1 = bounds
    x0, y0, y1 = x0 + inset_x, y0 + inset_y, y1 + inset_y
    return x0 >= image.width * .5 and y0 >= image.height * .82 and y1 - y0 <= image.height * .035


def normalize_input(path: Path, target: Path) -> None:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Password-protected PDF")
        if not reader.pages:
            raise ValueError("PDF has no pages")
        shutil.copyfile(path, target)
        return
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Unsupported file format")
    if path.suffix.lower() in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        if oriented.mode not in {"RGB", "RGBA", "L"}:
            oriented = oriented.convert("RGB")
        encoded = io.BytesIO()
        oriented.save(encoded, format="PNG")
        target.write_bytes(img2pdf.convert(encoded.getvalue()))


def merge_pdfs(documents: list[dict], directory: Path) -> dict:
    """Keep page streams intact; rendering is only for deduplication and blank detection."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "merged.pdf"
    ordered = sorted(documents, key=lambda d: (
        {"questions": 0, "figures": 1, "answers": 2}[d["role"]], natural_key(d["name"])
    ))
    writer = PdfWriter()
    log = {"files": [], "pages": [], "excluded": []}
    file_hashes = set()
    page_hashes = set()
    readers = []
    for document in ordered:
        path = Path(document["pdf_path"])
        try:
            file_hash = digest(path)
            if file_hash in file_hashes:
                log["excluded"].append({"file": document["name"], "reason": "duplicate file"})
                continue
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Password-protected PDF")
            readers.append(reader)
            file_hashes.add(file_hash)
        except Exception as exc:
            log["excluded"].append({"file": document["name"], "reason": str(exc)[:200]})
            continue
        retained = []
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            preview = render_page(path, index, dpi=60)
            try:
                fingerprint = hashlib.sha256(str(preview.size).encode() + preview.tobytes()).hexdigest()
                reason = "blank page" if footer_only(preview, text) else (
                    "duplicate page" if fingerprint in page_hashes else None
                )
            finally:
                preview.close()
            record = {"document_id": document["document_id"], "source_file": document["name"],
                      "source_page": index, "role": document["role"]}
            if reason:
                log["pages"].append({**record, "merged_page": None, "reason": reason})
                continue
            page_hashes.add(fingerprint)
            writer.add_page(page)
            merged_page = len(writer.pages)
            retained.append(merged_page)
            log["pages"].append({**record, "merged_page": merged_page})
        log["files"].append({"file": document["name"], "document_id": document["document_id"],
                             "merged_pages": retained})
    if not writer.pages:
        raise ValueError("No usable pages remain after PDF integration")
    if len(ordered) == 1 and len(writer.pages) == len(readers[0].pages):
        shutil.copyfile(ordered[0]["pdf_path"], target)
    else:
        with target.open("wb") as stream:
            writer.write(stream)
    log["total_pages"] = len(writer.pages)
    atomic_json(directory / "pdf_merge_log.json", log)
    return log


def apply_rotations(pdf: Path, rotations: dict[int, int]) -> None:
    if not rotations:
        return
    reader = PdfReader(pdf)
    writer = PdfWriter()
    for number, page in enumerate(reader.pages, 1):
        if number in rotations:
            page.rotate(rotations[number])
        writer.add_page(page)
    temporary = pdf.with_suffix(".rotated.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(pdf)


@dataclass
class PdfChunk:
    path: Path
    pages: list[int]
    raster_fallback: bool = False


def pdf_chunks(pdf: Path, pages: list[int], directory: Path, max_pages=8, max_bytes=40_000_000,
               raster=False) -> list[PdfChunk]:
    """Split by actual serialized size, including shared PDF resources."""
    directory.mkdir(parents=True, exist_ok=True)
    stat = pdf.stat()
    identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "pages": pages,
                "max_pages": max_pages, "max_bytes": max_bytes, "raster": raster}
    manifest = directory / "chunks.json"
    if manifest.is_file():
        try:
            saved = read_json(manifest)
            parts = saved["parts"]
            if saved["source"] == identity and [p for part in parts for p in part["pages"]] == pages:
                if all(re.fullmatch(r"part_\d{4}\.pdf", part["name"])
                       and (directory / part["name"]).stat().st_size == part["size"] for part in parts):
                    return [PdfChunk(directory / part["name"], part["pages"], part["raster_fallback"])
                            for part in parts]
        except (ValueError, OSError, KeyError, TypeError):
            pass
    reader = PdfReader(pdf)
    if len(set(pages)) != len(pages) or any(p < 1 or p > len(reader.pages) for p in pages):
        raise ValueError("Invalid chunk page list")
    chunks = []
    raster_pages = {}

    def page_png(number):
        if number not in raster_pages:
            image = render_page(pdf, number, dpi=200)
            try:
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                raster_pages[number] = encoded.getvalue()
            finally:
                image.close()
        return raster_pages[number]

    def split(batch: list[int]):
        if raster:
            content = img2pdf.convert(*[page_png(number) for number in batch])
        else:
            writer = PdfWriter()
            for number in batch:
                writer.add_page(reader.pages[number - 1])
            buffer = io.BytesIO()
            writer.write(buffer)
            content = buffer.getvalue()
        if len(content) > max_bytes and len(batch) > 1:
            middle = len(batch) // 2
            split(batch[:middle])
            split(batch[middle:])
            return
        fallback = raster
        if len(content) > max_bytes:
            content = img2pdf.convert(page_png(batch[0]))
            fallback = True
            if len(content) > max_bytes:
                raise ValueError(f"Analysis copy of page {batch[0]} exceeds the Gemini size limit")
        target = directory / f"part_{len(chunks):04d}.pdf"
        target.write_bytes(content)
        chunks.append(PdfChunk(target, batch, fallback))

    for offset in range(0, len(pages), max_pages):
        split(pages[offset:offset + max_pages])
    atomic_json(manifest, {"source": identity, "parts": [
        {"name": chunk.path.name, "pages": chunk.pages, "size": chunk.path.stat().st_size,
         "raster_fallback": chunk.raster_fallback} for chunk in chunks
    ]})
    return chunks


def crop_images(pdf: Path, questions: list[dict], directory: Path) -> dict:
    page_count = len(PdfReader(pdf).pages)
    clip_dir = directory / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    log = {"success": 0, "failed": 0, "errors": []}
    # Render one page at a time, even when many questions reference the same page.
    references: dict[int, list] = {}
    for question in questions:
        for index, image in enumerate(question.get("images", [])):
            references.setdefault(image["page"], []).append((question["q_id"], index, image))
    for page, items in references.items():
        rendered = None
        render_error = None
        try:
            if not 1 <= page <= page_count:
                raise ValueError("Image page outside PDF")
            rendered = render_page(pdf, page, dpi=200)
        except Exception as exc:
            render_error = str(exc)
        for q_id, index, image in items:
            image["file"] = ""
            try:
                if render_error:
                    raise ValueError(render_error)
                y0, x0, y1, x1 = image["box_2d"]
                x0, x1 = sorted(max(0, min(1000, v)) for v in (x0, x1))
                y0, y1 = sorted(max(0, min(1000, v)) for v in (y0, y1))
                if x0 == x1 or y0 == y1:
                    raise ValueError("Zero-size image box")
                image["box_2d"] = [y0, x0, y1, x1]
                width, height = rendered.size
                box = (max(0, math.floor((x0 / 1000 - .02) * width)),
                       max(0, math.floor((y0 / 1000 - .02) * height)),
                       min(width, math.ceil((x1 / 1000 + .02) * width)),
                       min(height, math.ceil((y1 / 1000 + .02) * height)))
                relative = f"clips/q{q_id}_f{index}.png"
                with rendered.crop(box) as clipped:
                    clipped.save(directory / relative, format="PNG")
                image["file"] = relative
                log["success"] += 1
            except Exception as exc:
                log["failed"] += 1
                log["errors"].append({"q_id": q_id, "label": image["label"], "reason": str(exc)})
        if rendered:
            rendered.close()
    atomic_json(directory / "crop_log.json", log)
    return log
