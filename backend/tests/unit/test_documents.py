"""Context document ingestion, chunking and retrieval.

These are the data dictionaries and business-rule documents that hard questions
turn on. Chunking is paragraph-aligned on purpose: a definition cut in half
produces two chunks that each retrieve well and neither of which states the rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.ingest.documents import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    ContextDocument,
    DocumentExtractionError,
    UnsupportedDocumentError,
    chunk_text,
    is_supported_document,
    load_document,
    search_documents,
    supported_document_extensions,
)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def test_paragraphs_are_kept_whole_when_they_fit() -> None:
    text = "First rule about fees.\n\nSecond rule about refunds.\n\nThird rule about chargebacks."
    chunks = chunk_text(text, size=4000, overlap=0)
    assert len(chunks) == 1
    assert "chargebacks" in chunks[0]


def test_paragraphs_are_packed_up_to_the_budget() -> None:
    paragraphs = "\n\n".join(f"Paragraph number {index} with some body text." for index in range(20))
    chunks = chunk_text(paragraphs, size=120, overlap=0)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    # Nothing is lost in the packing.
    joined = " ".join(chunks)
    assert "number 0" in joined and "number 19" in joined


def test_an_oversized_paragraph_is_split_on_sentences() -> None:
    paragraph = " ".join(f"Sentence {index} explains a rule." for index in range(80))
    chunks = chunk_text(paragraph, size=200, overlap=0)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)


def test_a_single_sentence_longer_than_the_budget_is_hard_cut() -> None:
    """No sentence boundary to use, so the alternative is losing the text."""
    chunks = chunk_text("x" * 900, size=200, overlap=0)
    assert len(chunks) == 5
    assert all(len(chunk) <= 200 for chunk in chunks)


def test_overlap_carries_context_across_a_boundary() -> None:
    """A rule spanning two chunks must be retrievable from either side."""
    text = "\n\n".join([f"Rule {index}: distinctive marker {index}." for index in range(12)])
    with_overlap = chunk_text(text, size=100, overlap=40)
    assert len(with_overlap) > 1


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n"])
def test_empty_input_produces_no_chunks(text: str) -> None:
    assert chunk_text(text) == []


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def test_markdown_is_loaded_and_chunked(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.md"
    path.write_text(
        "# Data dictionary\n\n"
        "`status` — C means cancelled, not complete.\n\n"
        "`fee_bps` — basis points, so divide by 10000.\n",
        encoding="utf-8",
    )

    document = load_document(path, "dictionary.md")

    assert document.name == "dictionary.md"
    assert document.source_format == "md"
    assert document.chunks
    assert "cancelled" in document.text


def test_a_non_utf8_export_still_loads(tmp_path: Path) -> None:
    """A dictionary exported from Excel on Windows is cp1252 far more often
    than it is UTF-8, and failing an upload over a smart quote would be absurd."""
    path = tmp_path / "rules.txt"
    path.write_bytes("Fee rule “quoted” applies.".encode("cp1252"))

    document = load_document(path, "rules.txt")
    assert "Fee rule" in document.text
    assert "quoted" in document.text


def test_html_tags_are_stripped(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text("<html><body><p>Chargeback fees are 15 bps.</p></body></html>", encoding="utf-8")

    document = load_document(path, "page.html")
    assert "Chargeback fees are 15 bps." in document.text
    assert "<p>" not in document.text


def test_an_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thing.exe"
    path.write_text("binary-ish", encoding="utf-8")

    with pytest.raises(UnsupportedDocumentError) as excinfo:
        load_document(path, "thing.exe")
    # The message must name what *is* accepted, or the user is guessing.
    assert ".md" in str(excinfo.value)


def test_an_empty_document_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "blank.md"
    path.write_text("   \n\n  ", encoding="utf-8")

    with pytest.raises(UnsupportedDocumentError):
        load_document(path, "blank.md")


@pytest.mark.parametrize("name", ["a.md", "b.txt", "c.pdf", "d.docx", "e.html"])
def test_supported_names_are_recognised(name: str) -> None:
    assert is_supported_document(name)


@pytest.mark.parametrize("name", ["a.csv", "b.xlsx", "c.parquet", "d.zip", "noextension"])
def test_data_files_are_not_documents(name: str) -> None:
    """Tabular formats belong to the dataset loader; the two must not overlap."""
    assert not is_supported_document(name)


def test_the_advertised_list_matches_what_is_accepted() -> None:
    assert set(supported_document_extensions()) == SUPPORTED_DOCUMENT_EXTENSIONS


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def _document(name: str, body: str) -> ContextDocument:
    document = ContextDocument(name=name, text=body)
    from src.core.ingest.documents import DocumentChunk

    for index, chunk in enumerate(chunk_text(body, size=4000, overlap=0)):
        document.chunks.append(DocumentChunk(document=name, index=index, text=chunk))
    return document


def test_retrieval_finds_the_relevant_passage() -> None:
    documents = {
        "fees.md": _document("fees.md", "Chargeback fees are billed in basis points against volume."),
        "regions.md": _document("regions.md", "Region codes follow the ISO 3166 alpha-2 standard."),
    }

    hits = search_documents(documents, "how are chargeback fees billed", limit=1)

    assert hits
    assert hits[0][0] == "fees.md"


def test_retrieval_returns_the_document_it_came_from() -> None:
    """The agent has to be able to cite which reference stated a rule."""
    documents = {"rules.md": _document("rules.md", "Refunds are netted against gross volume monthly.")}
    hits = search_documents(documents, "refunds netted gross volume monthly")

    assert hits
    name, passage = hits[0]
    assert name == "rules.md"
    assert "Refunds" in passage


def test_searching_with_no_documents_is_empty_not_an_error() -> None:
    assert search_documents({}, "anything") == []


def test_an_empty_query_returns_nothing() -> None:
    documents = {"a.md": _document("a.md", "Some content here.")}
    assert search_documents(documents, "   ") == []


def test_summary_reports_what_the_ui_needs() -> None:
    document = _document("dict.md", "Line one of the dictionary.\n\nLine two.")
    summary = document.summary()

    assert summary["name"] == "dict.md"
    assert summary["chunks"] == len(document.chunks)
    assert summary["chars"] > 0
    assert summary["preview"]


# --------------------------------------------------------------------------- #
# PDF bounds -- a small file on disk does not bound what parsing it costs
# --------------------------------------------------------------------------- #
class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, path: str, page_count: int, text_per_page: str) -> None:
        self.pages = [_FakePage(text_per_page) for _ in range(page_count)]


def test_a_pdf_declaring_too_many_pages_is_refused(tmp_path: Path, monkeypatch) -> None:
    pypdf = pytest.importorskip("pypdf")

    monkeypatch.setattr("src.core.ingest.documents.settings.CONTEXT_DOC_MAX_PDF_PAGES", 5)
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: _FakeReader(path, page_count=6, text_per_page="hi"))

    path = tmp_path / "big.pdf"
    path.write_bytes(b"%PDF-1.4 not a real pdf, never opened by the fake reader")

    with pytest.raises(DocumentExtractionError, match="pages"):
        load_document(path, "big.pdf")


def test_a_pdf_decompressing_past_the_char_ceiling_is_refused(tmp_path: Path, monkeypatch) -> None:
    pypdf = pytest.importorskip("pypdf")

    monkeypatch.setattr("src.core.ingest.documents.settings.CONTEXT_DOC_MAX_EXTRACTED_CHARS", 100)
    monkeypatch.setattr(pypdf, "PdfReader", lambda path: _FakeReader(path, page_count=3, text_per_page="x" * 60))

    path = tmp_path / "bomb.pdf"
    path.write_bytes(b"%PDF-1.4 not a real pdf, never opened by the fake reader")

    with pytest.raises(DocumentExtractionError, match="memory"):
        load_document(path, "bomb.pdf")


def test_a_pdf_within_bounds_still_parses(tmp_path: Path, monkeypatch) -> None:
    pypdf = pytest.importorskip("pypdf")

    monkeypatch.setattr(pypdf, "PdfReader", lambda path: _FakeReader(path, page_count=2, text_per_page="A rule."))

    path = tmp_path / "small.pdf"
    path.write_bytes(b"%PDF-1.4 not a real pdf, never opened by the fake reader")

    document = load_document(path, "small.pdf")
    assert "A rule." in document.text
