"""Offline validation of the supplied PDFs; never claims LLM extraction succeeded."""
import argparse
import re
from pathlib import Path

from pypdf import PdfReader

from medbase.pdfs import crop_images, merge_pdfs, pdf_chunks, render_page
from medbase.storage import atomic_json, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("work/sample-inspection"))
    args = parser.parse_args()
    names = ["tp240424-01a_01.pdf", "tp240424-01a_02.pdf", "tp240424-01seitou.pdf"]
    paths = [args.directory / name for name in names]
    hashes = {path.name: digest(path) for path in paths}
    documents = [{"document_id": str(i), "name": path.name, "pdf_path": str(path.resolve()), "role": role}
                 for i, (path, role) in enumerate(zip(paths, ["questions", "figures", "answers"], strict=True))]
    print("Integrating original PDFs without re-encoding page streams", flush=True)
    integration = merge_pdfs(documents, args.output)
    merged = args.output / "merged.pdf"
    target_page = next(p["merged_page"] for p in integration["pages"]
                       if p["source_file"] == names[1] and p["source_page"] == 6)
    assert target_page is not None
    question = {"q_id": 17, "images": [{"label": "別冊 No.2", "page": target_page,
                                         "box_2d": [65, 110, 770, 930]}]}
    crop = crop_images(merged, [question], args.output)
    assert crop["success"] == 1
    original_pixels = render_page(paths[1], 6, dpi=80)
    merged_pixels = render_page(merged, target_page, dpi=80)
    assert original_pixels.size == merged_pixels.size
    assert original_pixels.tobytes() == merged_pixels.tobytes(), "Merge changed rendered page contents"
    original_pixels.close()
    merged_pixels.close()
    text = "\n".join(p.extract_text() or "" for p in PdfReader(paths[2]).pages)
    official = {int(n): value for n, value in re.findall(r"\bA(\d{3})\s+([A-L]+|\d+(?:\.\d+)?)\b", text)}
    assert len(official) == 75 and official[75] == "600"
    print("Checking actual serialized size of analysis PDF parts", flush=True)
    chunks = pdf_chunks(merged, list(range(1, len(PdfReader(merged).pages) + 1)), args.output / "analysis")
    assert all(chunk.path.stat().st_size <= 40_000_000 and len(chunk.pages) <= 8 for chunk in chunks)
    assert hashes == {path.name: digest(path) for path in paths}
    report = {"mode": "offline-pdf-only", "llm_extraction_verified": False,
              "input_pages": [len(PdfReader(p).pages) for p in paths], "merged_pages": integration["total_pages"],
              "removed_pages": sum(p["merged_page"] is None for p in integration["pages"]),
              "analysis_parts": len(chunks), "max_part_bytes": max(p.path.stat().st_size for p in chunks),
              "raster_analysis_fallbacks": sum(p.raster_fallback for p in chunks),
              "A_official_answers": len(official), "A075_official_answer_text": official[75],
              "A017_source_figure_page": 6, "A017_merged_figure_page": target_page,
              "crop": question["images"][0]["file"], "rendered_figure_unchanged": True,
              "original_sha256": hashes}
    atomic_json(args.output / "verification.json", report)
    print(report, flush=True)


if __name__ == "__main__":
    main()
