import re
from pathlib import Path
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from medbase.storage import inside

CHOICES = set("abcdefghijkl")
QUESTION_FIELDS = (
    "q_id", "exam_name", "page", "series_index", "series_total", "depends_on", "q_text",
    "options", "images", "official_answer", "official_answer_text",
)
ANSWER_FIELDS = (
    "image_findings", "answer", "answer_text", "generated_options", "generated_answer",
    "answer_source", "tags", "explanation", "option_explanations",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def clean(self) -> dict:
        return self.model_dump(exclude_none=True)


class ImageRef(StrictModel):
    label: str = Field(min_length=1)
    page: int = Field(ge=1)
    box_2d: list[int] = Field(min_length=4, max_length=4)
    file: str | None = None


class Question(StrictModel):
    q_id: int = Field(ge=1)
    exam_name: str
    page: int = Field(ge=1)
    series_index: int | None = Field(None, ge=1)
    series_total: int | None = Field(None, ge=2)
    depends_on: list[int] | None = None
    q_text: str = Field(min_length=1)
    options: dict[str, str] | None = None
    images: list[ImageRef] | None = Field(None, min_length=1)
    official_answer: list[str] | None = Field(None, min_length=1)
    official_answer_text: str | None = None

    @model_validator(mode="after")
    def validate_question(self):
        if "\n" in self.q_text or "\r" in self.q_text:
            raise ValueError("q_text must be a single continuous string")
        series = (self.series_index, self.series_total, self.depends_on)
        if any(v is not None for v in series):
            if any(v is None for v in series) or self.series_index > self.series_total:
                raise ValueError("All series fields must be present and consistent")
            if len(set(self.depends_on)) != len(self.depends_on):
                raise ValueError("Duplicate dependencies")
        if self.options is not None:
            if not self.options or list(self.options) != list("abcdefghijkl"[:len(self.options)]):
                raise ValueError("Options must be consecutive a-l; omit options for written questions")
            if not all(self.options.values()):
                raise ValueError("Empty option")
        if self.official_answer is not None:
            if not self.options or not set(self.official_answer) <= self.options.keys():
                raise ValueError("Official answer not in options")
            if len(set(self.official_answer)) != len(self.official_answer):
                raise ValueError("Duplicate official answer")
        if self.official_answer_text == "" and self.official_answer is None:
            raise ValueError("Omit official_answer_text when no official answer exists")
        return self


class QuestionBatch(StrictModel):
    questions: list[Question] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [q.q_id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate q_id")
        by_id = {q.q_id: q for q in self.questions}
        for q in self.questions:
            for dependency in q.depends_on or []:
                if dependency not in by_id or dependency >= q.q_id:
                    raise ValueError("Missing or forward dependency")
            if q.series_total is not None:
                start = q.q_id - q.series_index + 1
                group = [by_id.get(start + i) for i in range(q.series_total)]
                if any(p is None for p in group):
                    raise ValueError("Incomplete series")
                for index, member in enumerate(group, 1):
                    if member.series_index != index or member.series_total != q.series_total:
                        raise ValueError("Inconsistent series")
                if not set(q.depends_on or []) <= {p.q_id for p in group}:
                    raise ValueError("Dependency outside series")
        return self


class Finding(StrictModel):
    label: str
    finding: str = Field(min_length=1)


class Tags(StrictModel):
    diseases: list[str] | None = Field(None, min_length=1)
    keywords: list[str] = Field(min_length=5, max_length=10)
    question_type: Literal[
        "diagnosis", "treatment", "examination", "pathophysiology", "knowledge", "calculation", "ethics_law"
    ] | None = None

    @model_validator(mode="after")
    def unique_keywords(self):
        if len(set(self.keywords)) != len(self.keywords) or any(not k.strip() for k in self.keywords):
            raise ValueError("Keywords must be nonempty and unique")
        return self


class Answer(StrictModel):
    q_id: int = Field(ge=1)
    image_findings: list[Finding] | None = Field(None, min_length=1)
    answer: list[str] | None = Field(None, min_length=1)
    answer_text: str | None = Field(None, min_length=1)
    generated_options: dict[str, str] | None = None
    generated_answer: Literal["a", "b", "c"] | None = None
    answer_source: Literal["official", "generated"]
    tags: Tags
    explanation: str = Field(min_length=1)
    option_explanations: dict[str, str] | None = None


class AnswerBatch(StrictModel):
    answers: list[Answer] = Field(min_length=1)


def validate_answer(question: Question, answer: Answer) -> None:
    if question.q_id != answer.q_id:
        raise ValueError("Answer q_id mismatch")
    official = question.official_answer is not None or bool(question.official_answer_text)
    if answer.answer_source != ("official" if official else "generated"):
        raise ValueError("answer_source mismatch")
    if question.options:
        if not answer.answer or not set(answer.answer) <= question.options.keys():
            raise ValueError("Answer not in options")
        if len(set(answer.answer)) != len(answer.answer):
            raise ValueError("Duplicate answer")
        if any(v is not None for v in (answer.answer_text, answer.generated_options, answer.generated_answer)):
            raise ValueError("Choice questions must not have generated options or written answers")
        if set(answer.option_explanations or {}) != question.options.keys():
            raise ValueError("option_explanations keys must match original options")
        if question.official_answer is not None and set(answer.answer) != set(question.official_answer):
            raise ValueError("Answer differs from official answer")
    else:
        if answer.answer is not None or answer.option_explanations is not None or not answer.answer_text:
            raise ValueError("Invalid written answer")
        if set(answer.generated_options or {}) != {"a", "b", "c"} or not answer.generated_answer:
            raise ValueError("Written questions require three generated options")
        if any(not v for v in answer.generated_options.values()):
            raise ValueError("Empty generated option")
        if len(set(answer.generated_options.values())) != 3:
            raise ValueError("Generated options must be distinct")
        if question.official_answer_text and answer.answer_text != question.official_answer_text:
            raise ValueError("Written answer differs from official answer")
    expected_labels = [i.label for i in question.images or []]
    actual_labels = [i.label for i in answer.image_findings or []]
    if expected_labels != actual_labels:
        raise ValueError("image_findings labels/order differ from input")
    created = [answer.explanation, answer.answer_text or ""]
    created += list((answer.option_explanations or {}).values())
    created += list((answer.generated_options or {}).values())
    created += [f.finding for f in answer.image_findings or []]
    for text in created:
        if re.search(r"<\s*/?\s*(sup|sub)\b|\$[^$]+\$|\\(?:alpha|beta|frac|mathrm)\b", text):
            raise ValueError("Generated text must use Unicode, not LaTeX/HTML")
    if re.search(r"(?m)^\s*#{1,6}\s|^\s*\|.*\|\s*$", answer.explanation):
        raise ValueError("Headings and Markdown tables are not allowed in explanation")


class OkQuestion(Question, Answer):
    status: Literal["ok"]

    @model_validator(mode="after")
    def validate_join(self):
        q = Question.model_validate({k: v for k, v in self.clean().items() if k in QUESTION_FIELDS})
        a = Answer.model_validate({k: v for k, v in self.clean().items() if k in ("q_id", *ANSWER_FIELDS)})
        validate_answer(q, a)
        return self


class ErrorQuestion(Question):
    status: Literal["error"]
    error: str = Field(min_length=1)


class FinalDocument(StrictModel):
    exam_name: str
    source_file: str
    author: str = Field(min_length=1)
    generated_at: str
    total_questions: int = Field(ge=1)
    questions: list[Annotated[OkQuestion | ErrorQuestion, Field(discriminator="status")]]

    @model_validator(mode="after")
    def counts(self):
        if self.total_questions != len(self.questions) or not self.author.strip():
            raise ValueError("Invalid author or question count")
        ids = [q.q_id for q in self.questions]
        if ids != sorted(set(ids)):
            raise ValueError("Questions must be unique and sorted")
        return self


def validate_final(value: dict, directory: Path) -> None:
    Draft202012Validator(FinalDocument.model_json_schema()).validate(value)
    FinalDocument.model_validate(value)
    for question in value["questions"]:
        if question["status"] == "ok":
            for image in question.get("images", []):
                if not re.fullmatch(r"clips/q\d+_f\d+\.png", image.get("file", "")):
                    raise ValueError("Invalid or missing image file")
                if not inside(directory, image["file"]).is_file():
                    raise ValueError("Image file missing")


class Classification(StrictModel):
    document_id: str
    exam_key: str = Field(min_length=1)
    exam_name: str
    section: str
    role: Literal["questions", "figures", "answers"]


class ClassificationBatch(StrictModel):
    documents: list[Classification]


class IndexedQuestion(StrictModel):
    source_ref: str
    page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    series_key: str | None = None


class IndexedFigure(StrictModel):
    source_refs: list[str] = Field(min_length=1)
    label: str
    page: int = Field(ge=1)
    box_2d: list[int] = Field(min_length=4, max_length=4)


class IndexedOfficial(StrictModel):
    source_ref: str
    answer: list[str] | None = None
    answer_text: str | None = None


class Rotation(StrictModel):
    page: int = Field(ge=1)
    clockwise: Literal[90, 180, 270]


class PageIndex(StrictModel):
    questions: list[IndexedQuestion]
    figures: list[IndexedFigure]
    official_answers: list[IndexedOfficial]
    rotations: list[Rotation]
