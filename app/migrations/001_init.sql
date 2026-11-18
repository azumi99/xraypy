-- 001_init.sql — schema `cv`
-- {embedding_dim} is substituted by the migration runner from config.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS cv;

CREATE TABLE IF NOT EXISTS cv.parcel_identity (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    track_id       INTEGER NOT NULL,
    idx            INTEGER,                -- NULL until status=NEW
    status         TEXT NOT NULL CHECK (status IN ('NEW', 'REVIEW', 'DUPLICATE')),
    duplicate_of   BIGINT REFERENCES cv.parcel_identity(id),
    sim_score      DOUBLE PRECISION,
    review_reason  TEXT,                   -- EMBEDDING_FAILED | QUEUE_FULL | STORE_UNAVAILABLE | GRAY_ZONE | CROP_INVALID
    model_name     TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    orientation    SMALLINT NOT NULL DEFAULT 0,  -- 0=as seen, 1=180deg, 2=flip
    area           DOUBLE PRECISION,
    aspect_ratio   DOUBLE PRECISION,
    hu_moments     JSONB,
    crop_ref       TEXT,
    embedding      vector({embedding_dim}),  -- canonical (orientation 0); NULL when embedding failed
    awb_id         BIGINT,                 -- set when paired (same tx as idx)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, track_id)
);

CREATE TABLE IF NOT EXISTS cv.parcel_embedding (
    id             BIGSERIAL PRIMARY KEY,
    identity_id    BIGINT NOT NULL REFERENCES cv.parcel_identity(id) ON DELETE CASCADE,
    model_name     TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    orientation    SMALLINT NOT NULL DEFAULT 0,
    embedding      vector({embedding_dim}),  -- NULL when embedding failed (REVIEW)
    UNIQUE (identity_id, orientation)
);

CREATE TABLE IF NOT EXISTS cv.review_audit (
    id             BIGSERIAL PRIMARY KEY,
    identity_id    BIGINT NOT NULL REFERENCES cv.parcel_identity(id),
    action         TEXT NOT NULL,          -- NOT_DUPLICATE | CONFIRM_DUPLICATE | RESCAN_INTENTIONAL | AUTO_*
    operator       TEXT NOT NULL DEFAULT 'system',
    reason         TEXT,
    sim_score      DOUBLE PRECISION,
    model_version  TEXT,
    at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cv.awb (
    id             BIGSERIAL PRIMARY KEY,
    awb_number     TEXT NOT NULL,
    batch_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'WAITING' CHECK (status IN ('WAITING', 'PAIRED', 'CANCELLED')),
    parcel_id      BIGINT REFERENCES cv.parcel_identity(id),
    paired_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (awb_number)
);

CREATE TABLE IF NOT EXISTS cv.session (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       TEXT NOT NULL UNIQUE,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_parcel_identity_batch ON cv.parcel_identity (batch_id);
CREATE INDEX IF NOT EXISTS ix_parcel_identity_created ON cv.parcel_identity (created_at);
CREATE INDEX IF NOT EXISTS ix_parcel_identity_model ON cv.parcel_identity (model_name);
CREATE INDEX IF NOT EXISTS ix_parcel_identity_status ON cv.parcel_identity (status);
CREATE INDEX IF NOT EXISTS ix_parcel_embedding_model ON cv.parcel_embedding (model_name);
CREATE INDEX IF NOT EXISTS ix_awb_status ON cv.awb (status);

-- HNSW index for cosine search over the canonical embedding column.
CREATE INDEX IF NOT EXISTS ix_parcel_identity_embedding_hnsw
    ON cv.parcel_identity USING hnsw (embedding vector_cosine_ops)
    WITH (m = {hnsw_m}, ef_construction = {hnsw_ef});
