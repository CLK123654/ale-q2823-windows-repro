CREATE SCHEMA core;
CREATE SCHEMA ops;

CREATE TABLE core.catalog_state(
  product_id text PRIMARY KEY,
  name text,
  price numeric(10,2),
  is_deleted boolean NOT NULL,
  last_lsn bigint NOT NULL,
  last_seq integer NOT NULL,
  last_event_id text NOT NULL,
  CHECK ((is_deleted AND name IS NULL AND price IS NULL) OR
         (NOT is_deleted AND name IS NOT NULL AND price IS NOT NULL AND price >= 0))
);

CREATE TABLE ops.cdc_raw(
  event_id text PRIMARY KEY,
  batch_id text NOT NULL CHECK(batch_id IN ('B1','B2','B3')),
  product_id text NOT NULL,
  op text NOT NULL CHECK(op IN ('I','U','D')),
  source_lsn bigint NOT NULL,
  event_seq integer NOT NULL CHECK(event_seq >= 0),
  name text,
  price numeric(10,2),
  CHECK ((op='D' AND name IS NULL AND price IS NULL) OR
         (op IN ('I','U') AND name IS NOT NULL AND price IS NOT NULL AND price >= 0))
);

CREATE TABLE ops.apply_decision(
  event_id text PRIMARY KEY REFERENCES ops.cdc_raw(event_id),
  batch_id text NOT NULL,
  product_id text NOT NULL,
  decision text NOT NULL CHECK(decision IN ('APPLY','STALE','SUPERSEDED_IN_BATCH')),
  winner_event_id text NOT NULL,
  decided_against_lsn bigint,
  decided_against_seq integer,
  decided_against_event_id text,
  CHECK ((decided_against_lsn IS NULL AND decided_against_seq IS NULL AND decided_against_event_id IS NULL) OR
         (decided_against_lsn IS NOT NULL AND decided_against_seq IS NOT NULL AND decided_against_event_id IS NOT NULL))
);

CREATE TABLE ops.batch_receipt(
  batch_id text PRIMARY KEY,
  raw_events integer NOT NULL,
  applied integer NOT NULL,
  stale integer NOT NULL,
  superseded integer NOT NULL,
  state_rows integer NOT NULL,
  active_rows integer NOT NULL,
  deleted_rows integer NOT NULL
);
