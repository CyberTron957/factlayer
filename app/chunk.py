"""Split parsed pages into LLM-sized chunks that never cross a page boundary
(so every fact's evidence page is exact). Tables are kept whole when they fit;
oversize tables are split row-wise with the header repeated."""
from dataclasses import dataclass

from .config import settings
from .parse import Page


@dataclass
class Chunk:
    doc: str
    page_index: int
    page_label: str
    text: str
    modality_hints: list
    page_text: str = ""  # full page markdown; fallback span search stays on-page


def chunk_pages(pages: list[Page], doc_name: str) -> list[Chunk]:
    out: list[Chunk] = []
    for p in pages:
        for piece in _split_page(p.markdown):
            if piece.strip():
                out.append(Chunk(doc=doc_name, page_index=p.index,
                               page_label=p.label, text=piece,
                               modality_hints=p.modality_hints,
                               page_text=p.markdown))
    return out


def _split_page(md: str, limit: int = 0) -> list[str]:
    limit = limit or settings.max_chunk_chars
    if len(md) <= limit:
        return [md]
    # try splitting on table-row boundaries first, then paragraphs
    paras = md.split("\n\n")
    chunks, cur = [], ""
    for para in paras:
        lines = para.split("\n")
        if len(para) > limit and all(l.strip().startswith("|") for l in lines if l.strip()):
            header = next((l for l in lines if l.strip().startswith("|")), "")
            if cur.strip():
                chunks.append(cur)
                cur = ""
            row_cur = header + "\n"
            for line in lines[1:]:
                if len(row_cur) + len(line) + 1 > limit:
                    chunks.append(row_cur)
                    row_cur = header + "\n" + line + "\n"
                else:
                    row_cur += line + "\n"
            if row_cur.strip() and row_cur.strip() != header.strip():
                chunks.append(row_cur)
            continue
        if len(cur) + len(para) + 2 > limit and cur.strip():
            chunks.append(cur)
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        chunks.append(cur)
    return chunks
