-- Esquema v1 de reZme (hito M0), tal como lo creaba Store. Solo para tests de migración.
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
    origin TEXT NOT NULL CHECK (origin IN ('subtitles_manual', 'subtitles_auto', 'whisper', 'pasted')),
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
    created_at TEXT NOT NULL
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('company', 'security', 'crypto_asset', 'commodity', 'currency', 'index', 'country', 'central_bank', 'macro_indicator', 'sector', 'technology', 'drug', 'disease', 'biological_concept', 'person', 'organization', 'regulation', 'event', 'concept')),
    canonical_name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    external_ids_json TEXT NOT NULL DEFAULT '{}',
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
    type TEXT NOT NULL CHECK (type IN ('fact', 'statistic', 'study_result', 'causal_claim', 'forecast', 'opinion', 'own_calculation', 'recommendation', 'risk', 'catalyst', 'methodology', 'definition')),
    domain TEXT,
    statement TEXT NOT NULL,
    evidence_grade TEXT NOT NULL CHECK (evidence_grade IN ('primary_data', 'peer_reviewed', 'official_stat', 'cited_secondary', 'own_analysis', 'expert_opinion', 'anecdote', 'none')),
    stance TEXT NOT NULL CHECK (stance IN ('bullish', 'bearish', 'neutral', 'n/a')),
    metric_name TEXT, metric_value REAL, metric_unit TEXT,
    metric_period TEXT, currency TEXT,
    as_of TEXT, valid_from TEXT, valid_to TEXT, horizon TEXT,
    ts_start REAL, ts_end REAL, quote TEXT, confidence REAL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('candidate', 'verified', 'ungrounded', 'rejected', 'superseded')),
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
    direction TEXT NOT NULL CHECK (direction IN ('positive', 'negative', 'mixed', 'unclear')),
    mechanism TEXT, horizon TEXT, strength REAL, confidence REAL,
    basis TEXT NOT NULL CHECK (basis IN ('stated_by_source', 'inferred_by_system')),
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
        CHECK (resolution IN ('pending', 'correct', 'incorrect', 'partial', 'void')),
    resolved_at TEXT, notes TEXT
);

CREATE TABLE source_profiles (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE (platform, channel_id)
);
PRAGMA user_version = 1;
