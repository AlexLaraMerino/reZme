"""Verificación determinista (sin LLM) de las afirmaciones extraídas.

Una afirmación pasa a `verified` solo si:
  a) su cita aparece en el tramo, con coincidencia aproximada tras normalizar
     tildes, mayúsculas y puntuación, y
  b) su cifra (`metric_value`) se encuentra en el texto del tramo, aceptando
     1,36 / 1.36 / 1 36, separadores de miles, escalas («45 mil millones») y
     cifras dictadas sencillas («uno coma treinta y seis»).
Si no, queda `ungrounded` con el motivo en `attrs["grounding"]`.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .chunking import Chunk
from .schema import Claim, normalize_name
from .store import Store

QUOTE_THRESHOLD = 0.85
MIN_QUOTE_TOKENS = 4


@dataclass
class Grounding:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    quote_score: float | None = None
    ts_start: float | None = None
    ts_end: float | None = None

    def as_attrs(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.quote_score is not None:
            out["similitud_cita"] = round(self.quote_score, 3)
        if self.reasons:
            out["motivos"] = list(self.reasons)
        return out


# --------------------------------------------------------------------------
# Citas
# --------------------------------------------------------------------------

def _tokens(chunk: Chunk) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    owner: list[int] = []  # índice del cue al que pertenece cada token
    for i, (_, text) in enumerate(chunk.cues):
        words = normalize_name(text).split()
        tokens.extend(words)
        owner.extend([i] * len(words))
    return tokens, owner


def _exact(tokens: list[str], quote: list[str]) -> int | None:
    n = len(quote)
    first = quote[0]
    for i in range(len(tokens) - n + 1):
        if tokens[i] == first and tokens[i:i + n] == quote:
            return i
    return None


def find_quote(quote: str, chunk: Chunk) -> tuple[float, float | None, float | None]:
    """(similitud, ts_start, ts_end) de la mejor coincidencia de la cita en el tramo."""
    q = normalize_name(quote).split()
    tokens, owner = _tokens(chunk)
    if not q or not tokens:
        return 0.0, None, None

    best, best_i, best_len = 0.0, 0, len(q)
    pos = _exact(tokens, q)
    if pos is not None:
        best, best_i = 1.0, pos
    else:
        qset = set(q)
        hits = [0]
        for t in tokens:
            hits.append(hits[-1] + (t in qset))
        slack = max(1, len(q) // 8)
        for length in sorted({max(1, len(q) - slack), len(q), len(q) + slack}):
            length = min(length, len(tokens))
            for i in range(len(tokens) - length + 1):
                # Descarta ventanas que no pueden alcanzar el umbral.
                if hits[i + length] - hits[i] < 0.7 * min(length, len(q)):
                    continue
                score = SequenceMatcher(None, tokens[i:i + length], q, autojunk=False).ratio()
                if score > best:
                    best, best_i, best_len = score, i, length

    if best < QUOTE_THRESHOLD:
        return best, None, None
    first_cue = owner[best_i]
    last_cue = owner[min(best_i + best_len, len(tokens)) - 1]
    ts_start = chunk.cues[first_cue][0]
    ts_end = chunk.cues[last_cue + 1][0] if last_cue + 1 < len(chunk.cues) else chunk.end
    return best, ts_start, max(ts_end, ts_start)


# --------------------------------------------------------------------------
# Cifras
# --------------------------------------------------------------------------

_SMALL = {
    "cero": 0, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veintiun": 21, "veintiuno": 21, "veintiuna": 21, "veintidos": 22,
    "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90,
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_HUNDREDS = {
    "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300,
    "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500,
    "quinientas": 500, "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
    "setecientas": 700, "ochocientos": 800, "ochocientas": 800, "novecientos": 900,
    "novecientas": 900,
}
_THOUSAND = {"mil", "thousand"}
_JOINERS = {"y", "and"}
_DECIMAL_WORDS = {"coma", "punto", "point", "con"}
# «billón» (es) = 10^12; «billion» (en) = 10^9.
_SCALES = {
    "mil": 1e3, "miles": 1e3, "millon": 1e6, "millones": 1e6, "billon": 1e12,
    "billones": 1e12, "trillon": 1e18, "trillones": 1e18, "thousand": 1e3, "million": 1e6,
    "millions": 1e6, "billion": 1e9, "billions": 1e9, "trillion": 1e12, "trillions": 1e12,
}
_NUM_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _digit_values(token: str) -> set[float]:
    """Lecturas posibles de '1,36', '1.000', '1.234,56'…"""
    parts = re.split(r"[.,]", token)
    seps = re.findall(r"[.,]", token)
    if not seps:
        return {float(token)}
    out: set[float] = set()
    if len(seps) == 1:
        out.add(float(f"{parts[0]}.{parts[1]}"))
        if len(parts[1]) == 3 and not parts[0].startswith("0"):  # o separador de miles
            out.add(float(parts[0] + parts[1]))
    elif len(set(seps)) == 1:
        if all(len(p) == 3 for p in parts[1:]):
            out.add(float("".join(parts)))
    elif all(s == seps[0] for s in seps[:-1]):  # miles + decimal: 1.234,56
        out.add(float("".join(parts[:-1]) + "." + parts[-1]))
    return out


def _word_number(tokens: list[str], i: int) -> tuple[int, int]:
    """Número escrito con palabras a partir de `i`. Devuelve (valor, fin); fin == i si no hay."""
    total = current = 0
    j = i
    seen = False
    while j < len(tokens):
        w = tokens[j]
        if w in _SMALL:
            v = _SMALL[w]
            if seen:
                tens_free = current % 100 == 0
                unit_free = current % 10 == 0 and current % 100 >= 20
                if not (current > 0 or total > 0) or not (tens_free or (v < 10 and unit_free)):
                    break
            current += v
        elif w in _HUNDREDS:
            if seen and current != 0:
                break
            current += _HUNDREDS[w]
        elif w == "hundred" and seen and 0 < current < 10:
            current *= 100
        elif w in _THOUSAND and total == 0:
            total = max(current, 1) * 1000
            current = 0
        elif w in _JOINERS and seen and j + 1 < len(tokens) and tokens[j + 1] in _SMALL:
            j += 1
            continue
        else:
            break
        seen = True
        j += 1
    return total + current, (j if seen else i)


def numbers_in(text: str) -> set[float]:
    """Todos los valores numéricos que se pueden leer en el texto."""
    toks = _NUM_TOKEN_RE.findall(_fold(text))
    atoms: dict[int, tuple[int, set[float], str | None, bool]] = {}  # inicio -> (fin, valores, dígitos, es_cifra)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t[0].isdigit():
            atoms[i] = (i + 1, _digit_values(t), t if t.isdigit() else None, True)
            i += 1
        elif t in ("un", "una") and i + 1 < len(toks) and toks[i + 1] in _SCALES:
            atoms[i] = (i + 1, {1.0}, "1", False)
            i += 1
        else:
            value, j = _word_number(toks, i)
            if j > i:
                atoms[i] = (j, {float(value)}, str(value), False)
                i = j
            else:
                i += 1

    found: set[float] = set()
    for start, (end, values, digits, is_digit) in atoms.items():
        bases: list[tuple[set[float], int]] = [(values, end)]
        if digits is not None:
            after = atoms.get(end + 1)
            if end < len(toks) and toks[end] in _DECIMAL_WORDS and after and after[2] is not None:
                bases.append(({float(f"{digits}.{after[2]}")}, after[0]))  # «1 coma 36»
            nxt = atoms.get(end)
            if is_digit and nxt and nxt[3] and nxt[2] is not None:
                bases.append(({float(f"{digits}.{nxt[2]}")}, nxt[0]))  # «1 36»
                joined, k = digits, nxt
                while k and k[3] and k[2] is not None and len(k[2]) == 3:  # «45 000 000»
                    joined += k[2]
                    bases.append(({float(joined)}, k[0]))
                    k = atoms.get(k[0])
        for vals, e in bases:
            found |= vals
            scale = 1.0
            while e < len(toks) and toks[e] in _SCALES:
                scale *= _SCALES[toks[e]]
                e += 1
                found |= {v * scale for v in vals}
    return found


def number_in_text(value: float, text: str) -> bool:
    target = abs(float(value))
    return any(math.isclose(target, c, rel_tol=1e-6, abs_tol=1e-9) for c in numbers_in(text))


# --------------------------------------------------------------------------
# Veredicto
# --------------------------------------------------------------------------

def _fmt(value: float) -> str:
    return f"{value:g}"


def ground(quote: str | None, metric_value: float | None, chunk: Chunk) -> Grounding:
    result = Grounding(ok=True)
    if not quote or not quote.strip():
        result.reasons.append("sin cita literal")
    elif len(normalize_name(quote).split()) < MIN_QUOTE_TOKENS:
        result.reasons.append(
            f"cita demasiado corta para verificar (mínimo {MIN_QUOTE_TOKENS} palabras)")
    else:
        score, ts_start, ts_end = find_quote(quote, chunk)
        result.quote_score = score
        if ts_start is None:
            result.reasons.append(f"la cita no aparece en el tramo (similitud {score:.2f})")
        else:
            result.ts_start, result.ts_end = ts_start, ts_end
    if metric_value is not None and not number_in_text(metric_value, chunk.text):
        result.reasons.append(f"la cifra {_fmt(metric_value)} no aparece en el tramo")
    result.ok = not result.reasons
    return result


def verify_claim(claim: Claim, chunk: Chunk) -> Grounding:
    """Fija estado, motivo y anclaje temporal de una afirmación candidata."""
    result = ground(claim.quote, claim.metric_value, chunk)
    claim.status = "verified" if result.ok else "ungrounded"
    claim.attrs["grounding"] = result.as_attrs()
    if result.ts_start is not None:
        # El anclaje sale de dónde está la cita, no de lo que diga el modelo.
        claim.ts_start, claim.ts_end = result.ts_start, result.ts_end
    else:
        inside = (claim.ts_start is not None and chunk.start <= claim.ts_start <= chunk.end
                  and (claim.ts_end is None or claim.ts_start <= claim.ts_end <= chunk.end))
        if not inside:
            claim.ts_start, claim.ts_end = chunk.start, chunk.end
    return result


def verify_source(store: Store, source_id: int, *, run_id: int | None = None,
                  recheck: bool = False) -> dict[str, int]:
    """Verifica las afirmaciones `candidate` guardadas de una fuente.

    Con `recheck` repite también las ya verificadas o no ancladas (útil si
    cambian las reglas de verificación). Devuelve el recuento por estado.
    """
    transcript = store.latest_transcript(source_id)
    if transcript is None:
        raise ValueError(f"La fuente {source_id} no tiene transcripción guardada.")
    cues = transcript["cues"]
    states = ("candidate", "verified", "ungrounded") if recheck else ("candidate",)
    counts = {"verified": 0, "ungrounded": 0}
    for row in store.claims_for_source(source_id, run_id=run_id):
        if row["status"] not in states:
            continue
        span = row["attrs"].get("tramo") or {}
        start = float(span.get("inicio", cues[0][0]))
        end = float(span.get("fin", cues[-1][0] + 1))
        chunk = Chunk(int(span.get("indice", 0)), start, end,
                      [c for c in cues if start <= c[0] < end])
        result = ground(row["quote"], row["metric_value"], chunk)
        status = "verified" if result.ok else "ungrounded"
        attrs = dict(row["attrs"], grounding=result.as_attrs())
        ts_start = result.ts_start if result.ts_start is not None else row["ts_start"]
        ts_end = result.ts_end if result.ts_start is not None else row["ts_end"]
        store.set_claim_grounding(row["id"], status, attrs, ts_start, ts_end)
        counts[status] += 1
    return counts
