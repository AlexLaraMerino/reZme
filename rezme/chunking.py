"""Troceado de la transcripción en tramos para la extracción.

Tramos de unos 10 minutos: cada llamada al modelo repite las instrucciones
enteras, así que tramos cortos multiplican el coste sin aportar nada. Si el
vídeo tiene capítulos, se agrupan capítulos consecutivos hasta llenar un tramo
(el corte cae siempre entre capítulos); los capítulos largos y los vídeos sin
capítulos se parten en ventanas con un solape pequeño. Cada tramo conserva sus
cues con los segundos de inicio, que es lo que después permite anclar cada
afirmación a un momento del vídeo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

WINDOW_S = 600.0
OVERLAP_S = 20.0
MAX_CHAPTER_S = 900.0
# Sin duración conocida, se supone que el último cue dura esto.
LAST_CUE_S = 10.0

Cue = tuple[float, str]


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    cues: list[Cue]
    title: str | None = None

    @property
    def text(self) -> str:
        return " ".join(t for _, t in self.cues)


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def render(chunk: Chunk, step: float = 20.0) -> str:
    """Texto del tramo en bloques de ~20 s con marca [hh:mm:ss] al principio."""
    lines: list[str] = []
    current: list[str] = []
    block_start = chunk.cues[0][0] if chunk.cues else chunk.start
    for start, text in chunk.cues:
        if start - block_start >= step and current:
            lines.append(f"[{hms(block_start)}] " + " ".join(current))
            current, block_start = [], start
        current.append(text)
    if current:
        lines.append(f"[{hms(block_start)}] " + " ".join(current))
    return "\n".join(lines)


def _windows(start: float, end: float, window_s: float, overlap_s: float) -> list[tuple[float, float]]:
    out = []
    t = start
    while True:
        w_end = min(t + window_s, end)
        if end - w_end < window_s / 4:  # evita una última ventana casi vacía
            w_end = end
        out.append((t, w_end))
        if w_end >= end:
            return out
        t = w_end - overlap_s


def _chapter_spans(chapters: Sequence[dict[str, Any]], first: float,
                   end: float) -> list[tuple[float, float, str | None]]:
    starts = []
    for ch in chapters:
        try:
            starts.append((float(ch["start_time"]), str(ch.get("title") or "").strip() or None))
        except (KeyError, TypeError, ValueError):
            continue
    starts.sort(key=lambda x: x[0])
    spans: list[tuple[float, float, str | None]] = []
    if starts and starts[0][0] > first:  # texto anterior al primer capítulo
        spans.append((first, starts[0][0], None))
    for i, (s, title) in enumerate(starts):
        e = starts[i + 1][0] if i + 1 < len(starts) else end
        if e > s:
            spans.append((s, e, title))
    return spans


def _pack(spans: list[tuple[float, float, str | None]], window_s: float, overlap_s: float,
          max_chapter_s: float) -> list[tuple[float, float, str | None]]:
    """Agrupa capítulos consecutivos hasta `window_s`; parte los que superan `max_chapter_s`."""
    pieces: list[tuple[float, float, str | None]] = []
    group: list[tuple[float, float, str | None]] = []

    def flush() -> None:
        if group:
            titles = list(dict.fromkeys(t for _, _, t in group if t))
            pieces.append((group[0][0], group[-1][1], " · ".join(titles)[:200] or None))
            group.clear()

    for span in spans:
        s, e, title = span
        if e - s > max_chapter_s:
            flush()
            pieces.extend((ws, we, title) for ws, we in _windows(s, e, window_s, overlap_s))
            continue
        if group and e - group[0][0] > window_s:
            flush()
        group.append(span)
    flush()
    return pieces


def chunk_transcript(cues: Sequence[Cue], chapters: Sequence[dict[str, Any]] | None = None,
                     duration: float | None = None, *, window_s: float | None = None,
                     overlap_s: float | None = None,
                     max_chapter_s: float | None = None) -> list[Chunk]:
    """Tramos en orden temporal. El resultado es determinista para la misma entrada."""
    window_s = WINDOW_S if window_s is None else window_s
    overlap_s = OVERLAP_S if overlap_s is None else overlap_s
    max_chapter_s = MAX_CHAPTER_S if max_chapter_s is None else max_chapter_s
    if not 0 <= overlap_s < window_s:
        raise ValueError("el solape debe ser menor que la ventana")
    cues = sorted(((float(s), t) for s, t in cues), key=lambda c: c[0])
    if not cues:
        return []
    first = cues[0][0]
    end = max(float(duration or 0), cues[-1][0] + LAST_CUE_S)

    spans = _chapter_spans(chapters or [], first, end)
    if spans:
        pieces = _pack(spans, window_s, overlap_s, max_chapter_s)
    else:
        pieces = [(s, e, None) for s, e in _windows(first, end, window_s, overlap_s)]

    chunks: list[Chunk] = []
    for s, e, title in pieces:
        inside = [c for c in cues if s <= c[0] < e]
        if inside:
            chunks.append(Chunk(len(chunks), s, e, inside, title))
    return chunks
