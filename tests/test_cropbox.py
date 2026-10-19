from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject

from medbase.pdfs import footer_only, merge_pdfs, render_page


def test_japanese_cid_font_without_embedding_is_rendered(tmp_path):
    from PIL import Image, ImageChops
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    target = tmp_path / "japanese.pdf"
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
    document = canvas.Canvas(str(target), pagesize=(600, 800))
    document.setFont("HeiseiMin-W3", 24)
    document.drawString(60, 700, "日本語の問題文と選択肢")
    document.save()
    image = render_page(target, 1, dpi=72)
    try:
        text_area = image.crop((50, 65, 550, 115))
        difference = ImageChops.difference(text_area, Image.new("RGB", text_area.size, "white")).convert("L")
        assert sum(difference.histogram()[31:]) > 300, "Japanese CID text disappeared during PDF rendering"
    finally:
        image.close()


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
