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

from .units import normalize as normalize_metric
from .schema import (
    CLAIM_STATUSES, CLAIM_TYPES, DIRECTIONS, ENTITY_TYPES, EVIDENCE_GRADES,
    FORECAST_RESOLUTIONS, IMPLICATION_BASES, JOB_STAGES, JOB_STATUSES, KNOWLEDGE_FIELDS,
    RELATION_TYPES, SCHEMA_VERSION, STANCES,
    TRANSCRIPT_ORIGINS, Claim, Entity, Implication, ValidationError,
    default_expires_at, normalize_name, utc_now,
)


class AmbiguousEntity(ValueError):
    """El nombre corresponde a más de una entidad; hay que indicar el tipo."""


def _in(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


CLAIMS_TABLE_DDL = f"""
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
    fingerprint TEXT NOT NULL,
    title TEXT,
    mechanism_json TEXT NOT NULL DEFAULT '[]',
    applies_when_json TEXT NOT NULL DEFAULT '[]',
    fails_when_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metric_value_abs REAL,
    metric_unit_base TEXT
);
"""

# Índices, búsqueda de texto y disparadores de `claims` (se recrean al reconstruir la tabla).
CLAIMS_EXTRAS_DDL = """
CREATE UNIQUE INDEX idx_claim_dedupe ON claims(source_id, IFNULL(run_id, 0), fingerprint);
CREATE INDEX idx_claim_entity ON claims(entity_id);
CREATE INDEX idx_claim_status ON claims(status, expires_at);

CREATE VIRTUAL TABLE claims_fts USING fts5(
    title, statement, quote, content='claims', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, title, statement, quote)
    VALUES (new.id, new.title, new.statement, new.quote);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, title, statement, quote)
    VALUES ('delete', old.id, old.title, old.statement, old.quote);
END;
CREATE TRIGGER claims_au AFTER UPDATE OF title, statement, quote ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, title, statement, quote)
    VALUES ('delete', old.id, old.title, old.statement, old.quote);
    INSERT INTO claims_fts(rowid, title, statement, quote)
    VALUES (new.id, new.title, new.statement, new.quote);
END;

"""

RELATIONS_DDL = f"""
CREATE TABLE claim_relations (
    id INTEGER PRIMARY KEY,
    from_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ({_in(RELATION_TYPES)})),
    to_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    basis TEXT NOT NULL CHECK (basis IN ({_in(IMPLICATION_BASES)})),
    created_at TEXT NOT NULL,
    UNIQUE (from_claim_id, relation, to_claim_id),
    CHECK (from_claim_id != to_claim_id)
);
CREATE INDEX idx_relation_to ON claim_relations(to_claim_id);
"""

# Limpieza del catálogo: propuestas de fusión de entidades y registro de las ya hechas.
CATALOG_DDL = """
CREATE TABLE merge_proposals (
    id INTEGER PRIMARY KEY,
    into_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    from_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ('rule', 'model')),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dismissed')),
    created_at TEXT NOT NULL,
    UNIQUE (into_entity_id, from_entity_id),
    CHECK (into_entity_id != from_entity_id)
);
CREATE TABLE entity_merges (
    id INTEGER PRIMARY KEY,
    into_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    from_name TEXT NOT NULL, from_type TEXT NOT NULL,
    claims_moved INTEGER NOT NULL, origin TEXT, reason TEXT,
    created_at TEXT NOT NULL,
    moved_json TEXT
);
"""

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

{CLAIMS_TABLE_DDL}{CLAIMS_EXTRAS_DDL}
CREATE TABLE implications (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    target_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    target_label TEXT,
    direction TEXT NOT NULL CHECK (direction IN ({_in(DIRECTIONS)})),
    mechanism TEXT, horizon TEXT, strength REAL, confidence REAL,
    basis TEXT NOT NULL CHECK (basis IN ({_in(IMPLICATION_BASES)})),
    created_at TEXT NOT NULL,
    conditional_on TEXT,
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
""" + JOBS_DDL + RELATIONS_DDL + CATALOG_DDL

# Pasos desde cada versión antigua hasta la actual.
_MIGRATIONS = {
    1: """
ALTER TABLE extraction_runs ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE;
ALTER TABLE extraction_runs ADD COLUMN transcript_id INTEGER
    REFERENCES transcripts(id) ON DELETE SET NULL;
ALTER TABLE extraction_runs ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}';
""",
    2: JOBS_DDL,
    4: CATALOG_DDL,
}

# Columnas de `claims` anteriores a la v4, para copiar los datos al reconstruir la tabla.
_CLAIM_COLUMNS_V3 = (
    "id, source_id, transcript_id, run_id, entity_id, type, domain, statement, evidence_grade, "
    "stance, metric_name, metric_value, metric_unit, metric_period, currency, as_of, valid_from, "
    "valid_to, horizon, ts_start, ts_end, quote, confidence, attrs_json, status, published_at, "
    "captured_at, expires_at, fingerprint")

_STATS_TABLES = ("sources", "transcripts", "entities", "claims", "implications",
                 "forecasts", "extraction_runs", "jobs", "claim_relations", "entity_merges")
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


def _claim(row: sqlite3.Row) -> dict[str, Any]:
    """Fila de `claims` con sus columnas JSON ya decodificadas."""
    item = dict(row)
    item["attrs"] = json.loads(item.pop("attrs_json"))
    for name in (*KNOWLEDGE_FIELDS, "tags"):
        item[name] = json.loads(item.pop(f"{name}_json"))
    return item


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
            if step == 3:
                self._migrate_claims_v4()
                continue
            if step == 5:
                self._migrate_v6()
                continue
            self.db.executescript(
                f"BEGIN;\n{_MIGRATIONS[step]}\nPRAGMA user_version = {step + 1};\nCOMMIT;")

    def _migrate_v6(self) -> None:
        """v5 -> v6: cifras normalizadas en `claims` y detalle de cada fusión para poder deshacerla."""
        with self.db:
            # Una base que viene de v3 o anterior ya ha recreado estas tablas con la forma actual.
            columns = lambda table: {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if "metric_value_abs" not in columns("claims"):
                self.db.execute("ALTER TABLE claims ADD COLUMN metric_value_abs REAL")
                self.db.execute("ALTER TABLE claims ADD COLUMN metric_unit_base TEXT")
            if "moved_json" not in columns("entity_merges"):
                self.db.execute("ALTER TABLE entity_merges ADD COLUMN moved_json TEXT")
            rows = self.db.execute("SELECT id, metric_value, metric_unit FROM claims "
                                   "WHERE metric_value IS NOT NULL").fetchall()
            self.db.executemany(
                "UPDATE claims SET metric_value_abs=?, metric_unit_base=? WHERE id=?",
                [(*normalize_metric(r["metric_value"], r["metric_unit"]), r["id"]) for r in rows])
            self.db.execute("PRAGMA user_version = 6")

    def _migrate_claims_v4(self) -> None:
        """v3 -> v4: tipos nuevos y campos de conocimiento en `claims`, relaciones entre afirmaciones.

        Cambiar la lista de tipos permitidos exige reconstruir la tabla. Antes se deja una
        copia de seguridad junto a la base, por si algo fallara.
        """
        if self.path != ":memory:":
            backup = sqlite3.connect(f"{self.path}.v3.bak")
            with backup:
                self.db.backup(backup)
            backup.close()
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            self.db.executescript(f"""BEGIN;
                DROP TABLE claims_fts;
                {CLAIMS_TABLE_DDL.replace("CREATE TABLE claims (", "CREATE TABLE claims_new (")}
                INSERT INTO claims_new ({_CLAIM_COLUMNS_V3}) SELECT {_CLAIM_COLUMNS_V3} FROM claims;
                DROP TABLE claims;
                ALTER TABLE claims_new RENAME TO claims;
                {CLAIMS_EXTRAS_DDL}
                INSERT INTO claims_fts(claims_fts) VALUES ('rebuild');
                ALTER TABLE implications ADD COLUMN conditional_on TEXT;
                {RELATIONS_DDL}
                PRAGMA user_version = 4;
                COMMIT;""")
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.db.execute("PRAGMA foreign_keys = ON")
        broken = self.db.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError("La migración a v4 ha dejado referencias rotas; restaura la copia .v3.bak.")

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

    def list_entities(self) -> list[dict[str, Any]]:
        """Todas las entidades con sus alias y cuántas afirmaciones verificadas tienen."""
        aliases: dict[int, list[str]] = {}
        for row in self.db.execute("SELECT entity_id, alias FROM entity_aliases ORDER BY id"):
            aliases.setdefault(row["entity_id"], []).append(row["alias"])
        out = []
        for row in self.db.execute(
                """SELECT e.id, e.type, e.canonical_name, e.norm_name, e.external_ids_json,
                          (SELECT COUNT(*) FROM claims c
                            WHERE c.entity_id = e.id AND c.status = 'verified') AS claims
                   FROM entities e ORDER BY e.id"""):
            item = dict(row)
            item["external_ids"] = json.loads(item.pop("external_ids_json"))
            item["aliases"] = aliases.get(row["id"], [])
            out.append(item)
        return out

    def merge_entities(self, into_id: int, from_id: int, *, origin: str | None = None,
                       reason: str | None = None) -> int:
        """Fusiona `from_id` dentro de `into_id`. Devuelve cuántas afirmaciones cambian de entidad.

        Las afirmaciones e implicaciones pasan a la entidad que queda; el nombre y los alias de la
        absorbida se conservan como alias, así que las próximas extracciones ya la reconocen.
        """
        if into_id == from_id:
            raise ValidationError("no se puede fusionar una entidad consigo misma")
        target, source = self.get_entity(into_id), self.get_entity(from_id)
        if target is None or source is None:
            raise KeyError("alguna de las entidades ya no existe")
        with self.db:
            claim_ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM claims WHERE entity_id=?", (from_id,))]
            implication_ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM implications WHERE target_entity_id=?", (from_id,))]
            moved = self.db.execute("UPDATE claims SET entity_id=? WHERE entity_id=?",
                                    (into_id, from_id)).rowcount
            self.db.execute("UPDATE implications SET target_entity_id=? WHERE target_entity_id=?",
                            (into_id, from_id))
            added_aliases = []
            for alias in (source["canonical_name"], *source["aliases"]):
                norm = normalize_name(alias)
                if norm and norm != target["norm_name"]:
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, norm_alias)"
                        " VALUES (?,?,?)", (into_id, alias.strip(), norm))
                    if cur.rowcount:
                        added_aliases.append(norm)
            detail = {"claims": claim_ids, "implications": implication_ids, "aliases": added_aliases,
                      "source_aliases": source["aliases"], "source_external_ids": source["external_ids"],
                      "target_external_ids": target["external_ids"]}
            merged = {**source["external_ids"], **target["external_ids"]}
            self.db.execute("UPDATE entities SET external_ids_json=? WHERE id=?",
                            (json.dumps(merged, ensure_ascii=False), into_id))
            # Las propuestas que apuntaban a la absorbida pasan a apuntar a la que queda.
            self.db.execute("UPDATE OR IGNORE merge_proposals SET into_entity_id=? "
                            "WHERE into_entity_id=? AND from_entity_id != ?", (into_id, from_id, into_id))
            self.db.execute("UPDATE OR IGNORE merge_proposals SET from_entity_id=? "
                            "WHERE from_entity_id=? AND into_entity_id != ?", (into_id, from_id, into_id))
            self.db.execute(
                """INSERT INTO entity_merges (into_entity_id, from_name, from_type, claims_moved,
                    origin, reason, created_at, moved_json) VALUES (?,?,?,?,?,?,?,?)""",
                (into_id, source["canonical_name"], source["type"], moved, origin, reason, utc_now(),
                 json.dumps(detail, ensure_ascii=False)))
            self.db.execute("DELETE FROM entities WHERE id=?", (from_id,))
        return moved

    def entity_merges(self) -> list[dict[str, Any]]:
        """Fusiones hechas, la más reciente primero. `undoable` si se guardó qué se movió."""
        return [dict(r) for r in self.db.execute(
            """SELECT m.id, m.from_name, m.from_type, m.claims_moved, m.origin, m.reason,
                      m.created_at, m.into_entity_id, e.canonical_name AS into_name,
                      m.moved_json IS NOT NULL AS undoable
               FROM entity_merges m JOIN entities e ON e.id = m.into_entity_id
               ORDER BY m.id DESC""")]

    def undo_merge(self, merge_id: int) -> int:
        """Deshace una fusión: recrea la entidad absorbida y le devuelve lo que era suyo.

        La pareja queda descartada como propuesta, para que no vuelva a sugerirse.
        """
        row = self.db.execute("SELECT * FROM entity_merges WHERE id=?", (merge_id,)).fetchone()
        if row is None:
            raise KeyError("esa fusión no existe")
        if row["moved_json"] is None:
            raise ValidationError("esta fusión es anterior al registro de detalle y no se puede deshacer")
        detail = json.loads(row["moved_json"])
        into_id = row["into_entity_id"]
        with self.db:
            for norm in detail["aliases"]:
                self.db.execute("DELETE FROM entity_aliases WHERE entity_id=? AND norm_alias=?",
                                (into_id, norm))
            self.db.execute("UPDATE entities SET external_ids_json=? WHERE id=?",
                            (json.dumps(detail["target_external_ids"], ensure_ascii=False), into_id))
        restored = self.upsert_entity(row["from_type"], row["from_name"], detail["source_aliases"],
                                      detail["source_external_ids"])
        with self.db:
            marks = ",".join("?" * len(detail["claims"])) or "NULL"
            moved = self.db.execute(
                f"UPDATE claims SET entity_id=? WHERE entity_id=? AND id IN ({marks})",
                (restored, into_id, *detail["claims"])).rowcount
            marks = ",".join("?" * len(detail["implications"])) or "NULL"
            self.db.execute(
                f"UPDATE implications SET target_entity_id=? WHERE target_entity_id=? AND id IN ({marks})",
                (restored, into_id, *detail["implications"]))
            self.db.execute("DELETE FROM entity_merges WHERE id=?", (merge_id,))
        if self.add_merge_proposal(into_id, restored, row["origin"] or "rule", row["reason"]):
            pending = [p["id"] for p in self.merge_proposals()
                       if {p["into_entity_id"], p["from_entity_id"]} == {into_id, restored}]
            self.dismiss_merge_proposals(pending)
        return moved

    def add_merge_proposal(self, into_id: int, from_id: int, origin: str,
                           reason: str | None = None) -> bool:
        """Propone fusionar dos entidades. False si ya estaba propuesta (o descartada) en algún sentido."""
        if into_id == from_id:
            return False
        exists = self.db.execute(
            """SELECT 1 FROM merge_proposals WHERE (into_entity_id=? AND from_entity_id=?)
               OR (into_entity_id=? AND from_entity_id=?)""", (into_id, from_id, from_id, into_id)).fetchone()
        if exists:
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO merge_proposals (into_entity_id, from_entity_id, origin, reason, created_at)"
                " VALUES (?,?,?,?,?)", (into_id, from_id, origin, reason, utc_now()))
        return True

    def merge_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            """SELECT p.id, p.origin, p.reason, p.into_entity_id, p.from_entity_id,
                      a.canonical_name AS into_name, a.type AS into_type,
                      b.canonical_name AS from_name, b.type AS from_type,
                      (SELECT COUNT(*) FROM claims c WHERE c.entity_id=a.id AND c.status='verified') AS into_claims,
                      (SELECT COUNT(*) FROM claims c WHERE c.entity_id=b.id AND c.status='verified') AS from_claims
               FROM merge_proposals p JOIN entities a ON a.id=p.into_entity_id
               JOIN entities b ON b.id=p.from_entity_id
               WHERE p.status=? ORDER BY into_claims + from_claims DESC, p.id""", (status,))]

    def dismiss_merge_proposals(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        with self.db:
            cur = self.db.execute(
                f"UPDATE merge_proposals SET status='dismissed' WHERE id IN ({','.join('?' * len(ids))})", ids)
        return cur.rowcount

    def backup(self, suffix: str) -> str | None:
        """Copia de seguridad junto a la base (`rezme.db.<suffix>.bak`). None si es en memoria."""
        if self.path == ":memory:":
            return None
        target = f"{self.path}.{suffix}.bak"
        copy = sqlite3.connect(target)
        with copy:
            self.db.backup(copy)
        copy.close()
        return target

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
                    expires_at, fingerprint, title, mechanism_json, applies_when_json,
                    fails_when_json, tags_json, metric_value_abs, metric_unit_base)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (claim.source_id, claim.transcript_id, claim.run_id, claim.entity_id, claim.type,
                 claim.domain, claim.statement.strip(), claim.evidence_grade, claim.stance,
                 claim.metric_name, claim.metric_value, claim.metric_unit, claim.metric_period,
                 claim.currency, claim.as_of, claim.valid_from, claim.valid_to, claim.horizon,
                 claim.ts_start, claim.ts_end, claim.quote, claim.confidence,
                 json.dumps(claim.attrs, ensure_ascii=False), claim.status, published, captured,
                 expires, fingerprint, claim.title,
                 *(json.dumps(getattr(claim, name), ensure_ascii=False)
                   for name in (*KNOWLEDGE_FIELDS, "tags")),
                 *normalize_metric(claim.metric_value, claim.metric_unit)))
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
        return [_claim(row) for row in self.db.execute(
            sql + " ORDER BY c.ts_start IS NULL, c.ts_start, c.id", params)]

    def add_implication(self, implication: Implication) -> int:
        implication.validate()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO implications (claim_id, target_entity_id, target_label, direction,
                    mechanism, horizon, strength, confidence, basis, created_at, conditional_on)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (implication.claim_id, implication.target_entity_id, implication.target_label,
                 implication.direction, implication.mechanism, implication.horizon,
                 implication.strength, implication.confidence, implication.basis, utc_now(),
                 implication.conditional_on))
        return cur.lastrowid

    def add_relation(self, from_claim_id: int, relation: str, to_claim_id: int,
                     basis: str = "inferred_by_system") -> bool:
        """Relaciona dos afirmaciones (apoya, contradice, matiza…). False si ya existía."""
        if relation not in RELATION_TYPES:
            raise ValidationError(f"relation no válida: {relation!r}")
        if basis not in IMPLICATION_BASES:
            raise ValidationError(f"basis no válido: {basis!r}")
        if from_claim_id == to_claim_id:
            raise ValidationError("una afirmación no puede relacionarse consigo misma")
        with self.db:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO claim_relations (from_claim_id, relation, to_claim_id, basis,
                    created_at) VALUES (?,?,?,?,?)""",
                (from_claim_id, relation, to_claim_id, basis, utc_now()))
        return cur.rowcount > 0

    def relations_for(self, claim_id: int) -> list[dict[str, Any]]:
        """Relaciones en las que participa la afirmación, con el enunciado de la otra."""
        return [dict(r) for r in self.db.execute(
            """SELECT r.id, r.relation, r.basis, r.from_claim_id, r.to_claim_id,
                      CASE WHEN r.from_claim_id = ? THEN 'out' ELSE 'in' END AS direction,
                      c.id AS other_id, c.statement AS other_statement, c.status AS other_status
               FROM claim_relations r
               JOIN claims c ON c.id = CASE WHEN r.from_claim_id = ? THEN r.to_claim_id
                                            ELSE r.from_claim_id END
               WHERE r.from_claim_id = ? OR r.to_claim_id = ? ORDER BY r.id""",
            (claim_id, claim_id, claim_id, claim_id))]

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
        return [_claim(row) for row in self.db.execute(sql, params)]

    def implications_for(self, claim_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM implications WHERE claim_id=? ORDER BY id", (claim_id,))]

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                               for t in _STATS_TABLES}
        out["claims_by_status"] = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS n FROM claims GROUP BY status")}
        return out
