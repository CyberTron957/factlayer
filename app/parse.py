"""PDF -> per-page markdown. LlamaParse primary, PyMuPDF fallback.

Page identity keeps BOTH the 0-based file index and LlamaParse's printed
label (curated excerpts jump: file index 11 == printed page 12).
Results are cached by file SHA so re-runs and incremental adds cost nothing.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from .config import settings

CACHE_DIR = os.path.join(settings.data_dir, "parse_cache")
SEPARATOR = "\n== PAGE {n} ==\n"


@dataclass
class Page:
    index: int            # 0-based file index
    label: str            # printed label (may equal str(index+1))
    markdown: str
    modality_hints: list = field(default_factory=list)  # e.g. ["table","chart"]


def sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def _cache_paths(path: str):
    key = sha_of(path) + "_" + os.path.basename(path).replace("/", "_")
    return os.path.join(CACHE_DIR, key + ".json")


def parse_pdf(path: str, target_pages: str | None = None,
              force: str = "auto") -> list[Page]:
    """force: 'auto' | 'llamaparse' | 'pypdf'. Returns pages in file order."""
    cp = _cache_paths(path)
    if os.path.exists(cp) and target_pages is None:
        with open(cp) as f:
            raw = json.load(f)
        return [Page(**p) for p in raw["pages"]]

    use_llama = (force in ("auto", "llamaparse")) and settings.llama_cloud_api_key
    if use_llama:
        try:
            pages = _parse_llamaparse(path, target_pages)
        except Exception as e:
            if force == "llamaparse":
                raise
            print(f"[parse] llamaparse failed ({e}); falling back to pymupdf")
            pages = _parse_pymupdf(path, target_pages)
    else:
        pages = _parse_pymupdf(path, target_pages)

    if target_pages is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, "w") as f:
            json.dump({"pages": [p.__dict__ for p in pages]}, f)
    return pages


def _parse_llamaparse(path: str, target_pages: str | None) -> list[Page]:
    from llama_parse import LlamaParse

    def one(tp: str):
        parser = LlamaParse(
            api_key=settings.llama_cloud_api_key,
            result_type="markdown",
            verbose=False,
            target_pages=tp,
            page_separator=SEPARATOR.format(n="{pageNumber}"),
        )
        docs = parser.load_data(path)
        return "\n".join(d.text for d in docs)

    if target_pages:
        # request each page individually: index mapping stays exact even
        # when the API omits separators or drops pages from a batch
        pages: list[Page] = []
        for idx in _expand_target(target_pages, 0):
            try:
                body = one(str(idx))
            except Exception as e:
                print(f"[parse] page {idx} failed: {e}")
                continue
            label_m = re.search(r"== PAGE (\S+) ==", body)
            if label_m:
                label = label_m.group(1).strip()
            else:
                label = _folio_label(body)  # printed folio footer, else fallback
                if label == "?":
                    label = str(idx + 1)
            body = re.sub(r"\n== PAGE \S+ ==\n?", "\n", body).strip()
            if label_m is None and label != str(idx + 1):
                lines = body.splitlines()  # drop printed folio footer from text
                if lines and re.fullmatch(r"\s*\d{1,4}\s*", lines[-1]):
                    body = "\n".join(lines[:-1]).strip()
            if body:
                pages.append(Page(index=idx, label=label, markdown=body,
                                  modality_hints=_modality_hints(body)))
        return pages
    parser = LlamaParse(
        api_key=settings.llama_cloud_api_key,
        result_type="markdown",
        verbose=True,
        page_separator=SEPARATOR.format(n="{pageNumber}"),
    )
    docs = parser.load_data(path)
    full = "\n".join(d.text for d in docs)
    return _split_pages(full, None)


def _split_pages(full: str, target_pages: str | None) -> list[Page]:
    parts = re.split(r"\n== PAGE (\S+) ==\n?", full)
    if len(parts) == 1:  # separator scheme not honoured — single page blob
        idxs = _expand_target(target_pages, 1)
        return [Page(index=idxs[0], label="?", markdown=full.strip(),
                     modality_hints=_modality_hints(full))]
    pages: list[Page] = []
    first, rest = parts[0], parts[1:]
    labels, bodies = rest[0::2], rest[1::2]
    if first.strip():  # first page has no leading separator — keep it
        bodies = [first] + bodies
        labels = [_folio_label(first)] + labels
    requested = _expand_target(target_pages, len(bodies))
    for i, (label, body) in enumerate(zip(labels, bodies)):
        idx = requested[i] if i < len(requested) else i
        body = body.strip()
        if body:
            pages.append(Page(index=idx, label=str(label).strip(),
                              markdown=body, modality_hints=_modality_hints(body)))
    return pages


def _folio_label(body: str) -> str:
    """Trailing bare number (printed folio footer) or '?'."""
    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    if lines and re.fullmatch(r"\d{1,4}", lines[-1]):
        return lines[-1]
    return "?"


def _expand_target(target_pages: str | None, n: int) -> list[int]:
    if not target_pages:
        return list(range(n))
    out = []
    for tok in target_pages.split(","):
        tok = tok.strip()
        if "-" in tok:
            a, b = tok.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif tok:
            out.append(int(tok))
    return out


def _modality_hints(md: str) -> list:
    hints = []
    if "|" in md and "---" in md:
        hints.append("table")
    if re.search(r"\b(chart|margin|YoY|QoQ|legend)\b", md, re.I):
        hints.append("chart")
    return hints


def _parse_pymupdf(path: str, target_pages: str | None) -> list[Page]:
    import fitz
    doc = fitz.open(path)
    idxs = _expand_target(target_pages, len(doc))
    pages = []
    for i in idxs:
        page = doc[i]
        blocks = sorted(page.get_text("blocks"),
                        key=lambda b: (round(b[1] / 20), b[0]))  # row-major reading order
        text = "\n".join(b[4] for b in blocks)
        hints: list = []
        try:
            tables = page.find_tables()
            for t in tables:
                rows = t.extract()
                text += "\n\n" + "\n".join(
                    "| " + " | ".join((c or "").strip() for c in r) + " |" for r in rows if r)
            if tables:
                hints.append("table")
        except Exception:
            pass
        pages.append(Page(index=i, label=str(i + 1), markdown=text.strip(),
                          modality_hints=list(set(hints + _modality_hints(text)))))
    return pages


def render_crop(path: str, page_index: int, out_path: str, zoom: float = 1.5) -> str:
    """Render a full-page PNG as visual evidence. Returns out_path."""
    import fitz
    doc = fitz.open(path)
    pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pix.save(out_path)
    return out_path
