from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from medbase.storage import atomic_json


def fixture_data() -> dict:
    questions = [
        {"q_id": 1, "exam_name": "Synthetic exam", "page": 1, "q_text": "What is 2 + 2?",
         "options": {"a": "4", "b": "5"}, "official_answer": ["a"], "official_answer_text": ""},
        {"q_id": 2, "exam_name": "Synthetic exam", "page": 2, "q_text": "Calculate 20 times 30.",
         "official_answer_text": "600"},
        {"q_id": 3, "exam_name": "Synthetic exam", "page": 3, "q_text": "Let x = 2. Calculate x + x.",
         "options": {"a": "4", "b": "5"}, "series_index": 1, "series_total": 2, "depends_on": [],
         "images": [{"label": "Synthetic diagram", "page": 3, "box_2d": [200, 100, 700, 900]}]},
        {"q_id": 4, "exam_name": "Synthetic exam", "page": 3, "q_text": "Let x = 2. Calculate x times x.",
         "options": {"a": "4", "b": "6"}, "series_index": 2, "series_total": 2, "depends_on": [3],
         "images": [{"label": "Synthetic diagram", "page": 3, "box_2d": [200, 100, 700, 900]}]},
    ]
    answers = []
    for q in questions:
        answer = {"q_id": q["q_id"], "answer_source": "official" if q["q_id"] <= 2 else "generated",
                  "tags": {"keywords": ["合成試験", "動作検証", "数値", "加算", "乗算"], "question_type": "calculation"},
                  "explanation": "【和訳】計算問題です。\n・これは外部APIを使わない合成テストです。"}
        if q.get("options"):
            answer.update(answer=["a"], option_explanations={"a": "計算結果と一致します。", "b": "計算結果と異なります。"})
        else:
            answer.update(answer_text="600", generated_options={"a": "600", "b": "500", "c": "700"},
                          generated_answer="a")
        if q.get("images"):
            answer["image_findings"] = [{"label": "Synthetic diagram", "finding": "動作検証用の長方形です。"}]
        answers.append(answer)
    return {"questions": questions, "answers": answers}


def create_fixture(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in ["A001: What is 2 + 2? a: 4 b: 5", "A002: Calculate 20 times 30. Official: 600",
                 "A003-A004: Let x = 2. Calculate x+x; x*x."]:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 16 Tf 45 740 Td ({text}) Tj ET\n0.2 0.5 0.7 rg 100 300 300 200 re f".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/MedBaseSynthetic": "smoke-v1", "/Title": "Synthetic MedBase smoke exam"})
    target = directory / "smoke.pdf"
    with target.open("wb") as stream:
        writer.write(stream)
    atomic_json(directory / "mock_fixture.json", fixture_data())
    return target


def prepare_mock(root: Path, inputs: list[dict]) -> None:
    if len(inputs) != 1:
        raise ValueError("Mock mode accepts one synthetic smoke PDF")
    reader = PdfReader(root / inputs[0]["path"])
    if (reader.metadata or {}).get("/MedBaseSynthetic") != "smoke-v1" or len(reader.pages) != 3:
        raise ValueError("Mock mode accepts only medbase make-fixture output, not real exam PDFs")
    atomic_json(root / "mock_fixture.json", fixture_data())
