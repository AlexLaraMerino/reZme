"""Almacén SQLite de reZme (solo biblioteca estándar).

Reglas que el almacén hace cumplir:
  - Las transcripciones crudas se guardan una vez (hash) y permiten reextraer.
  - Los agentes solo reciben afirmaciones `verified`, vigentes y, si se pide,
    conocidas hasta una fecha (consultas «point-in-time» para backtesting).
  - Una entidad ambigua nunca se resuelve en silencio.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .schema import (
    CLAIM_STATUSES, CLAIM_TYPES, DIRECTIONS, ENTITY_TYPES, EVIDENCE_GRADES,
    FORECAST_RESOLUTIONS, IMPLICATION_BASES, JOB_STAGES, JOB_STATUSES, SCHEMA_VERSION, STANCES,
    TRANSCRIPT_ORIGINS, Claim, Entity, Implication, ValidationError,
    default_expires_at, normalize_name, utc_now,
)


class AmbiguousEntity(ValueError):
    """El nombre corresponde a más de una entidad; hay que indicar el tipo."""


def _in(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


# Cola de procesamiento por lotes: un trabajo por vídeo.
JOBS_DDL = f"""
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    playlist_url TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ({_in(JOB_STATUSES)})),
    stage TEXT NOT NULL DEFAULT 'ingest' CHECK (stage IN ({_in(JOB_STAGES)})),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, notes TEXT
);
CREATE INDEX idx_jobs_next ON jobs(status, priority DESC, id);
"""

DDL = f"""
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT, title TEXT, channel TEXT, channel_id TEXT,
    published_at TEXT, duration_s REAL, language TEXT,
    description TEXT, chapters_json TEXT,
    captured_at TEXT NOT NULL,
    UNIQUE (platform, external_id)
);

CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ({_in(TRANSCRIPT_ORIGINS)})),
    language TEXT,
    cues_json TEXT NOT NULL,
    n_cues INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, sha256)
);

CREATE TABLE extraction_runs (
    id INTEGER PRIMARY KEY,
    model TEXT, backend TEXT, prompt_version TEXT,
    schema_version INTEGER NOT NULL,
    cost_usd REAL, notes TEXT,
    created_at TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    transcript_id INTEGER REFERENCES transcripts(id) ON DELETE SET NULL,
    stats_json TEXT NOT NULL DEFAULT '{{}}'
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ({_in(ENTITY_TYPES)})),
    canonical_name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    external_ids_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    UNIQUE (type, norm_name)
);

CREATE TABLE entity_aliases (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    norm_alias TEXT NOT NULL,
    UNIQUE (entity_id, norm_alias)
);
CREATE INDEX idx_alias_norm ON entity_aliases(norm_alias);

CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    transcript_id INTEGER REFERENCES transcripts(id) ON DELETE SET NULL,
    run_id INTEGER REFERENCES extraction_runs(id) ON DELETE SET NULL,
    entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK (type IN ({_in(CLAIM_TYPES)})),
    domain TEXT,
    statement TEXT NOT NULL,
    evidence_grade TEXT NOT NULL CHECK (evidence_grade IN ({_in(EVIDENCE_GRADES)})),
    stance TEXT NOT NULL CHECK (stance IN ({_in(STANCES)})),
    metric_name TEXT, metric_value REAL, metric_unit TEXT,
    metric_period TEXT, currency TEXT,
    as_of TEXT, valid_from TEXT, valid_to TEXT, horizon TEXT,
    ts_start REAL, ts_end REAL, quote TEXT, confidence REAL,
    attrs_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL CHECK (status IN ({_in(CLAIM_STATUSES)})),
    published_at TEXT, captured_at TEXT NOT NULL, expires_at TEXT,
    fingerprint TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_claim_dedupe ON claims(source_id, IFNULL(run_id, 0), fingerprint);
CREATE INDEX idx_claim_entity ON claims(entity_id);
CREATE INDEX idx_claim_status ON claims(status, expires_at);

CREATE VIRTUAL TABLE claims_fts USING fts5(
    statement, quote, content='claims', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, statement, quote) VALUES (new.id, new.statement, new.quote);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, statement, quote)
    VALUES ('delete', old.id, old.statement, old.quote);
END;
CREATE TRIGGER claims_au AFTER UPDATE OF statement, quote ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, statement, quote)
    VALUES ('delete', old.id, old.statement, old.quote);
    INSERT INTO claims_fts(rowid, statement, quote) VALUES (new.id, new.statement, new.quote);
END;

CREATE TABLE implications (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    target_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    target_label TEXT,
    direction TEXT NOT NULL CHECK (direction IN ({_in(DIRECTIONS)})),
    mechanism TEXT, horizon TEXT, strength REAL, confidence REAL,
    basis TEXT NOT NULL CHECK (basis IN ({_in(IMPLICATION_BASES)})),
    created_at TEXT NOT NULL,
    CHECK (target_entity_id IS NOT NULL OR target_label IS NOT NULL)
);
CREATE INDEX idx_impl_claim ON implications(claim_id);
CREATE INDEX idx_impl_target ON implications(target_entity_id);

CREATE TABLE forecasts (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
    target_date TEXT,
    resolution TEXT NOT NULL DEFAULT 'pending'
        CHECK (resolution IN ({_in(FORECAST_RESOLUTIONS)})),
    resolved_at TEXT, notes TEXT
);

CREATE TABLE source_profiles (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{{}}',
    updated_at TEXT NOT NULL,
    UNIQUE (platform, channel_id)
);
""" + JOBS_DDL

# Pasos desde cada versión antigua hasta la actual.
_MIGRATIONS = {
    1: """
ALTER TABLE extraction_runs ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE;
ALTER TABLE extraction_runs ADD COLUMN transcript_id INTEGER
    REFERENCES transcripts(id) ON DELETE SET NULL;
ALTER TABLE extraction_runs ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}';
""",
    2: JOBS_DDL,
}

_STATS_TABLES = ("sources", "transcripts", "entities", "claims", "implications",
                 "forecasts", "extraction_runs", "jobs")
_JOB_FIELDS = frozenset({"status", "stage", "attempts", "last_error", "priority", "started_at",
                         "finished_at", "notes", "title"})


def _fts_query(text: str) -> str:
    """Convierte texto libre en una consulta FTS5 segura (todas las palabras)."""
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    if not tokens:
        raise ValueError("consulta vacía")
    return " ".join('"' + t + '"' for t in tokens)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        try:
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    # -- ciclo de vida -----------------------------------------------------

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"La base tiene esquema v{version}, más nuevo que esta versión (v{SCHEMA_VERSION}).")
        if version == 0:
            with self.db:
                self.db.executescript(DDL)
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for step in range(version or SCHEMA_VERSION, SCHEMA_VERSION):
            # Cada paso va en su transacción: si falla, la base queda en la versión anterior.
            self.db.executescript(
                f"BEGIN;\n{_MIGRATIONS[step]}\nPRAGMA user_version = {step + 1};\nCOMMIT;")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- fuentes y transcripciones ------------------------------------------

    def add_source(self, platform: str, external_id: str, *, url: str | None = None,
                   title: str | None = None, channel: str | None = None,
                   channel_id: str | None = None, published_at: str | None = None,
                   duration_s: float | None = None, language: str | None = None,
                   description: str | None = None,
                   chapters: list[dict[str, Any]] | None = None) -> tuple[int, bool]:
        """Crea la fuente o completa la existente sin pisar datos con vacíos."""
        existing = self.get_source(platform, external_id)
        chapters_json = json.dumps(chapters, ensure_ascii=False) if chapters else None
        with self.db:
            if existing:
                self.db.execute(
                    """UPDATE sources SET
                        url=COALESCE(?, url), title=COALESCE(?, title),
                        channel=COALESCE(?, channel), channel_id=COALESCE(?, channel_id),
                        published_at=COALESCE(?, published_at),
                        duration_s=COALESCE(?, duration_s), language=COALESCE(?, language),
                        description=COALESCE(?, description),
                        chapters_json=COALESCE(?, chapters_json)
                       WHERE id=?""",
                    (url, title, channel, channel_id, published_at, duration_s, language,
                     description, chapters_json, existing["id"]))
                return existing["id"], False
            cur = self.db.execute(
                """INSERT INTO sources (platform, external_id, url, title, channel, channel_id,
                    published_at, duration_s, language, description, chapters_json, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (platform, external_id, url, title, channel, channel_id, published_at,
                 duration_s, language, description, chapters_json, utc_now()))
            return cur.lastrowid, True

    def get_source(self, platform: str, external_id: str) -> dict[str, Any] | None:
        return _row(self.db.execute(
            "SELECT * FROM sources WHERE platform=? AND external_id=?",
            (platform, external_id)).fetchone())

    def get_source_by_id(self, source_id: int) -> dict[str, Any] | None:
        return _row(self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def list_sources(self) -> list[dict[str, Any]]:
        """Fuentes guardadas, la más reciente primero, con su transcripción y afirmaciones."""
        return [dict(r) for r in self.db.execute(
            """SELECT s.id, s.external_id, s.url, s.title, s.channel, s.published_at,
                  t.n_cues, t.origin,
                  (SELECT COUNT(*) FROM claims c
                    WHERE c.source_id = s.id AND c.status = 'verified') AS verified
               FROM sources s
               LEFT JOIN transcripts t ON t.id = (
                   SELECT MAX(id) FROM transcripts WHERE source_id = s.id)
               ORDER BY s.id DESC""")]

    def save_transcript(self, source_id: int, cues: list[tuple[float, str]], origin: str,
                        language: str | None = None) -> tuple[int, bool]:
        """Guarda los cues crudos. Mismo contenido para la misma fuente = no-op."""
        if origin not in TRANSCRIPT_ORIGINS:
            raise ValidationError(f"origin no válido: {origin!r}")
        if not cues:
            raise ValidationError("transcripción vacía")
        canonical = json.dumps([[round(float(s), 3), t] for s, t in cues],
                               ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        row = self.db.execute("SELECT id FROM transcripts WHERE source_id=? AND sha256=?",
                              (source_id, digest)).fetchone()
        if row:
            return row["id"], False
        with self.db:
            cur = self.db.execute(
                """INSERT INTO transcripts (source_id, origin, language, cues_json, n_cues,
                    sha256, created_at) VALUES (?,?,?,?,?,?,?)""",
                (source_id, origin, language, canonical, len(cues), digest, utc_now()))
        return cur.lastrowid, True

    def latest_transcript(self, source_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM transcripts WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (source_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["cues"] = [(s, t) for s, t in json.loads(out.pop("cues_json"))]
        return out

    # -- entidades ----------------------------------------------------------

    def upsert_entity(self, type: str, canonical_name: str, aliases: Iterable[str] = (),
                      external_ids: dict[str, str] | None = None) -> int:
        entity = Entity(type, canonical_name, list(aliases), dict(external_ids or {})).validate()
        norm = normalize_name(entity.canonical_name)
        with self.db:
            row = self.db.execute("SELECT id, external_ids_json FROM entities "
                                  "WHERE type=? AND norm_name=?", (entity.type, norm)).fetchone()
            if row is None:  # ¿ya existe con ese nombre como alias dentro del tipo?
                row = self.db.execute(
                    """SELECT e.id, e.external_ids_json FROM entities e
                       JOIN entity_aliases a ON a.entity_id = e.id
                       WHERE e.type=? AND a.norm_alias=?""", (entity.type, norm)).fetchone()
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO entities (type, canonical_name, norm_name, external_ids_json,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (entity.type, entity.canonical_name.strip(), norm,
                     json.dumps(entity.external_ids, ensure_ascii=False), utc_now()))
                entity_id = cur.lastrowid
            else:
                entity_id = row["id"]
                merged = {**json.loads(row["external_ids_json"]), **entity.external_ids}
                self.db.execute("UPDATE entities SET external_ids_json=? WHERE id=?",
                                (json.dumps(merged, ensure_ascii=False), entity_id))
            for alias in entity.aliases:
                n = normalize_name(alias)
                if n and n != norm:
                    self.db.execute(
                        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, norm_alias)"
                        " VALUES (?,?,?)", (entity_id, alias.strip(), n))
        return entity_id

    def resolve_entity(self, name: str, type: str | None = None) -> int | None:
        """Id de la entidad con ese nombre o alias. None si no existe.

        Lanza AmbiguousEntity si hay varias y no se indicó el tipo: un agente
        que opera con capital no debe confundir dos entidades parecidas.
        """
        norm = normalize_name(name)
        if not norm:
            return None
        sql = """SELECT DISTINCT e.id FROM entities e
                 LEFT JOIN entity_aliases a ON a.entity_id = e.id
                 WHERE (e.norm_name = ? OR a.norm_alias = ?)"""
        params: list[Any] = [norm, norm]
        if type:
            sql += " AND e.type = ?"
            params.append(type)
        ids = [r["id"] for r in self.db.execute(sql, params)]
        if len(ids) > 1:
            raise AmbiguousEntity(f"{name!r} coincide con {len(ids)} entidades; indica el tipo")
        return ids[0] if ids else None

    def entity_candidates(self, name: str) -> list[dict[str, Any]]:
        """Todas las entidades que responden a ese nombre o alias (para anotar ambigüedades)."""
        norm = normalize_name(name)
        return [dict(r) for r in self.db.execute(
            """SELECT DISTINCT e.id, e.type, e.canonical_name FROM entities e
               LEFT JOIN entity_aliases a ON a.entity_id = e.id
               WHERE e.norm_name = ? OR a.norm_alias = ? ORDER BY e.id""", (norm, norm))]

    def get_entity(self, entity_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["external_ids"] = json.loads(out.pop("external_ids_json"))
        out["aliases"] = [r["alias"] for r in self.db.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY alias", (entity_id,))]
        return out

    # -- extracción ----------------------------------------------------------

    def start_run(self, *, model: str | None = None, backend: str | None = None,
                  prompt_version: str | None = None, cost_usd: float | None = None,
                  notes: str | None = None, source_id: int | None = None,
                  transcript_id: int | None = None) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO extraction_runs (model, backend, prompt_version, schema_version,
                    cost_usd, notes, created_at, source_id, transcript_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (model, backend, prompt_version, SCHEMA_VERSION, cost_usd, notes, utc_now(),
                 source_id, transcript_id))
        return cur.lastrowid

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM extraction_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["stats"] = json.loads(out.pop("stats_json"))
        return out

    def find_run(self, source_id: int, transcript_id: int, prompt_version: str,
                 backend: str | None, model: str | None) -> dict[str, Any] | None:
        """Último run con la misma transcripción, prompt, backend y modelo (para reanudar)."""
        row = self.db.execute(
            """SELECT id FROM extraction_runs WHERE source_id=? AND transcript_id=?
               AND prompt_version=? AND backend IS ? AND model IS ?
               ORDER BY id DESC LIMIT 1""",
            (source_id, transcript_id, prompt_version, backend, model)).fetchone()
        return self.get_run(row["id"]) if row else None

    def latest_run(self, source_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id FROM extraction_runs WHERE source_id=? "
                              "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
        return self.get_run(row["id"]) if row else None

    def update_run(self, run_id: int, *, stats: dict[str, Any] | None = None,
                   cost_usd: float | None = None) -> None:
        with self.db:
            if stats is not None:
                self.db.execute("UPDATE extraction_runs SET stats_json=? WHERE id=?",
                                (json.dumps(stats, ensure_ascii=False), run_id))
            if cost_usd is not None:
                self.db.execute("UPDATE extraction_runs SET cost_usd=? WHERE id=?",
                                (cost_usd, run_id))

    def supersede_previous_runs(self, source_id: int, run_id: int) -> int:
        """Marca como `superseded` lo vigente de runs anteriores de la misma fuente.

        El histórico se conserva, pero los agentes solo ven la extracción más reciente.
        """
        with self.db:
            cur = self.db.execute(
                """UPDATE claims SET status='superseded' WHERE source_id=? AND run_id IS NOT NULL
                   AND run_id < ? AND status IN ('candidate', 'verified')""", (source_id, run_id))
        return cur.rowcount

    def add_claim(self, claim: Claim) -> tuple[int, bool]:
        """Inserta la afirmación (siempre validada). Devuelve (id, creada)."""
        claim.validate()
        src = self.db.execute("SELECT published_at FROM sources WHERE id=?",
                              (claim.source_id,)).fetchone()
        if src is None:
            raise ValidationError(f"la fuente {claim.source_id} no existe")
        fingerprint = claim.fingerprint()
        dup = self.db.execute(
            "SELECT id FROM claims WHERE source_id=? AND IFNULL(run_id,0)=? AND fingerprint=?",
            (claim.source_id, claim.run_id or 0, fingerprint)).fetchone()
        if dup:
            return dup["id"], False
        captured = utc_now()
        published = claim.published_at or src["published_at"]
        expires = claim.expires_at or default_expires_at(
            claim.type, published or captured, claim.valid_to)
        with self.db:
            cur = self.db.execute(
                """INSERT INTO claims (source_id, transcript_id, run_id, entity_id, type, domain,
                    statement, evidence_grade, stance, metric_name, metric_value, metric_unit,
                    metric_period, currency, as_of, valid_from, valid_to, horizon, ts_start,
                    ts_end, quote, confidence, attrs_json, status, published_at, captured_at,
                    expires_at, fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (claim.source_id, claim.transcript_id, claim.run_id, claim.entity_id, claim.type,
                 claim.domain, claim.statement.strip(), claim.evidence_grade, claim.stance,
                 claim.metric_name, claim.metric_value, claim.metric_unit, claim.metric_period,
                 claim.currency, claim.as_of, claim.valid_from, claim.valid_to, claim.horizon,
                 claim.ts_start, claim.ts_end, claim.quote, claim.confidence,
                 json.dumps(claim.attrs, ensure_ascii=False), claim.status, published, captured,
                 expires, fingerprint))
        return cur.lastrowid, True

    def set_claim_status(self, claim_id: int, status: str) -> None:
        if status not in CLAIM_STATUSES:
            raise ValidationError(f"status no válido: {status!r}")
        with self.db:
            cur = self.db.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
        if cur.rowcount == 0:
            raise KeyError(f"afirmación {claim_id} no existe")

    def set_claim_grounding(self, claim_id: int, status: str, attrs: dict[str, Any],
                            ts_start: float | None, ts_end: float | None) -> None:
        """Resultado de la verificación: estado, motivo en attrs y anclaje temporal."""
        if status not in CLAIM_STATUSES:
            raise ValidationError(f"status no válido: {status!r}")
        with self.db:
            cur = self.db.execute(
                "UPDATE claims SET status=?, attrs_json=?, ts_start=?, ts_end=? WHERE id=?",
                (status, json.dumps(attrs, ensure_ascii=False), ts_start, ts_end, claim_id))
        if cur.rowcount == 0:
            raise KeyError(f"afirmación {claim_id} no existe")

    def claims_for_source(self, source_id: int, *, run_id: int | None = None,
                          status: str | None = None) -> list[dict[str, Any]]:
        """Afirmaciones de una fuente en cualquier estado (revisión, no para agentes)."""
        sql = ("SELECT c.*, e.canonical_name AS entity_name FROM claims c "
               "LEFT JOIN entities e ON e.id = c.entity_id WHERE c.source_id = ?")
        params: list[Any] = [source_id]
        if run_id is not None:
            sql += " AND c.run_id = ?"
            params.append(run_id)
        if status:
            sql += " AND c.status = ?"
            params.append(status)
        out = []
        for row in self.db.execute(sql + " ORDER BY c.ts_start IS NULL, c.ts_start, c.id", params):
            item = dict(row)
            item["attrs"] = json.loads(item.pop("attrs_json"))
            out.append(item)
        return out

    def add_implication(self, implication: Implication) -> int:
        implication.validate()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO implications (claim_id, target_entity_id, target_label, direction,
                    mechanism, horizon, strength, confidence, basis, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (implication.claim_id, implication.target_entity_id, implication.target_label,
                 implication.direction, implication.mechanism, implication.horizon,
                 implication.strength, implication.confidence, implication.basis, utc_now()))
        return cur.lastrowid

    # -- cola de trabajos ------------------------------------------------------

    def enqueue_job(self, video_id: str, url: str, *, playlist_url: str | None = None,
                    title: str | None = None, priority: int = 0, stage: str = "ingest",
                    notes: str | None = None) -> tuple[int, bool]:
        """Encola un vídeo. Si ya estaba en la cola devuelve (id, False) sin tocarlo."""
        if stage not in JOB_STAGES:
            raise ValidationError(f"stage no válido: {stage!r}")
        row = self.db.execute("SELECT id FROM jobs WHERE video_id=?", (video_id,)).fetchone()
        if row:
            return row["id"], False
        with self.db:
            cur = self.db.execute(
                """INSERT INTO jobs (video_id, url, playlist_url, title, stage, priority, notes,
                    created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (video_id, url, playlist_url, title, stage, int(priority), notes, utc_now()))
        return cur.lastrowid, True

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return _row(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def update_job(self, job_id: int, **fields: Any) -> None:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValidationError(f"campos de trabajo no válidos: {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in JOB_STATUSES:
            raise ValidationError(f"status no válido: {fields['status']!r}")
        if "stage" in fields and fields["stage"] not in JOB_STAGES:
            raise ValidationError(f"stage no válido: {fields['stage']!r}")
        if not fields:
            return
        sets = ", ".join(f"{name}=?" for name in fields)
        with self.db:
            cur = self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        if cur.rowcount == 0:
            raise KeyError(f"el trabajo {job_id} no existe")

    def pending_jobs(self, *, stages: Iterable[str] = JOB_STAGES, status: str = "pending",
                     notes: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Trabajos por procesar: primero mayor prioridad, después orden de entrada."""
        stages = list(stages)
        sql = (f"SELECT * FROM jobs WHERE status=? AND stage IN ({','.join('?' * len(stages))})")
        params: list[Any] = [status, *stages]
        if notes is not None:
            sql += " AND notes = ?"
            params.append(notes)
        sql += " ORDER BY priority DESC, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.db.execute(sql, params)]

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in JOB_STATUSES:
            raise ValidationError(f"status no válido: {status!r}")
        sql, params = "SELECT * FROM jobs", []  # type: str, list[Any]
        if status:
            sql += " WHERE status=?"
            params.append(status)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY priority DESC, id", params)]

    def job_counts(self) -> dict[str, int]:
        return {r["k"]: r["n"] for r in self.db.execute(
            "SELECT status || '/' || stage AS k, COUNT(*) AS n FROM jobs GROUP BY status, stage")}

    def recover_running_jobs(self) -> int:
        """Tras una caída: lo que quedó `running` vuelve a `pending`."""
        with self.db:
            cur = self.db.execute(
                "UPDATE jobs SET status='pending', started_at=NULL WHERE status='running'")
        return cur.rowcount

    def retry_jobs(self, *, status: str | None = None, job_id: int | None = None) -> int:
        """Devuelve a `pending` los trabajos de un estado (failed/skipped) o uno concreto."""
        where, params = ("id=? AND status != 'running'", [job_id]) if job_id is not None else (
            "status=?", [status])
        with self.db:
            cur = self.db.execute(
                f"""UPDATE jobs SET status='pending', attempts=0, last_error=NULL, notes=NULL,
                    started_at=NULL, finished_at=NULL WHERE {where}""", params)
        return cur.rowcount

    def remove_job(self, job_id: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cur.rowcount > 0

    def clear_done_jobs(self) -> int:
        """Borra de la cola los trabajos terminados. No toca transcripciones ni afirmaciones."""
        with self.db:
            cur = self.db.execute("DELETE FROM jobs WHERE status='done'")
        return cur.rowcount

    # -- consulta (lo que verán los agentes) ---------------------------------

    def search_claims(self, query: str | None = None, *, status: str | None = "verified",
                      domain: str | None = None, entity_id: int | None = None,
                      type: str | None = None, known_at: str | None = None,
                      include_expired: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        """Búsqueda de afirmaciones con las garantías pensadas para agentes.

        - `status='verified'` por defecto (None = todas, solo para depuración).
        - Se ocultan las caducadas salvo `include_expired`.
        - `known_at` (ISO) devuelve lo que se sabía ese día: fecha de la fuente
          <= known_at y caducidad evaluada en ese momento. Sirve para backtesting.
        """
        now = known_at or utc_now()
        where, params = [], []  # type: list[str], list[Any]
        if query:
            sql = ("SELECT c.*, e.canonical_name AS entity_name FROM claims_fts "
                   "JOIN claims c ON c.id = claims_fts.rowid "
                   "LEFT JOIN entities e ON e.id = c.entity_id ")
            where.append("claims_fts MATCH ?")
            params.append(_fts_query(query))
            order = "ORDER BY bm25(claims_fts)"
        else:
            sql = ("SELECT c.*, e.canonical_name AS entity_name FROM claims c "
                   "LEFT JOIN entities e ON e.id = c.entity_id ")
            order = "ORDER BY c.id DESC"
        if status:
            where.append("c.status = ?")
            params.append(status)
        if domain:
            where.append("c.domain = ?")
            params.append(domain)
        if entity_id is not None:
            where.append("c.entity_id = ?")
            params.append(entity_id)
        if type:
            where.append("c.type = ?")
            params.append(type)
        if known_at:
            where.append("COALESCE(c.published_at, c.captured_at) <= ?")
            params.append(known_at)
        if not include_expired:
            where.append("(c.expires_at IS NULL OR c.expires_at > ?)")
            params.append(now)
        sql += "WHERE " + " AND ".join(where) + f" {order} LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self.db.execute(sql, params):
            item = dict(row)
            item["attrs"] = json.loads(item.pop("attrs_json"))
            out.append(item)
        return out

    def implications_for(self, claim_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM implications WHERE claim_id=? ORDER BY id", (claim_id,))]

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                               for t in _STATS_TABLES}
        out["claims_by_status"] = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS n FROM claims GROUP BY status")}
        return out
