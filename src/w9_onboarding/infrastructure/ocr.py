"""OCR layer: turn a PDF/PNG/JPG into positioned text lines + rendered page images.

Provider chain (`auto`):
  1. PDF with a usable text layer  -> read it directly (free, exact, confidence ~0.99)
  2. LlamaParse (LlamaCloud)       -> if LLAMA_CLOUD_API_KEY is set (managed parsing -> markdown + layout boxes)
  3. RapidOCR (ONNX, local)        -> offline fallback so the prototype runs with zero cloud setup
Every provider returns the same `OcrResult`, so extraction is provider-agnostic.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from typing import Optional

from w9_onboarding.domain.models import OcrLine, OcrResult
from w9_onboarding.utils.observability import get_logger, log_event

_log = get_logger("w9.ocr")

RENDER_DPI = 200
SUPPORTED = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 6  # a W-9 is 1 page of data (+ up to 5 instruction pages); more is suspicious


class OcrError(Exception):
    """Raised when the document cannot be read at all (corrupt, unsupported, no provider)."""


@dataclass
class LoadedDocument:
    path: Path
    data: bytes
    mime_type: str
    sha256: str
    page_images: list[Image.Image]

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf"


def load_document(path: str | Path) -> LoadedDocument:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise OcrError(f"Unsupported file type '{ext}'. Supported: {sorted(SUPPORTED)}")
    data = p.read_bytes()
    if len(data) > MAX_BYTES:
        raise OcrError(f"File too large ({len(data)} bytes > {MAX_BYTES})")
    mime = SUPPORTED[ext]
    # Trust magic bytes, not the extension.
    if data[:4] == b"%PDF":
        mime = "application/pdf"
    elif ext == ".pdf":
        raise OcrError("File has .pdf extension but is not a PDF")

    if mime == "application/pdf":
        import pypdfium2 as pdfium
        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as e:  # corrupt / encrypted
            raise OcrError(f"Cannot open PDF: {e}") from e
        if len(pdf) > MAX_PAGES:
            raise OcrError(f"PDF has {len(pdf)} pages; a W-9 should have at most {MAX_PAGES}")
        images = [pdf[i].render(scale=RENDER_DPI / 72).to_pil().convert("RGB") for i in range(len(pdf))]
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as e:
            raise OcrError(f"Cannot open image: {e}") from e
        images = [img]
    return LoadedDocument(p, data, mime, hashlib.sha256(data).hexdigest(), images)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Provider 1: PDF text layer
# ---------------------------------------------------------------------------
def pdf_text_layer(doc: LoadedDocument, min_chars: int = 200) -> OcrResult | None:
    """Digital-born PDFs carry exact text; use it. Returns None for scanned (image-only) PDFs."""
    import pdfplumber

    scale = RENDER_DPI / 72
    lines: list[OcrLine] = []
    with pdfplumber.open(io.BytesIO(doc.data)) as pdf:
        for pno, page in enumerate(pdf.pages[:1], start=1):  # data lives on page 1 of a W-9
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            words.sort(key=lambda w: (round(w["top"]), w["x0"]))
            rows: list[list[dict]] = []
            for w in words:
                if rows and abs(rows[-1][0]["top"] - w["top"]) <= 2.5:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            for row in rows:
                row.sort(key=lambda w: w["x0"])
                seg: list[dict] = [row[0]]
                for w in row[1:]:
                    if w["x0"] - seg[-1]["x1"] > 14:  # big horizontal gap => separate text block
                        lines.append(_seg_to_line(seg, scale, pno))
                        seg = [w]
                    else:
                        seg.append(w)
                lines.append(_seg_to_line(seg, scale, pno))
    if sum(len(l.text) for l in lines) < min_chars:
        return None
    return OcrResult(provider="pdf-text-layer", lines=lines, page_count=len(doc.page_images), dpi=RENDER_DPI,
                     page_images=doc.page_images, image_bytes_png=_png_bytes(doc.page_images[0]))


def _seg_to_line(seg: list[dict], scale: float, page: int) -> OcrLine:
    return OcrLine(
        text=" ".join(w["text"] for w in seg),
        bbox=(seg[0]["x0"] * scale, min(w["top"] for w in seg) * scale,
              seg[-1]["x1"] * scale, max(w["bottom"] for w in seg) * scale),
        confidence=0.99, page=page)


# ---------------------------------------------------------------------------
# Provider 2: LlamaParse (LlamaCloud managed parsing; needs LLAMA_CLOUD_API_KEY)
# ---------------------------------------------------------------------------
# List-price estimates - VERIFY against current LlamaCloud pricing before quoting them.
LLAMAPARSE_USD_PER_CREDIT = 0.00125
LLAMAPARSE_CREDITS_PER_PAGE = {"fast": 1, "cost_effective": 3, "agentic": 10, "agentic_plus": 45}

_EXPAND_ATTEMPTS = (["markdown_full", "metadata"], ["markdown_full"])   # metadata = page confidence


def _llamacloud_client():
    from llama_cloud import LlamaCloud

    key = os.environ.get("LLAMA_CLOUD_API_KEY")
    if not key:
        raise OcrError("LLAMA_CLOUD_API_KEY is not set")
    return LlamaCloud(api_key=key)


def _llamaparse_page_confidence(result) -> Optional[float]:
    md = getattr(result, "metadata", None)
    for p in getattr(md, "pages", None) or []:
        if getattr(p, "page_number", 1) == 1 and getattr(p, "confidence", None) is not None:
            return max(0.0, min(1.0, float(p.confidence)))
    conf = getattr(getattr(md, "document", None), "confidence", None)
    return max(0.0, min(1.0, float(conf))) if conf is not None else None


def _local_geometry_lines(doc: LoadedDocument) -> list[OcrLine]:
    """Positions from the PDF text layer or local OCR - used when the parser returns no bounding boxes."""
    if doc.is_pdf:
        res = pdf_text_layer(doc)
        if res is not None:
            return res.lines
    try:
        return rapid_ocr(doc).lines
    except OcrError:
        return []


def llamaparse(doc: LoadedDocument, client=None) -> OcrResult:
    """LlamaParse -> markdown (for the LLM reader) + positioned lines (for checkbox/signature/TIN logic).

    Positions come from local geometry, because LlamaParse items are block-level. Cost comes from the
    job's reported credits, else a per-tier estimate.
    """
    client = client or _llamacloud_client()
    tier = os.environ.get("LLAMAPARSE_TIER", "agentic")
    try:
        file_obj = client.files.create(file=(doc.path.name, doc.data, doc.mime_type), purpose="parse")
        result = None
        for i, expand in enumerate(_EXPAND_ATTEMPTS):
            try:
                result = client.parsing.parse(file_id=file_obj.id, tier=tier, version="latest", expand=list(expand))
                break
            except Exception as e:  # an unsupported `expand` value is a 400/422 -> retry with fewer fields
                if getattr(e, "status_code", None) in (400, 422) and i < len(_EXPAND_ATTEMPTS) - 1:
                    continue
                raise
    except OcrError:
        raise
    except Exception as e:
        raise OcrError(f"LlamaParse request failed ({type(e).__name__}): {str(e)[:200]}") from e

    job = getattr(result, "job", None)
    if job is not None and getattr(job, "status", "COMPLETED") != "COMPLETED":
        raise OcrError(f"LlamaParse job {job.status}: {getattr(job, 'error_message', '') or 'no detail'}")
    markdown = getattr(result, "markdown_full", None) or ""
    if not markdown:
        md_pages = getattr(getattr(result, "markdown", None), "pages", None) or []
        markdown = "\n\n".join(p.markdown for p in md_pages if getattr(p, "success", False))
    page_conf = _llamaparse_page_confidence(result)
    # Positions come from local geometry (PDF text layer or RapidOCR). LlamaParse layout items are block-level
    # (label and value merged), which the line-based layout reader cannot use - see DESIGN.md 2.1.
    lines, geometry = _local_geometry_lines(doc), "local-geometry"
    credits = getattr(getattr(job, "usage", None), "credits", None)
    if credits is None:
        credits = LLAMAPARSE_CREDITS_PER_PAGE.get(tier, 10) * len(doc.page_images)
    return OcrResult(provider=f"llamaparse:{tier}({geometry})", lines=lines, markdown=markdown or None,
                     page_confidence=page_conf, cost_usd=float(credits) * LLAMAPARSE_USD_PER_CREDIT,
                     page_count=len(doc.page_images), dpi=RENDER_DPI, page_images=doc.page_images,
                     image_bytes_png=_png_bytes(doc.page_images[0]))


# ---------------------------------------------------------------------------
# Provider 3: RapidOCR (local ONNX models, pip-installable, no system deps)
# ---------------------------------------------------------------------------
_rapid_engine = None


def _rapid_engine_get():
    """Lazily construct the local OCR engine (None if not installed)."""
    global _rapid_engine
    if _rapid_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return None
        _rapid_engine = RapidOCR()
    return _rapid_engine


def rapid_ocr(doc: LoadedDocument) -> OcrResult:
    import numpy as np

    if _rapid_engine_get() is None:
        raise OcrError("No OCR provider for image input: install rapidocr_onnxruntime or set LLAMA_CLOUD_API_KEY")
    img = doc.page_images[0]
    result, _ = _rapid_engine(np.array(img))
    lines: list[OcrLine] = []
    for box, text, score in result or []:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        lines.append(OcrLine(text=text, bbox=(min(xs), min(ys), max(xs), max(ys)), confidence=float(score), page=1))
    lines.sort(key=lambda l: (round(l.bbox[1] / 10), l.bbox[0]))
    # Normalise image resolution to RENDER_DPI-equivalent coordinates is unnecessary: extraction
    # works in the image's own pixel space, and ink analysis uses the same image.
    return OcrResult(provider="rapidocr-onnx", lines=lines, page_count=1, dpi=RENDER_DPI,
                     page_images=doc.page_images, image_bytes_png=_png_bytes(img))


def run_ocr(doc: LoadedDocument, provider: str = "auto") -> OcrResult:
    """auto: PDF text layer -> LlamaParse (if LLAMA_CLOUD_API_KEY is set) -> local RapidOCR."""
    if provider in ("auto", "pdf-text") and doc.is_pdf:
        res = pdf_text_layer(doc)
        if res is not None:
            return res
        if provider == "pdf-text":
            raise OcrError("PDF has no usable text layer (scanned?)")
    if provider == "llamaparse":
        return llamaparse(doc)
    if provider == "auto" and os.environ.get("LLAMA_CLOUD_API_KEY"):
        try:
            return llamaparse(doc)
        except OcrError as e:  # prototype convenience; production alerts instead of silently degrading
            log_event(_log, "ocr_fallback", provider="llamaparse", reason=str(e)[:160])
            res = rapid_ocr(doc)
            res.provider += "(llamaparse-failed-fallback)"
            return res
    if provider in ("auto", "rapidocr"):
        return rapid_ocr(doc)
    raise OcrError(f"Unknown OCR provider '{provider}'")


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
