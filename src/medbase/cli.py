import argparse
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

from pypdf import PdfReader

from medbase.config import get_settings
from medbase.fixtures import create_fixture, prepare_mock
from medbase.pipeline import Pipeline
from medbase.storage import read_json, safe_error


def check_sample(result_path: Path, official_pdf: Path):
    result = read_json(result_path)
    questions = result["questions"]
    if len(questions) != 75 or [q["q_id"] for q in questions] != list(range(1, 76)):
        raise ValueError("A区分75問が揃っていません")
    text = "\n".join(page.extract_text() or "" for page in PdfReader(official_pdf).pages)
    official = {int(number): value for number, value in re.findall(r"\bA(\d{3})\s+([A-L]+|\d+(?:\.\d+)?)\b", text)}
    if len(official) != 75:
        raise ValueError("正答表のA区分75問を読み取れませんでした")
    for question in questions:
        q_id = question["q_id"]
        if question["status"] != "ok":
            raise ValueError(f"q_id={q_id}: 処理エラーがあります")
        expected = official[q_id]
        if expected.isalpha():
            if set(question.get("official_answer", [])) != set(expected.lower()):
                raise ValueError(f"q_id={q_id}: 公式正答が一致しません")
            if set(question.get("answer", [])) != set(expected.lower()):
                raise ValueError(f"q_id={q_id}: 解答が一致しません")
        elif question.get("answer_text") != expected or question.get("official_answer_text") != expected:
            raise ValueError(f"q_id={q_id}: 数値正答が一致しません")
        for image in question.get("images", []):
            if not image.get("file") or not (result_path.parent / image["file"]).is_file():
                raise ValueError(f"q_id={q_id}: 画像ファイルがありません")
    if not questions[16].get("images") or questions[74].get("answer_text") != "600":
        raise ValueError("A017の別冊画像またはA075の数値解答を確認してください")
    integration_log = result_path.parent / "pdf_merge_log.json"
    if integration_log.is_file():
        figure_page = next((p["merged_page"] for p in read_json(integration_log)["pages"]
                            if p["source_file"] == "tp240424-01a_02.pdf" and p["source_page"] == 6), None)
        if figure_page is None or not any(i["page"] == figure_page for i in questions[16]["images"]):
            raise ValueError("A017の図版が別冊No.2のページに対応していません")
    return {"questions": 75, "official_answers_matched": 75, "A075": "600", "status": "passed"}


def main():
    parser = argparse.ArgumentParser(prog="medbase")
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("make-fixture", help="Create a synthetic PDF for mock tests")
    fixture.add_argument("directory", type=Path)
    run = commands.add_parser("run", help="Run the same processing pipeline without the web server")
    run.add_argument("files", type=Path, nargs="+")
    run.add_argument("--author")
    run.add_argument("--output", type=Path)
    check = commands.add_parser("check-sample", help="Verify the user-supplied 118th exam A acceptance criteria")
    check.add_argument("--result", type=Path, required=True)
    check.add_argument("--official", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "make-fixture":
            print(create_fixture(args.directory).resolve())
        elif args.command == "check-sample":
            print(json.dumps(check_sample(args.result, args.official), ensure_ascii=False))
        else:
            settings = get_settings()
            author = (args.author if args.author is not None else settings.author_name).strip()
            if not author:
                raise ValueError("作者名を入力してください")
            root = args.output or settings.storage_root / "cli" / str(uuid.uuid4())
            if (root / "classification.json").exists():
                raise ValueError("Output already contains a run; use the job retry API or choose a new directory")
            incoming = root / "inputs"
            incoming.mkdir(parents=True, exist_ok=True)
            inputs = []
            for path in args.files:
                document_id = str(uuid.uuid4())
                target = incoming / (document_id + path.suffix.lower())
                shutil.copyfile(path, target)
                inputs.append({"document_id": document_id, "name": path.name,
                               "path": target.relative_to(root).as_posix()})
            if settings.llm_mode == "mock":
                prepare_mock(root, inputs)
            results = Pipeline(settings, root.resolve(), progress=lambda stage, **v: print(stage, v)).run(inputs, author)
            print(json.dumps(results, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
