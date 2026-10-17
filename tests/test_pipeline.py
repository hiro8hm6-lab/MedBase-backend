import copy
import json
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from medbase.fixtures import fixture_data, prepare_mock
from medbase.llm import MockProvider
from medbase.pdfs import crop_images, merge_pdfs, normalize_input, pdf_chunks, render_page
from medbase.pipeline import CallError, Pipeline, combine
from medbase.schemas import Answer, Question, QuestionBatch, validate_answer
from medbase.storage import digest, read_json


def pipeline_root(tmp_path, sample):
    root = tmp_path / "run"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "smoke.pdf").write_bytes(sample.read_bytes())
    inputs = [{"document_id": "synthetic", "name": "smoke.pdf", "path": "inputs/smoke.pdf"}]
    prepare_mock(root, inputs)
    return root, inputs


def test_all_stages_and_resume(settings, sample, tmp_path):
    root, inputs = pipeline_root(tmp_path, sample)
    provider = MockProvider(root / "mock_fixture.json")
    result = Pipeline(settings, root, provider).run(inputs, "  山田太郎、佐藤花子  ")[0]
    directory = Path(result["directory"])
    final = read_json(directory / "result.json")
    assert final["author"] == "山田太郎、佐藤花子"
    assert final["total_questions"] == 4
    assert final["generated_at"].endswith("+09:00")
    assert all(q["status"] == "ok" for q in final["questions"])
    assert final["questions"][1]["answer_text"] == "600"
    assert "options" not in final["questions"][1]
    assert "option_explanations" not in final["questions"][1]
    assert "series_index" not in final["questions"][0]
    assert final["questions"][3]["depends_on"] == [3]
    assert digest(directory / "merged.pdf") == digest(sample)  # Single PDF is passed through unchanged.
    clip_a, clip_b = directory / "clips/q3_f0.png", directory / "clips/q4_f0.png"
    assert clip_a.is_file() and clip_b.is_file() and digest(clip_a) == digest(clip_b)
    assert not (directory / "result.json").read_bytes().startswith(b"\xef\xbb\xbf")
    with zipfile.ZipFile(directory / "bundle.zip") as archive:
        assert "clips/q4_f0.png" in archive.namelist()
        assert json.loads(archive.read("result.json"))["total_questions"] == 4

    class NoCalls:
        def generate(self, *args, **kwargs):
            raise AssertionError("A completed checkpoint should be reused")

    resumed = Pipeline(settings, root, NoCalls()).run(inputs, "author")
    assert resumed[0]["error_count"] == 0


def test_failed_index_request_preserves_successful_checkpoints(settings, sample, tmp_path):
    root, inputs = pipeline_root(tmp_path, sample)

    class InterruptedPipeline(Pipeline):
        def index(self, pdf, directory, integration, section):
            from medbase.storage import atomic_json
            atomic_json(directory / "checkpoints/index_0_0_0.json", {"saved": True})
            raise CallError("Gemini API error (429)")

    with pytest.raises(CallError):
        InterruptedPipeline(settings, root).run(inputs, "author")
    assert len(list(root.rglob("index_0_0_0.json"))) == 1


def test_chunk_resume_reuses_saved_analysis(sample, tmp_path, monkeypatch):
    expected = pdf_chunks(sample, [1, 2, 3], tmp_path / "parts", max_pages=2)

    def fail_reader(*args, **kwargs):
        raise AssertionError("Saved analysis should not be reserialized")

    monkeypatch.setattr("medbase.pdfs.PdfReader", fail_reader)
    assert pdf_chunks(sample, [1, 2, 3], tmp_path / "parts", max_pages=2) == expected


def test_raster_analysis_preserves_page_mapping(sample, tmp_path):
    original_hash = digest(sample)
    parts = pdf_chunks(sample, [1, 3], tmp_path / "raster", raster=True)
    assert len(parts) == 1 and parts[0].pages == [1, 3] and parts[0].raster_fallback
    assert len(PdfReader(parts[0].path).pages) == 2
    assert digest(sample) == original_hash


def test_partial_failure_and_retry_only_failed_groups(settings, sample, tmp_path):
    root, inputs = pipeline_root(tmp_path, sample)

    class FailingProvider(MockProvider):
        calls = Counter()
        fail = True

        def generate(self, task, prompt, schema, files, context):
            if task == "explain":
                first = context["questions"][0]["q_id"]
                self.calls[first] += 1
                if first == 2 and self.fail:
                    return {"answers": []}
            return super().generate(task, prompt, schema, files, context)

    provider = FailingProvider(root / "mock_fixture.json")
    result = Pipeline(settings, root, provider).run(inputs, "author")[0]
    directory = Path(result["directory"])
    final = read_json(directory / "result.json")
    assert result["error_count"] == 1 and final["total_questions"] == 4
    assert final["questions"][1]["status"] == "error"
    assert "explanation" not in final["questions"][1]
    assert provider.calls == {1: 1, 2: 4, 3: 1}
    provider.fail = False
    assert Pipeline(settings, root, provider).run(inputs, "author", retry=True)[0]["error_count"] == 0
    assert provider.calls == {1: 1, 2: 5, 3: 1}


def test_schema_invariants():
    fixture = fixture_data()
    QuestionBatch.model_validate({"questions": fixture["questions"]})
    for q, a in zip(fixture["questions"], fixture["answers"], strict=True):
        validate_answer(Question.model_validate(q), Answer.model_validate(a))
    bad = copy.deepcopy(fixture["answers"][0])
    bad["answer"] = ["b"]
    with pytest.raises(ValueError, match="differs from official"):
        validate_answer(Question.model_validate(fixture["questions"][0]), Answer.model_validate(bad))
    q = copy.deepcopy(fixture["questions"][0])
    q["q_id"] = "1"
    with pytest.raises(ValueError):
        Question.model_validate(q)
    q = copy.deepcopy(fixture["questions"][0])
    q["q_text"] += "\nline"
    with pytest.raises(ValueError):
        Question.model_validate(q)
    with pytest.raises(ValueError, match="Incomplete series"):
        QuestionBatch.model_validate({"questions": fixture["questions"][2:3]})


def test_merge_conflicting_keys_and_missing_answers_are_partial_errors(tmp_path):
    (tmp_path / "merged.pdf").write_bytes(b"not needed by JSON merger")
    q = fixture_data()["questions"][:2]
    answer = {**fixture_data()["answers"][0], "q_text": "unexpected overwrite"}
    result = combine(q, {1: answer}, {}, tmp_path, "exam", "author", {})
    assert result["questions"][0]["q_text"] == q[0]["q_text"]
    assert "Duplicate keys" in result["questions"][0]["error"]
    assert result["questions"][1]["status"] == "error"
    with pytest.raises(ValueError, match="作者名"):
        combine(q, {}, {}, tmp_path, "exam", " ", {})


def test_crop_clamps_swaps_margin_and_continues(sample, tmp_path):
    questions = [{"q_id": 1, "images": [
        {"label": "reversed", "page": 1, "box_2d": [1100, 900, -100, 100]},
        {"label": "zero", "page": 1, "box_2d": [500, 100, 500, 200]},
        {"label": "outside", "page": 99, "box_2d": [0, 0, 1000, 1000]},
    ]}]
    log = crop_images(sample, questions, tmp_path)
    assert (log["success"], log["failed"]) == (1, 2)
    assert questions[0]["images"][1]["file"] == questions[0]["images"][2]["file"] == ""
    page = render_page(sample, 1, 200)
    with Image.open(tmp_path / "clips/q1_f0.png") as cropped:
        assert cropped.height == page.height
        assert abs(cropped.width - page.width * .84) < 3
    page.close()


def test_pdf_merge_natural_order_blank_duplicate_and_rotation(sample, tmp_path):
    source = PdfReader(sample)
    docs = []
    for number, page_index in [(10, 1), (2, 0)]:
        writer = PdfWriter()
        writer.add_page(source.pages[page_index])
        if number == 2:
            writer.add_blank_page(width=600, height=800)
            writer.add_page(source.pages[page_index])
        target = tmp_path / f"part{number}.pdf"
        with target.open("wb") as stream:
            writer.write(stream)
        docs.append({"document_id": str(number), "name": target.name, "pdf_path": str(target), "role": "questions"})
    output = tmp_path / "merged"
    log = merge_pdfs(docs, output)
    reader = PdfReader(output / "merged.pdf")
    assert len(reader.pages) == 2
    assert "A001" in reader.pages[0].extract_text()
    assert "A002" in reader.pages[1].extract_text()
    assert {p.get("reason") for p in log["pages"]} >= {"blank page", "duplicate page"}
    from medbase.pdfs import apply_rotations
    apply_rotations(output / "merged.pdf", {1: 90})
    assert PdfReader(output / "merged.pdf").pages[0].rotation == 90


def test_lossless_image_conversion_and_chunks(sample, tmp_path):
    png = tmp_path / "image.png"
    Image.new("RGB", (100, 80), "red").save(png)
    pdf = tmp_path / "image.pdf"
    normalize_input(png, pdf)
    assert len(PdfReader(pdf).pages) == 1
    parts = pdf_chunks(sample, [1, 2, 3], tmp_path / "chunks", max_pages=2, max_bytes=1_000_000)
    assert [p.pages for p in parts] == [[1, 2], [3]]
    assert all(p.path.stat().st_size <= 1_000_000 for p in parts)
    with pytest.raises(ValueError):
        pdf_chunks(sample, [99], tmp_path / "invalid")


def test_encrypted_pdf_excluded(sample, tmp_path):
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_page(PdfReader(sample).pages[0])
    writer.encrypt("secret")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ValueError, match="Password"):
        normalize_input(encrypted, tmp_path / "out.pdf")


def test_malformed_explanation_never_replaces_official_answer(tmp_path):
    fixture = fixture_data()
    bad = copy.deepcopy(fixture["answers"][0])
    bad["answer"] = ["b"]
    result = combine(fixture["questions"][:1], {1: bad}, {}, tmp_path, "exam", "author", {})
    assert result["questions"][0]["status"] == "error"
    assert result["questions"][0]["official_answer"] == ["a"]
    assert "answer" not in result["questions"][0]
