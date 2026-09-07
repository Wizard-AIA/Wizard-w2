"""Reference documents attached to a session.

A data dictionary, a fee schedule, a metric definition, the page explaining that
``status = 'C'`` means cancelled and not complete. Hard analytical questions turn
on these far more often than on anything discoverable from the tables, and until
now there was nowhere to put one: ingestion accepted tabular files only.

`DABstep <https://arxiv.org/abs/2506.23719>`_ is built around exactly this —
its hard tasks require cross-referencing structured data against unstructured
documentation, and one of its named failure modes is agents failing "to consult
required documentation at the right point".

Design
------
Deliberately small. Documents are chunked on paragraph boundaries, embedded once
at upload, and retrieved by the same :mod:`~src.core.embeddings` service the rest
of the app uses -- which degrades to lexical overlap when no transformer is
loaded, so this works air-gapped and in CI.

PDF and DOCX parsing is *optional*. The extractors are imported inside the
functions that need them, so a deployment that never uploads a PDF does not pay
for the dependency and an install without it still starts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.config import settings
from src.core.embeddings import embedding_service
from src.core.rag.hybrid_search import reciprocal_rank_fusion
from src.core.rag.reranker import get_reranker
from src.core.rag.retriever import lexical_overlap, tokenize
from src.utils.logging import logger


TEXT_LIKE = {".md", ".markdown", ".txt", ".rst", ".text", ".log"}
PDF_LIKE = {".pdf"}
DOCX_LIKE = {".docx"}
HTML_LIKE = {".html", ".htm"}

SUPPORTED_DOCUMENT_EXTENSIONS = TEXT_LIKE | PDF_LIKE | DOCX_LIKE | HTML_LIKE

#: Paragraph boundary: one or more blank lines, or a markdown heading.
PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
HTML_TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"[ \t]+")


class UnsupportedDocumentError(ValueError):
    """Raised for a document extension that cannot be parsed."""


class DocumentExtractionError(RuntimeError):
    """Raised when a supported format is present but its parser is not installed."""


@dataclass(frozen=True)
class Citation:
    """A stable, session-scoped locator for a retrieved source passage."""

    document: str
    content_hash: str
    chunk_index: int
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None

    def label(self) -> str:
        page = ""
        if self.page_start is not None:
            page = f", p. {self.page_start}" if self.page_start == self.page_end else f", pp. {self.page_start}-{self.page_end}"
        return f"{self.document}{page} (chunk {self.chunk_index + 1})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "content_hash": self.content_hash,
            "chunk_index": self.chunk_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass
class DocumentChunk:
    """One retrievable passage."""

    document: str
    index: int
    text: str
    embedding: list[float] | None = None
    char_start: int = 0
    char_end: int = 0
    page_start: int | None = None
    page_end: int | None = None

    def __post_init__(self) -> None:
        """Give manually-created chunks a useful source span as well.

        A few integrations construct chunks directly instead of using
        :func:`load_document`.  Treating an omitted end offset as the extent of
        that passage preserves a valid citation for those callers without
        changing their constructor contract.
        """
        if self.char_end == 0 and self.text:
            self.char_end = self.char_start + len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "index": self.index,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass
class ContextDocument:
    """A reference document belonging to one session."""

    name: str
    text: str
    chunks: list[DocumentChunk] = field(default_factory=list)
    source_format: str = "txt"
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def char_count(self) -> int:
        return len(self.text)

    def summary(self) -> dict[str, Any]:
        head = self.text.strip().splitlines()
        return {
            "name": self.name,
            "content_hash": self.content_hash,
            "chars": self.char_count,
            "chunks": len(self.chunks),
            "source_format": self.source_format,
            "preview": (head[0][:200] if head else ""),
        }


# ---------------------------------------------------------------------- #
# Extraction
# ---------------------------------------------------------------------- #
def _extract_text(path: Path, suffix: str) -> str:
    """Compatibility wrapper for callers that need plain extracted text."""
    return _extract_document(path, suffix)[0]


def _extract_document(path: Path, suffix: str) -> tuple[str, list[tuple[int, int, int]]]:
    """Return text plus ``(page, start, end)`` ranges in that extracted text."""
    if suffix in TEXT_LIKE:
        return _read_text(path), []

    if suffix in HTML_LIKE:
        return HTML_TAG.sub(" ", _read_text(path)), []

    if suffix in PDF_LIKE:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise DocumentExtractionError(
                "Reading PDFs needs the `pypdf` package. Install it, or upload the document as Markdown or text."
            ) from exc
        reader = PdfReader(str(path))

        # A small file on disk does not bound what comes out of it: a PDF can
        # declare an arbitrary page count, and a page's content stream can
        # decompress to far more text than its own size suggests. Both are
        # cheap for an attacker and expensive for this process, so both are
        # capped before extraction is allowed to run unbounded.
        page_count = len(reader.pages)
        if page_count > settings.CONTEXT_DOC_MAX_PDF_PAGES:
            raise DocumentExtractionError(
                f"'{path.name}' has {page_count} pages, more than the "
                f"{settings.CONTEXT_DOC_MAX_PDF_PAGES} this deployment will parse."
            )

        parts: list[str] = []
        page_ranges: list[tuple[int, int, int]] = []
        total_chars = 0
        offset = 0
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            total_chars += len(text)
            if total_chars > settings.CONTEXT_DOC_MAX_EXTRACTED_CHARS:
                raise DocumentExtractionError(
                    f"'{path.name}' decompresses to more text than this deployment will hold in memory "
                    f"(over {settings.CONTEXT_DOC_MAX_EXTRACTED_CHARS:,} characters)."
                )
            parts.append(text)
            page_ranges.append((page_number, offset, offset + len(text)))
            offset += len(text) + 2
        return "\n\n".join(parts), page_ranges

    if suffix in DOCX_LIKE:
        try:
            import docx
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise DocumentExtractionError(
                "Reading .docx needs the `python-docx` package. Install it, or upload the document as Markdown or text."
            ) from exc
        document = docx.Document(str(path))
        return "\n\n".join(paragraph.text for paragraph in document.paragraphs), []

    raise UnsupportedDocumentError(f"Unsupported document type '{suffix}'.")


def _read_text(path: Path) -> str:
    """Decodes a text file, tolerating whatever encoding it arrived in.

    A data dictionary exported from Excel on Windows is cp1252 far more often
    than it is UTF-8, and failing the upload over a smart quote would be absurd.
    """
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------- #
# Chunking
# ---------------------------------------------------------------------- #
def _normalise_text(text: str) -> str:
    return WHITESPACE.sub(" ", (text or "").replace("\r\n", "\n")).strip()


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Splits on paragraph boundaries, packing up to ``size`` characters.

    Paragraph-aligned rather than fixed-width because these documents are
    definitions and rules: cutting one in half produces two chunks that each
    retrieve well and neither of which states the rule.
    """
    limit = size or settings.CONTEXT_CHUNK_CHARS
    lap = overlap if overlap is not None else settings.CONTEXT_CHUNK_OVERLAP

    normalised = _normalise_text(text)
    if not normalised:
        return []

    paragraphs = [p.strip() for p in PARAGRAPH_BREAK.split(normalised) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        # A paragraph longer than the budget is split on its own, by sentence.
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(paragraph, limit))
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            # Carry the tail of the previous chunk so a rule spanning the
            # boundary is still retrievable from either side.
            current = (current[-lap:] + "\n\n" + paragraph) if lap else paragraph
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            # A single sentence over the budget: hard-cut it, nothing else to do.
            for start in range(0, len(sentence), limit):
                chunks.append(sentence[start : start + limit])
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------- #
# Loading
# ---------------------------------------------------------------------- #
def supported_document_extensions() -> list[str]:
    return sorted(SUPPORTED_DOCUMENT_EXTENSIONS)


def is_supported_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def load_document(path: Path, name: str) -> ContextDocument:
    """Parses, chunks and embeds one reference document."""
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise UnsupportedDocumentError(
            f"Unsupported document type '{suffix or name}'. Supported: {', '.join(supported_document_extensions())}"
        )

    text, page_ranges = _extract_document(path, suffix)
    if not text.strip():
        raise UnsupportedDocumentError(f"'{name}' parsed successfully but contains no readable text.")

    normalised = _normalise_text(text)
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    document = ContextDocument(name=name, text=normalised, source_format=suffix.lstrip("."), content_hash=content_hash)
    chunks_text = list(chunk_text(normalised))
    embeddings: list[Any] = [None] * len(chunks_text)
    try:
        embeddings = list(embedding_service.encode_many(chunks_text))
    except Exception as exc:
        logger.debug("Batch embedding failed; falling back to per-chunk encoding", error=str(exc))
        for i, body in enumerate(chunks_text):
            try:
                embeddings[i] = embedding_service.encode(body)
            except Exception:
                pass  # lexical fallback will be used
    offset = 0
    for index, (body, emb) in enumerate(zip(chunks_text, embeddings, strict=False)):
        start = normalised.find(body, offset)
        if start < 0:
            start = offset
        end = start + len(body)
        offset = end
        pages = [page for page, page_start, page_end in page_ranges if page_start < end and page_end > start]
        document.chunks.append(
            DocumentChunk(
                document=name,
                index=index,
                text=body,
                embedding=emb,
                char_start=start,
                char_end=end,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
            )
        )

    logger.info("Context document loaded", document=name, chars=len(text), chunks=len(document.chunks))
    return document


@dataclass(frozen=True)
class DocumentHit:
    """A retrieved passage with citation and ranking evidence.

    Tuple-style access remains intentionally supported while callers migrate to
    typed citations: ``hit[0]`` is the document name and ``hit[1]`` the text.
    """

    text: str
    citation: Citation
    score: float
    methods: tuple[str, ...]

    @property
    def document(self) -> str:
        return self.citation.document

    def __iter__(self) -> Iterable[str]:
        return iter((self.document, self.text))

    def __getitem__(self, index: int) -> str:
        return (self.document, self.text)[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citation": self.citation.to_dict(),
            "score": round(self.score, 6),
            "methods": list(self.methods),
        }


def search_documents(
    documents: dict[str, ContextDocument],
    query: str,
    limit: int | None = None,
    *,
    document_names: set[str] | None = None,
    source_formats: set[str] | None = None,
    page_range: tuple[int, int] | None = None,
) -> list[DocumentHit]:
    """Rank eligible chunks with hybrid retrieval and return cited passages.

    Dense and lexical rankings are fused by reciprocal-rank fusion.  The
    optional cross-encoder operates only on this bounded first-stage set.  If
    either optional model is unavailable, the lexical path keeps retrieval
    deterministic and usable offline.
    """
    top_k = limit or settings.CONTEXT_TOP_K
    allowed_names = document_names or set(documents)
    chunks = [
        (document, chunk)
        for document in documents.values()
        if document.name in allowed_names and (source_formats is None or document.source_format in source_formats)
        for chunk in document.chunks
        if page_range is None
        or chunk.page_start is None
        or (chunk.page_start <= page_range[1] and (chunk.page_end or chunk.page_start) >= page_range[0])
    ]
    if not chunks or not query.strip():
        return []

    candidates: list[tuple[str, np.ndarray | None]] = [
        (chunk.text, np.asarray(chunk.embedding, dtype=np.float32) if chunk.embedding is not None else None)
        for _, chunk in chunks
    ]
    dense = embedding_service.rank(query, candidates) if settings.FEATURE_HYBRID_RETRIEVAL else []
    query_tokens = tokenize(query)
    lexical = sorted(
        ((index, lexical_overlap(query_tokens, chunk.text)) for index, (_, chunk) in enumerate(chunks)),
        key=lambda item: item[1],
        reverse=True,
    )
    dense_rank = [(str(index), score) for score, index in dense if score >= settings.RAG_MIN_SIMILARITY]
    lexical_rank = [(str(index), score) for index, score in lexical if score > 0]
    if not dense_rank and not lexical_rank:
        return []

    fused = reciprocal_rank_fusion(dense_rank, lexical_rank)
    method_by_index = {
        index: tuple(
            method
            for method, rank in (("dense", dense_rank), ("lexical", lexical_rank))
            if any(candidate_index == index for candidate_index, _ in rank)
        )
        for index, _ in fused
    }
    selected = fused[: max(top_k * 3, top_k)]
    reranked = False
    if settings.RAG_RERANK_ENABLED and selected:
        rerank_results = get_reranker().rerank(query, [chunks[int(index)][1].text for index, _ in selected], top_k=top_k)
        positions = [(selected[result.original_index][0], result.score) for result in rerank_results]
        reranked = True
    else:
        positions = selected[:top_k]

    results: list[DocumentHit] = []
    for index, score in positions:
        document, chunk = chunks[int(index)]
        results.append(
            DocumentHit(
                text=chunk.text,
                citation=Citation(
                    document=document.name,
                    content_hash=document.content_hash,
                    chunk_index=chunk.index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                ),
                score=score,
                methods=method_by_index[index] + (("rerank",) if reranked else ()),
            )
        )
    return results
