from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject

from medbase.pdfs import footer_only, merge_pdfs, render_page


def test_print_marks_outside_cropbox_do_not_hide_blank_pages(sample, tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    page.cropbox = RectangleObject([30, 30, 570, 770])
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"0 400 m 20 400 l S 580 400 m 600 400 l S BT /F1 8 Tf 400 45 Td (DKIX-01-AH-64) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    raw_page = writer.add_blank_page(width=600, height=800)
    raw_page[NameObject("/Resources")] = page["/Resources"]
    raw_stream = DecodedStreamObject()
    raw_stream.set_data(b"0 400 m 20 400 l S 580 400 m 600 400 l S BT /F1 8 Tf 400 100 Td (DKIX-01-AH-65) Tj ET")
    raw_page[NameObject("/Contents")] = writer._add_object(raw_stream)
    blank = tmp_path / "blank.pdf"
    with blank.open("wb") as output:
        writer.write(output)
    image = render_page(blank, 1, 60)
    assert abs(image.width - 450) <= 1
    assert footer_only(image, PdfReader(blank).pages[0].extract_text())
    image.close()
    raw_image = render_page(blank, 2, 60)
    assert footer_only(raw_image, PdfReader(blank).pages[1].extract_text())
    raw_image.close()
    docs = [{"document_id": "q", "name": "a.pdf", "pdf_path": str(sample), "role": "questions"},
            {"document_id": "blank", "name": "b.pdf", "pdf_path": str(blank), "role": "questions"}]
    log = merge_pdfs(docs, tmp_path / "merged")
    assert log["total_pages"] == 3
    assert log["pages"][-1]["reason"] == "blank page"
