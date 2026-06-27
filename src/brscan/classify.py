"""Classify a scanned document with Claude and propose how to file it.

Sends the scanned page image(s) to the Anthropic Messages API and gets back a
validated structured result (document type, destination folder, vendor, date,
amount, filename, summary). Uses the official `anthropic` SDK (the `ai` extra).
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

DEFAULT_MODEL = "claude-opus-4-8"
MAX_EDGE = 1568  # downscale long edge before upload to keep image tokens modest
MAX_PAGES = 2


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
    summary: str = Field(description="A one-line human summary of the document.")
    filename: str = Field(
        description="Concise, descriptive base filename WITHOUT extension and "
        "WITHOUT a date or amount (those are added separately), e.g. "
        "'Safeway grocery receipt' or 'HelloFresh 11 free meals (code EB-Z3TRJ)'."
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


def classify(pages: List[Path], known_folders: List[str],
             model: str = DEFAULT_MODEL,
             api_key: Optional[str] = None) -> Optional[Filing]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    folders_line = ", ".join(known_folders) if known_folders else "(none yet)"
    content: list = [{
        "type": "text",
        "text": (
            "You are filing a scanned paper document into a folder on disk.\n"
            f"Existing folders: {folders_line}.\n"
            "Look at the scanned page(s) and decide where the document belongs. "
            "Strongly prefer an existing folder when one fits; only propose a new "
            "folder when none do. Folder names are PascalCase (e.g. Receipts, "
            "Offers, TaxForms). The page may be rotated or upside down. Return the "
            "structured filing details."
        ),
    }]
    for p in pages[:MAX_PAGES]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _encode_image(p),
            },
        })

    resp = client.messages.parse(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
        output_format=Filing,
    )
    return resp.parsed_output
