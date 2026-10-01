"""Esquema universal de reZme: vocabularios controlados, validación y tiempo.

El esquema es agnóstico al dominio (macro, empresas, biología, medicina…).
Lo específico de inversión vive en `Implication`, que enlaza una afirmación con
su efecto sobre un activo, sector o variable macro.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA_VERSION = 4

CLAIM_TYPES = (
    "fact", "statistic", "study_result", "causal_claim", "forecast", "opinion",
    "own_calculation", "recommendation", "risk", "catalyst", "methodology", "definition",
    # Conocimiento que dura: cómo funciona algo, no qué pasó ayer.
    "mechanism", "mental_model", "heuristic", "framework", "historical_case",
)
EVIDENCE_GRADES = (
    "primary_data", "peer_reviewed", "official_stat", "cited_secondary",
    "own_analysis", "expert_opinion", "anecdote", "none",
)
STANCES = ("bullish", "bearish", "neutral", "n/a")
CLAIM_STATUSES = ("candidate", "verified", "ungrounded", "rejected", "superseded")
ENTITY_TYPES = (
    "company", "security", "crypto_asset", "commodity", "currency", "index",
    "country", "central_bank", "macro_indicator", "sector", "technology", "drug",
    "disease", "biological_concept", "person", "organization", "regulation",
    "event", "concept",
)
DIRECTIONS = ("positive", "negative", "mixed", "unclear")
# stated_by_source: lo dice el autor. inferred_by_system: lo deduce el modelo.
IMPLICATION_BASES = ("stated_by_source", "inferred_by_system")
# Para qué decisión de inversión sirve una afirmación (mejora la recuperación por los agentes).
DECISION_TAGS = (
    "valuation", "business_quality", "competitive_advantage", "management", "capital_allocation",
    "accounting", "risk", "position_sizing", "market_structure", "macro", "liquidity", "behavioral",
)
RELATION_TYPES = (
    "supports", "contradicts", "refines", "causes", "caused_by", "example_of",
    "counterexample_of", "depends_on", "related_to", "generalizes", "specializes",
)
TRANSCRIPT_ORIGINS = ("subtitles_manual", "subtitles_auto", "whisper", "pasted")
FORECAST_RESOLUTIONS = ("pending", "correct", "incorrect", "partial", "void")
JOB_STATUSES = ("pending", "running", "done", "failed", "skipped")
JOB_STAGES = ("ingest", "extract")

# Caducidad por defecto (días desde la fecha de la fuente). None = no caduca.
# Son valores iniciales ajustables: un dato estadístico envejece antes que un
# mecanismo biológico, y un precio objetivo antes que un hecho histórico.
SHELF_LIFE_DAYS: dict[str, int | None] = {
    "fact": 730, "statistic": 120, "study_result": 1825, "causal_claim": 1095,
    "forecast": 365, "opinion": 90, "own_calculation": 180, "recommendation": 60,
    "risk": 180, "catalyst": 180, "methodology": None, "definition": None,
    "mechanism": None, "mental_model": None, "heuristic": None, "framework": None,
    "historical_case": None,
}

MAX_STATEMENT_CHARS = 1000
# Cita literal corta: sirve para verificar, no para reproducir el contenido.
MAX_QUOTE_CHARS = 300
MAX_TITLE_CHARS = 150
# Mecanismo, condiciones de aplicación y de fallo: listas cortas de frases.
MAX_ITEMS = 6
MAX_ITEM_CHARS = 400
KNOWLEDGE_FIELDS = ("mechanism", "applies_when", "fails_when")


class ValidationError(ValueError):
    """Registro que no cumple el esquema."""


# --------------------------------------------------------------------------
# Tiempo
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Acepta AAAA, AAAA-MM, AAAA-MM-DD o fecha y hora ISO. Devuelve UTC."""
    value = value.strip()
    m = _DATE_RE.match(value)
    if m:
        return datetime(int(m[1]), int(m[2] or 1), int(m[3] or 1), tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_expires_at(claim_type: str, base: str | None, valid_to: str | None = None) -> str | None:
    """Caducidad: `valid_to` si existe; si no, vida útil del tipo desde `base`."""
    if valid_to:
        return to_iso(parse_iso(valid_to))
    days = SHELF_LIFE_DAYS.get(claim_type)
    if days is None:
        return None
    start = parse_iso(base) if base else datetime.now(timezone.utc)
    return to_iso(start + timedelta(days=days))


def parse_ts(value: Any) -> float:
    """Segundos a partir de número, 'ss', 'mm:ss', 'hh:mm:ss' o '[hh:mm:ss]'."""
    if isinstance(value, bool):
        raise ValidationError(f"marca de tiempo no válida: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValidationError(f"marca de tiempo no válida: {value!r}")
        return float(value)
    if isinstance(value, str):
        parts = value.strip().strip("[]").split(":")
        if 1 <= len(parts) <= 3 and all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
            seconds = 0.0
            for p in parts:
                seconds = seconds * 60 + float(p)
            return seconds
    raise ValidationError(f"marca de tiempo no válida: {value!r}")


# --------------------------------------------------------------------------
# Nombres
# --------------------------------------------------------------------------

def normalize_name(text: str) -> str:
    """Minúsculas, sin tildes ni signos: 'AST  SpaceMobile' == 'ast spacemobile'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", stripped.casefold()).strip()


# --------------------------------------------------------------------------
# Registros
# --------------------------------------------------------------------------

def _check_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValidationError(f"{name} no válido: {value!r} (permitidos: {', '.join(choices)})")


def _check_date(name: str, value: str | None) -> None:
    if value is None:
        return
    try:
        parse_iso(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{name} no es una fecha ISO válida: {value!r}") from None


def _check_unit_interval(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValidationError(f"{name} debe estar entre 0 y 1: {value!r}")


@dataclass
class Entity:
    type: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)

    def validate(self) -> "Entity":
        _check_choice("type", self.type, ENTITY_TYPES)
        if not normalize_name(self.canonical_name):
            raise ValidationError("canonical_name vacío")
        return self


@dataclass
class Claim:
    """Una idea atómica con su estatus epistémico, su anclaje y su vigencia."""
    source_id: int
    statement: str
    type: str
    entity_id: int | None = None
    domain: str | None = None
    evidence_grade: str = "none"
    stance: str = "n/a"
    metric_name: str | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    metric_period: str | None = None
    currency: str | None = None
    as_of: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    horizon: str | None = None
    ts_start: float | None = None
    ts_end: float | None = None
    quote: str | None = None
    confidence: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    run_id: int | None = None
    transcript_id: int | None = None
    published_at: str | None = None
    expires_at: str | None = None
    # K = afirmación + mecanismo + cuándo aplica + cuándo falla. Cada elemento de las listas es
    # {"text", "basis"[, "quote"]}: `basis` dice si lo afirma el autor o lo deduce el sistema.
    title: str | None = None
    mechanism: list[dict[str, Any]] = field(default_factory=list)
    applies_when: list[dict[str, Any]] = field(default_factory=list)
    fails_when: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def validate(self) -> "Claim":
        _check_choice("type", self.type, CLAIM_TYPES)
        _check_choice("evidence_grade", self.evidence_grade, EVIDENCE_GRADES)
        _check_choice("stance", self.stance, STANCES)
        _check_choice("status", self.status, CLAIM_STATUSES)
        if not self.statement or not self.statement.strip():
            raise ValidationError("statement vacío")
        if len(self.statement) > MAX_STATEMENT_CHARS:
            raise ValidationError(f"statement supera {MAX_STATEMENT_CHARS} caracteres")
        if self.quote is not None and len(self.quote) > MAX_QUOTE_CHARS:
            raise ValidationError(f"quote supera {MAX_QUOTE_CHARS} caracteres: usa una cita corta")
        _check_unit_interval("confidence", self.confidence)
        if self.metric_value is not None:
            if isinstance(self.metric_value, bool) or not isinstance(self.metric_value, (int, float)) \
                    or not math.isfinite(self.metric_value):
                raise ValidationError(f"metric_value no es un número finito: {self.metric_value!r}")
        if self.ts_start is not None:
            self.ts_start = parse_ts(self.ts_start)
        if self.ts_end is not None:
            self.ts_end = parse_ts(self.ts_end)
        if self.ts_start is not None and self.ts_end is not None and self.ts_end < self.ts_start:
            raise ValidationError("ts_end anterior a ts_start")
        for name in ("as_of", "valid_from", "valid_to", "published_at", "expires_at"):
            _check_date(name, getattr(self, name))
        if self.valid_from and self.valid_to and parse_iso(self.valid_to) < parse_iso(self.valid_from):
            raise ValidationError("valid_to anterior a valid_from")
        if not isinstance(self.attrs, dict):
            raise ValidationError("attrs debe ser un objeto")
        if self.title is not None and (not isinstance(self.title, str)
                                       or len(self.title) > MAX_TITLE_CHARS):
            raise ValidationError(f"title debe ser un texto de hasta {MAX_TITLE_CHARS} caracteres")
        for name in KNOWLEDGE_FIELDS:
            items = getattr(self, name)
            if not isinstance(items, list) or len(items) > MAX_ITEMS:
                raise ValidationError(f"{name} debe ser una lista de hasta {MAX_ITEMS} elementos")
            for item in items:
                if not isinstance(item, dict) or set(item) - {"text", "basis", "quote"}:
                    raise ValidationError(f"{name}: cada elemento es un objeto con text, basis y quote")
                text = item.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_ITEM_CHARS:
                    raise ValidationError(
                        f"{name}: text debe ser un texto de hasta {MAX_ITEM_CHARS} caracteres")
                _check_choice(f"{name}.basis", item.get("basis"), IMPLICATION_BASES)
                quote = item.get("quote")
                if quote is not None and (not isinstance(quote, str) or len(quote) > MAX_QUOTE_CHARS):
                    raise ValidationError(f"{name}: quote supera {MAX_QUOTE_CHARS} caracteres")
        if not isinstance(self.tags, list):
            raise ValidationError("tags debe ser una lista")
        for tag in self.tags:
            _check_choice("tags", tag, DECISION_TAGS)
        return self

    def fingerprint(self) -> str:
        """Identidad de la idea dentro de una fuente (evita duplicados al reintentar)."""
        raw = f"{self.source_id}|{self.entity_id}|{self.type}|{normalize_name(self.statement)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class Implication:
    """Efecto de una afirmación sobre algo invertible.

    `basis` distingue lo que dice la fuente de lo que deduce el sistema; los
    agentes deben poder ponderarlos distinto.
    """
    claim_id: int
    direction: str
    basis: str
    target_entity_id: int | None = None
    target_label: str | None = None
    mechanism: str | None = None
    horizon: str | None = None
    strength: float | None = None
    confidence: float | None = None
    conditional_on: str | None = None  # la misma idea puede ser positiva o negativa según el contexto

    def validate(self) -> "Implication":
        _check_choice("direction", self.direction, DIRECTIONS)
        _check_choice("basis", self.basis, IMPLICATION_BASES)
        if self.target_entity_id is None and not (self.target_label and self.target_label.strip()):
            raise ValidationError("la implicación necesita target_entity_id o target_label")
        _check_unit_interval("strength", self.strength)
        _check_unit_interval("confidence", self.confidence)
        return self
