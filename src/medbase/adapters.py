import copy


def frontend_document(result: dict, set_id: str, api_base: str = "/api/v1") -> dict:
    questions = []
    for question in result["questions"]:
        if question["status"] != "ok":
            continue
        row = copy.deepcopy(question)
        row["source_file"] = set_id
        row["options"] = row.get("options", {})
        row["answer"] = row.get("answer", row.get("answer_text", ""))
        for index, image in enumerate(row.get("images", [])):
            image["src"] = f"{api_base}/sets/{set_id}/images/{row['q_id']}/{index}"
        questions.append(row)
    return {"source_file": set_id, "exam_name": result["exam_name"], "author": result["author"],
            "q_and_a": questions, "pdf_url": f"{api_base}/sets/{set_id}/pdf"}
