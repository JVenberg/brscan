"""Classify a scanned document with Claude and propose how to file it.

Sends the scanned page image(s) to the Anthropic Messages API and gets back a
validated structured result (document type, destination folder, vendor, date,
amount, filename, summary). Uses the official `anthropic` SDK (the `ai` extra).
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high")
MAX_EDGE = 1568  # downscale long edge before upload to keep image tokens modest
MAX_PAGES = 2
# Thinking is always on and counts toward max_tokens.
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClassifyError(Exception):
    """Claude returned no usable filing (refusal, truncation, or bad output)."""


class Filing(BaseModel):
    document_type: str = Field(
        description="What kind of document this is in a few words, e.g. "
        "'grocery receipt', 'promotional mailer', 'medical bill', "
        "'bank statement', 'utility bill', 'tax form', 'warranty'."
    )
    folder: str = Field(
        description="Destination folder name in PascalCase (e.g. 'Receipts', "
        "'Offers', 'TaxForms', 'MedicalBills'). Reuse one of the existing "
        "folders when it clearly fits; only propose a new, concise folder name "
        "when none do."
    )
    vendor: str = Field(
        description="Business or organization the document is from, e.g. "
        "'Safeway', 'HelloFresh'. Empty string if unknown."
    )
    date: str = Field(
        description="The document's own date as YYYY-MM-DD if visible on the "
        "page, otherwise an empty string. Do not invent a date."
    )
    amount: str = Field(
        description="Total amount for receipts/bills as a bare number like "
        "'71.29' (no currency symbol). Empty string if not applicable."
    )
    filename: str = Field(
        description="A BRIEF base filename: at most ~4 words and ~40 characters, "
        "WITHOUT extension, date, or amount. Prefer vendor plus a short document "
        "type, e.g. 'Safeway receipt', 'HelloFresh offer', 'Invisalign agreement'. "
        "Keep it short and scannable; put the detail in title/summary instead."
    )
    title: str = Field(
        description="A clear, descriptive one-line document title for the PDF's "
        "metadata (more detail than the filename), e.g. 'Smiles on Madison - "
        "Invisalign payment agreement and proposed treatment plan'."
    )
    summary: str = Field(
        description="A detailed 1-3 sentence description of the document for the "
        "PDF metadata: capture the key specifics (parties, amounts, dates, terms, "
        "what it is and what it's for)."
    )


def _encode_image(path: Path, max_edge: int = MAX_EDGE):
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_edge / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


class Document(BaseModel):
    first_page: int = Field(
        description="Number of the page (as labeled) where this document starts."
    )
    filing: Filing


class Batch(BaseModel):
    documents: List[Document] = Field(
        description="Every document in the batch, in page order. The first starts at page 1."
    )


FILING_RULES = (
    "Strongly prefer an existing folder when one fits; only propose a new "
    "folder when none do. Folder names are PascalCase (e.g. Receipts, "
    "Offers, TaxForms). Keep the filename brief; put the descriptive detail "
    "in the title and summary (they become the PDF's metadata). Pages "
    "may be rotated or upside down."
)


def _image_block(path: Path, max_edge: int = MAX_EDGE) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": _encode_image(path, max_edge)},
    }


def _ask(content: list, schema, model: str, api_key: Optional[str], effort: str):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    # create + validate (not parse) so stop_reason is checked before the JSON is parsed.
    resp = client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": content}],
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
        },
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise ClassifyError("Claude declined to classify this document")
    if resp.stop_reason == "max_tokens":
        raise ClassifyError("response was cut off (max_tokens)")
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise ClassifyError(f"unexpected output: {exc.errors()[0]['msg']}") from exc


def _folders_line(known_folders: List[str]) -> str:
    return ", ".join(known_folders) if known_folders else "(none yet)"


def classify(pages: List[Path], known_folders: List[str],
             model: str = DEFAULT_MODEL,
             api_key: Optional[str] = None,
             effort: str = DEFAULT_EFFORT) -> Filing:
    content: list = [{
        "type": "text",
        "text": (
            "You are filing a scanned paper document into a folder on disk.\n"
            f"Existing folders: {_folders_line(known_folders)}.\n"
            "Look at the scanned page(s) and decide where the document belongs. "
            f"{FILING_RULES} Return the structured filing details."
        ),
    }]
    content += [_image_block(p) for p in pages[:MAX_PAGES]]
    return _ask(content, Filing, model, api_key, effort)


SPLIT_CHUNK = 40  # pages per request; keeps image count and request size well inside API limits


def _split_edge(n: int) -> int:
    return MAX_EDGE if n <= 10 else 1280 if n <= 20 else 1024


def split(pages: List[Path], known_folders: List[str],
          model: str = DEFAULT_MODEL,
          api_key: Optional[str] = None,
          effort: str = DEFAULT_EFFORT,
          labels: Optional[List[str]] = None) -> List[Tuple[int, Filing]]:
    """Split a batch of pages into documents and file each one.

    Returns (first page index, filing) pairs, 0-based, in page order. labels
    name each page for Claude (e.g. "sheet 2, back"); a new chunk always starts
    a new document.
    """
    out: List[Tuple[int, Filing]] = []
    for base in range(0, len(pages), SPLIT_CHUNK):
        chunk = pages[base:base + SPLIT_CHUNK]
        edge = _split_edge(len(chunk))
        content: list = [{
            "type": "text",
            "text": (
                "You are filing a batch of scanned pages into folders.\n"
                f"Existing folders: {_folders_line(known_folders)}.\n"
                "The pages were fed through a sheet-fed scanner one after another and "
                "may hold several separate documents, for example a receipt, then a "
                "two-page letter, then a bill. Split the batch into documents, each a "
                "run of consecutive pages, and file each one. Pages of one document "
                "share a sender, layout, or page numbering (\"Page 2 of 3\"), or "
                "continue the previous page. Both sides of a sheet belong to the same "
                f"document. {FILING_RULES}"
            ),
        }]
        for i, p in enumerate(chunk, start=1):
            label = labels[base + i - 1] if labels else ""
            content.append({"type": "text", "text": f"Page {i}" + (f" ({label})" if label else "")})
            content.append(_image_block(p, edge))
        batch = _ask(content, Batch, model, api_key, effort)
        if not batch.documents:
            raise ClassifyError("Claude returned no documents")
        first, *rest = sorted(batch.documents, key=lambda d: d.first_page)
        out.append((base, first.filing))
        out += [(base + d.first_page - 1, d.filing) for d in rest
                if 1 < d.first_page <= len(chunk)]
    return out
