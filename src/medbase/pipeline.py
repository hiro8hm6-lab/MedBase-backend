import json
import re
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.resources import files as resources
from pathlib import Path

import img2pdf
from pypdf import PdfReader

from medbase.config import Settings
from medbase.llm import GeminiProvider, MockProvider, Provider, wait_for_retry
from medbase.pdfs import (
    apply_rotations,
    crop_images,
    merge_pdfs,
    natural_key,
    normalize_input,
    pdf_chunks,
    render_page,
)
from medbase.schemas import (
    ANSWER_FIELDS,
    QUESTION_FIELDS,
    Answer,
    AnswerBatch,
    ClassificationBatch,
    FinalDocument,
    PageIndex,
    Question,
    QuestionBatch,
    validate_answer,
    validate_final,
)
from medbase.storage import atomic_json, now, read_json, safe_error


class CallError(ValueError):
    """A failed request must not invalidate other successful checkpoints."""


def prompt(name: str) -> str:
    return resources("medbase").joinpath(f"prompts/{name}.md").read_text(encoding="utf-8")


def source_ref(value: str) -> str:
    compact = re.sub(r"[\s_\-]", "", value).upper()
    match = re.fullmatch(r"([A-Z]*)(\d+)", compact)
    return f"{match[1]}{int(match[2])}" if match else compact


def question_groups(questions: list[dict]) -> list[list[dict]]:
    groups = []
    for question in questions:
        if question.get("series_index", 1) == 1:
            groups.append([])
        if not groups:
            raise ValueError("Series starts in the middle")
        groups[-1].append(question)
    return groups


def combine(questions: list[dict], answers: dict[int, dict], errors: dict[int, str],
            directory: Path, exam_name: str, author: str, retries: dict) -> dict:
    author = author.strip()
    if not author:
        raise ValueError("作者名を入力してください")
    QuestionBatch.model_validate({"questions": questions})
    result_questions = []
    for question in sorted(questions, key=lambda q: q["q_id"]):
        q_id = question["q_id"]
        answer = answers.get(q_id)
        error = errors.get(q_id)
        if answer and not error:
            try:
                overlap = (question.keys() & answer.keys()) - {"q_id"}
                if overlap:
                    raise ValueError("Duplicate keys: " + ", ".join(sorted(overlap)))
                validate_answer(Question.model_validate(question), Answer.model_validate(answer))
            except Exception as exc:
                error = safe_error(exc)
        if not answer and not error:
            error = "解答解説がありません"
        for image in question.get("images", []):
            if not image.get("file") or not (directory / image["file"]).is_file():
                error = error or "画像の切り出しに失敗しました"
        merged = {**question, **(answer if not error else {})}
        row = {key: merged[key] for key in (*QUESTION_FIELDS, *ANSWER_FIELDS) if key in merged}
        row["status"] = "error" if error else "ok"
        if error:
            row["error"] = error
        result_questions.append(row)
    result = {"exam_name": exam_name, "source_file": "merged.pdf", "author": author,
              "generated_at": now().isoformat(), "total_questions": len(result_questions),
              "questions": result_questions}
    validate_final(result, directory)
    atomic_json(directory / "result.json", result)
    atomic_json(directory / "result.schema.json", FinalDocument.model_json_schema())
    validate_final(read_json(directory / "result.json"), directory)
    ok = sum(q["status"] == "ok" for q in result_questions)
    summary = [f"total_questions: {len(result_questions)}", f"ok: {ok}", f"error: {len(result_questions)-ok}",
               "retries: " + json.dumps(retries, ensure_ascii=False)]
    summary.extend(f"q_id={q['q_id']}: {q['error']}" for q in result_questions if q["status"] == "error")
    (directory / "merge_log.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    temporary = directory / "bundle.tmp"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        names = ["merged.pdf", "result.json", "result.schema.json", "pdf_merge_log.json", "crop_log.json",
                 "merge_log.txt", "index.json", "questions.json", "questions_with_images.json", "answers.json"]
        for name in names:
            if (directory / name).is_file():
                archive.write(directory / name, name)
        for clip in sorted((directory / "clips").glob("*.png")):
            archive.write(clip, "clips/" + clip.name)
    temporary.replace(directory / "bundle.zip")
    return result


class Pipeline:
    def __init__(self, settings: Settings, root: Path, provider: Provider | None = None, progress=None):
        self.settings = settings
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.provider = provider or (
            MockProvider(root / "mock_fixture.json") if settings.llm_mode == "mock"
            else GeminiProvider(settings, root)
        )
        self.progress = progress or (lambda stage, **values: None)

    def call(self, task, text, schema, files, context, checkpoint: Path, validate=None):
        if checkpoint.is_file():
            try:
                cached = schema.model_validate(read_json(checkpoint)).model_dump(exclude_none=True)
                if validate:
                    validate(cached)
                return cached
            except (ValueError, OSError):
                checkpoint.unlink(missing_ok=True)
        last_error = None
        for attempt in range(4):
            try:
                value = self.provider.generate(task, text, schema, files, context)
                value = schema.model_validate(value).model_dump(exclude_none=True)
                if validate:
                    validate(value)
                atomic_json(checkpoint, value)
                return value
            except Exception as exc:
                last_error = safe_error(exc)
                if attempt < 3:
                    wait_for_retry(exc, attempt)
                # Retry validation errors too, but do not reveal raw provider exceptions.
                text += f"\nPrevious output was invalid: {last_error}. Correct the output."
        raise CallError(f"{task} failed after 4 attempts: {last_error}")

    def classify(self, inputs: list[dict]) -> list[dict]:
        normalized = self.root / "normalized"
        normalized.mkdir(exist_ok=True)
        documents, covers, excluded = [], [], []
        for item in inputs:
            source = self.root / item["path"]
            target = normalized / f"{item['document_id']}.pdf"
            try:
                if not target.is_file():
                    normalize_input(source, target)
                count = len(PdfReader(target).pages)
                cover_count = min(3, count)
                for page in range(1, cover_count + 1):
                    cover = normalized / f"{item['document_id']}_{page}.png"
                    if not cover.is_file():
                        image = render_page(target, page, dpi=80)
                        image.save(cover)
                        image.close()
                    covers.append(cover)
                documents.append({**item, "pdf_path": str(target.resolve()), "cover_count": cover_count})
            except Exception as exc:
                excluded.append({"file": item["name"], "reason": safe_error(exc)})
        atomic_json(self.root / "input_log.json", {"excluded": excluded})
        if not documents:
            raise ValueError("開ける入力ファイルがありません。input_log.jsonを確認してください")
        metadata = [{k: d[k] for k in ("document_id", "name", "cover_count")} for d in documents]
        def validate_classification(value):
            classifications = value["documents"]
            if {d["document_id"] for d in classifications} != {d["document_id"] for d in documents}:
                raise ValueError("Classification omitted or added input files")
            if len(classifications) != len(documents):
                raise ValueError("Duplicate document classification")

        classifications = self.call("classify", prompt("classify"), ClassificationBatch, covers,
                                    {"documents": metadata}, self.root / "classification.json",
                                    validate_classification)["documents"]
        by_id = {d["document_id"]: d for d in classifications}
        return [{**d, **by_id[d["document_id"]]} for d in documents]

    def run(self, inputs: list[dict], author: str, retry=False) -> list[dict]:
        if not author.strip():
            raise ValueError("作者名を入力してください")
        self.progress("integrate", completed=0)
        documents = self.classify(inputs)
        groups: dict[tuple[str, str], list[dict]] = {}
        for d in documents:
            if d["role"] != "answers":
                groups.setdefault((d["exam_key"], d["section"]), []).append(d)
        if not groups:
            raise ValueError("問題冊子がありません")
        results = []
        for (exam_key, section), members in groups.items():
            if not any(d["role"] == "questions" for d in members):
                raise ValueError("図版別冊に対応する問題冊子を特定できませんでした")
            members += [d for d in documents if d["role"] == "answers" and d["exam_key"] == exam_key
                        and d["section"] in (section, "")]
            set_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.root.name}:{exam_key}:{section}"))
            directory = self.root / "sets" / set_id
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "pdf_merge_log.json"
            if not log_path.is_file():
                merge_pdfs(members, directory)
            merged = directory / "merged.pdf"
            try:
                index = self.index(merged, directory, read_json(log_path), section)
            except CallError:
                raise
            except ValueError:
                # A semantic indexing error must be recoverable by the retry API.
                for failed_checkpoint in (directory / "checkpoints").glob("index_*.json"):
                    failed_checkpoint.unlink()
                raise
            questions_path = directory / "questions.json"
            if not questions_path.is_file():
                questions = self.extract(merged, directory, index)
                atomic_json(questions_path, {"questions": questions})
            questions = read_json(questions_path)["questions"]
            self.progress("crop", set_id=set_id, total=len(questions))
            cropped_path = directory / "questions_with_images.json"
            if not cropped_path.is_file() or retry:
                crop_images(merged, questions, directory)
                atomic_json(cropped_path, {"questions": questions})
            questions = read_json(cropped_path)["questions"]
            answers, errors, retries = self.explain(questions, directory, retry)
            self.progress("combine", set_id=set_id, total=len(questions))
            exam_name = next((q["exam_name"] for q in questions if q["exam_name"]), members[0]["exam_name"])
            result = combine(questions, answers, errors, directory, exam_name, author, retries)
            errors_count = sum(q["status"] == "error" for q in result["questions"])
            results.append({"set_id": set_id, "title": exam_name or "名称未設定の試験", "directory": str(directory),
                            "question_count": len(questions), "error_count": errors_count})
        return results

    def index(self, pdf, directory, integration, section):
        checkpoint = directory / "index.json"
        if checkpoint.is_file():
            return read_json(checkpoint)
        self.progress("index", section=section)
        page_count = len(PdfReader(pdf).pages)
        roles = {p["merged_page"]: p["role"] for p in integration["pages"] if p["merged_page"]}
        first_pass = 1 if integration.get("rotations_clockwise") else 0
        for pass_number in range(first_pass, 2):
            collected = {"questions": [], "figures": [], "official_answers": [], "rotations": []}
            indexed_pages = set()
            # Overlap physical pages so an index pass sees cross-page continuations.
            for offset in range(0, page_count, 7):
                pages = list(range(offset + 1, min(page_count, offset + 8) + 1))
                chunks = pdf_chunks(pdf, pages, directory / "analysis" / f"index_{pass_number}_{offset}",
                                    max_pages=self.settings.chunk_max_pages, max_bytes=self.settings.chunk_max_bytes,
                                    raster=any(roles[p] == "figures" for p in pages))
                for number, chunk in enumerate(chunks):
                    context = {"pages": chunk.pages, "parts": [chunk.pages], "roles": roles, "section": section,
                               "known_questions": collected["questions"]}
                    value = self.call("index", prompt("index"), PageIndex, [chunk.path], context,
                                      directory / "checkpoints" / f"index_{pass_number}_{offset}_{number}.json")
                    for key in collected:
                        collected[key].extend(value[key])
                    indexed_pages.update(chunk.pages)
                    self.progress("index", completed=len(indexed_pages), total=page_count,
                                  orientation_pass=pass_number + 1)
            rotations = {}
            for rotation in collected["rotations"]:
                page = rotation["page"]
                if not 1 <= page <= page_count:
                    raise ValueError("Rotation page outside PDF")
                if page in rotations and rotations[page] != rotation["clockwise"]:
                    raise ValueError("Conflicting page orientations")
                rotations[page] = rotation["clockwise"]
            if rotations:
                if pass_number:
                    raise ValueError("Page orientation could not be resolved")
                # A crash between rewriting the PDF and saving its journal must
                # never apply the same rotation twice on a resumed job.
                original = directory / "merged.before_rotation.pdf"
                if not original.is_file():
                    shutil.copyfile(pdf, original)
                else:
                    shutil.copyfile(original, pdf)
                apply_rotations(pdf, rotations)
                integration["rotations_clockwise"] = rotations
                atomic_json(directory / "pdf_merge_log.json", integration)
                continue
            break
        indexed, official, figures = {}, {}, []
        for item in collected["questions"]:
            item["source_ref"] = source_ref(item["source_ref"])
            if not 1 <= item["page"] <= item["end_page"] <= page_count:
                raise ValueError("Question index page outside PDF")
            if roles[item["page"]] != "questions":
                raise ValueError("Question was indexed from a figure/answer booklet")
            existing = indexed.get(item["source_ref"])
            if existing and existing["page"] != item["page"]:
                raise ValueError("Conflicting question index; refusing to discard duplicate source numbers")
            if existing:
                item["end_page"] = max(existing["end_page"], item["end_page"])
                item["series_key"] = item.get("series_key") or existing.get("series_key")
            indexed[item["source_ref"]] = item
        for item in collected["official_answers"]:
            ref = source_ref(item["source_ref"])
            if ref in official and official[ref] != item:
                previous = {**official[ref], "source_ref": ref}
                if previous != {**item, "source_ref": ref}:
                    raise ValueError("Conflicting official answers")
            official[ref] = {**item, "source_ref": ref}
        ordered = sorted(indexed.values(), key=lambda q: (q["page"], natural_key(q["source_ref"])))
        if not ordered:
            raise ValueError("No real questions found")
        if section:
            expected = {ref for ref in official if re.match(rf"^{re.escape(section.upper())}\d+$", ref)}
            missing = expected - indexed.keys()
            if missing:
                raise ValueError("Questions missing from official answer inventory: " + ", ".join(sorted(missing)))
        for q_id, item in enumerate(ordered, 1):
            item["q_id"] = q_id
        for figure in collected["figures"]:
            figure["source_refs"] = [source_ref(ref) for ref in figure["source_refs"]]
            if not 1 <= figure["page"] <= page_count:
                raise ValueError("Figure page outside PDF")
            if figure not in figures:
                figures.append(figure)
        value = {"questions": ordered, "figures": figures, "official_answers": official}
        atomic_json(checkpoint, value)
        return value

    def extract(self, pdf, directory, index):
        self.progress("extract", total=len(index["questions"]), completed=0)
        inventory = index["questions"]
        groups = []
        for entry in inventory:
            if not entry.get("series_key") or not groups or groups[-1][0].get("series_key") != entry["series_key"]:
                groups.append([])
            groups[-1].append(entry)
        by_id = {entry["q_id"]: entry for entry in inventory}
        unit_by_id = {entry["q_id"]: group for group in groups for entry in group}

        def request_pages(entries):
            refs = {entry["source_ref"] for entry in entries}
            pages = {figure["page"] for figure in index["figures"] if refs & set(figure["source_refs"])}
            for entry in entries:
                end = entry["end_page"]
                following = by_id.get(entry["q_id"] + 1)
                if following and following["page"] > entry["page"]:
                    end = max(end, following["page"])
                pages.update(range(max(1, entry["page"] - 1), end + 1))
            return pages

        batches = []
        for group in groups:
            candidate = batches[-1] + group if batches else group
            if batches and len(candidate) <= 6 and len(request_pages(candidate)) <= 8:
                batches[-1].extend(group)
            else:
                batches.append(list(group))
        questions = []
        for entries in batches:
            refs = {entry["source_ref"] for entry in entries}
            figures = [f for f in index["figures"] if refs & set(f["source_refs"])]
            pages = request_pages(entries)
            parts = pdf_chunks(pdf, sorted(pages), directory / "analysis" / f"extract_{entries[0]['q_id']}",
                               max_bytes=self.settings.chunk_max_bytes)
            paths = [p.path for p in parts]
            if sum(p.stat().st_size for p in paths) > self.settings.chunk_max_bytes:
                paths = self.raster_bundle(pdf, sorted(pages), directory / "analysis" / f"raster_{entries[0]['q_id']}")
                mappings = [[page] for page in sorted(pages)]
            else:
                mappings = [p.pages for p in parts]
            context = {"questions": entries, "parts": mappings, "figures": figures,
                       "official_answers": [index["official_answers"][ref] for ref in sorted(refs, key=natural_key)
                                            if ref in index["official_answers"]]}
            text = prompt("extract") + (
                "\nUse EXACT q_id from context.questions. Extract ONLY those source_ref values. "
                "PDF parts have local pages: map output pages via context.parts. Include shared case text. "
                "Use context.figures for associated figure booklet images. Do not extract neighbouring questions."
            )
            def validate_extraction(value):
                if {q["q_id"] for q in value["questions"]} != {entry["q_id"] for entry in entries}:
                    raise ValueError("Extraction omitted or added indexed questions")
                expected_pages = {e["q_id"]: e["page"] for e in entries}
                for question in value["questions"]:
                    if question["page"] != expected_pages[question["q_id"]]:
                        raise ValueError("Extracted question page differs from index")

            batch = self.call("extract", text, QuestionBatch, paths, context,
                              directory / "checkpoints" / f"extract_{entries[0]['q_id']}.json", validate_extraction)
            extracted_by_id = {q["q_id"]: q for q in batch["questions"]}
            for entry in entries:
                question = extracted_by_id[entry["q_id"]]
                if question["page"] != entry["page"]:
                    raise ValueError("Extracted question page differs from index")
                expected = index["official_answers"].get(entry["source_ref"])
                if expected:
                    if question.get("options") and expected.get("answer"):
                        question["official_answer"] = expected["answer"]
                        question["official_answer_text"] = ""
                    elif expected.get("answer_text"):
                        question["official_answer_text"] = expected["answer_text"]
                unit = unit_by_id[entry["q_id"]]
                if len(unit) > 1:
                    position = next(i for i, member in enumerate(unit, 1) if member["q_id"] == entry["q_id"])
                    question["series_index"] = position
                    question["series_total"] = len(unit)
                    question.setdefault("depends_on", [] if position == 1 else [unit[position - 2]["q_id"]])
                for figure in figures:
                    if entry["source_ref"] in figure["source_refs"]:
                        image = {k: figure[k] for k in ("label", "page", "box_2d")}
                        images = question.setdefault("images", [])
                        if not any(i["page"] == image["page"] and i["label"] == image["label"] for i in images):
                            images.append(image)
                questions.append(question)
            self.progress("extract", total=len(inventory), completed=len(questions))
        questions.sort(key=lambda q: q["q_id"])
        QuestionBatch.model_validate({"questions": questions})
        return questions

    def raster_bundle(self, pdf, pages, directory):
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for number in pages:
            image = render_page(pdf, number, dpi=200)
            png = directory / f"page_{number}.png"
            image.save(png)
            image.close()
            path = png.with_suffix(".pdf")
            path.write_bytes(img2pdf.convert(str(png)))
            paths.append(path)
        if sum(p.stat().st_size for p in paths) > self.settings.chunk_max_bytes:
            raise ValueError("Question/series analysis images exceed 40 MB; cannot truncate source material")
        return paths

    def explain(self, questions, directory, retry):
        answer_dir = directory / "explanations"
        answer_dir.mkdir(exist_ok=True)
        groups = question_groups(questions)
        self.progress("explain", total=len(questions), completed=0)

        def process(group):
            checkpoint = answer_dir / f"q{group[0]['q_id']}.json"
            if checkpoint.is_file():
                try:
                    saved = read_json(checkpoint)
                    if not retry or not saved["errors"]:
                        if not saved["errors"]:
                            if set(saved["answers"]) != {str(q["q_id"]) for q in group}:
                                raise ValueError("Invalid explanation checkpoint IDs")
                            for q in group:
                                validate_answer(Question.model_validate(q), Answer.model_validate(saved["answers"][str(q["q_id"])]))
                        return saved
                except (ValueError, KeyError, OSError, TypeError):
                    checkpoint.unlink(missing_ok=True)
            retries = {}
            error = "解答解説を生成できませんでした"
            for attempt in range(4):
                try:
                    images = []
                    for q in group:
                        for image in q.get("images", []):
                            if not image.get("file") or not (directory / image["file"]).is_file():
                                raise ValueError("Required image crop missing")
                            images.append(directory / image["file"])
                    text = prompt("explain")
                    if attempt:
                        text += "\nCorrect the previous validation error: " + error
                    value = self.provider.generate("explain", text, AnswerBatch, images, {"questions": group})
                    batch = AnswerBatch.model_validate(value)
                    if [a.q_id for a in batch.answers] != [q["q_id"] for q in group]:
                        raise ValueError("Answers omitted, added or reordered question IDs")
                    for q, answer in zip(group, batch.answers, strict=True):
                        validate_answer(Question.model_validate(q), answer)
                    result = {"answers": {str(a.q_id): a.clean() for a in batch.answers}, "errors": {},
                              "retries": {str(q["q_id"]): attempt for q in group if attempt}}
                    atomic_json(checkpoint, result)
                    return result
                except Exception as exc:
                    error = safe_error(exc)
                    retries = {str(q["q_id"]): attempt for q in group if attempt}
                    if attempt < 3:
                        wait_for_retry(exc, attempt)
            result = {"answers": {}, "errors": {str(q["q_id"]): error for q in group}, "retries": retries}
            atomic_json(checkpoint, result)
            return result

        answers, errors, retries = {}, {}, {}
        completed = 0
        with ThreadPoolExecutor(max_workers=self.settings.gemini_concurrency) as executor:
            pending = {executor.submit(process, group): len(group) for group in groups}
            for future in as_completed(pending):
                saved = future.result()
                answers.update({int(k): v for k, v in saved["answers"].items()})
                errors.update({int(k): v for k, v in saved["errors"].items()})
                retries.update(saved["retries"])
                completed += pending[future]
                self.progress("explain", total=len(questions), completed=completed)
        atomic_json(directory / "answers.json", {"answers": list(answers.values()), "errors": errors,
                                                 "retries": retries})
        return answers, errors, retries
