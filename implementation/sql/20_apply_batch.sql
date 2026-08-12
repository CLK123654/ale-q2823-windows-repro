CREATE OR REPLACE FUNCTION ops.apply_batch(p_batch text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO ops.apply_decision(event_id,batch_id,product_id,decision,winner_event_id,decided_against_lsn,decided_against_seq,decided_against_event_id)
  WITH ranked AS (
    SELECT r.*,
           row_number() OVER(PARTITION BY product_id ORDER BY source_lsn DESC,event_seq DESC,event_id DESC) AS rn,
           first_value(event_id) OVER(PARTITION BY product_id ORDER BY source_lsn DESC,event_seq DESC,event_id DESC) AS winner_event_id
    FROM ops.cdc_raw r
    WHERE batch_id=p_batch
  )
  SELECT r.event_id,r.batch_id,r.product_id,
         CASE WHEN r.rn>1 THEN 'SUPERSEDED_IN_BATCH'
              WHEN t.product_id IS NULL THEN 'APPLY'
              WHEN (r.source_lsn,r.event_seq,r.event_id) > (t.last_lsn,t.last_seq,t.last_event_id) THEN 'APPLY'
              ELSE 'STALE' END,
         r.winner_event_id,t.last_lsn,t.last_seq,t.last_event_id
  FROM ranked r
  LEFT JOIN core.catalog_state t USING(product_id)
  ON CONFLICT(event_id) DO NOTHING;

  MERGE INTO core.catalog_state AS t
  USING (
    SELECT r.*
    FROM ops.cdc_raw r
    JOIN ops.apply_decision d USING(event_id)
    WHERE d.batch_id=p_batch AND d.decision='APPLY'
  ) AS s
  ON t.product_id=s.product_id
  WHEN MATCHED AND (s.source_lsn,s.event_seq,s.event_id) > (t.last_lsn,t.last_seq,t.last_event_id)
    THEN UPDATE SET
      name=CASE WHEN s.op='D' THEN NULL ELSE s.name END,
      price=CASE WHEN s.op='D' THEN NULL ELSE s.price END,
      is_deleted=(s.op='D'),
      last_lsn=s.source_lsn,last_seq=s.event_seq,last_event_id=s.event_id
  WHEN NOT MATCHED
    THEN INSERT(product_id,name,price,is_deleted,last_lsn,last_seq,last_event_id)
         VALUES(s.product_id,CASE WHEN s.op='D' THEN NULL ELSE s.name END,
                CASE WHEN s.op='D' THEN NULL ELSE s.price END,s.op='D',
                s.source_lsn,s.event_seq,s.event_id);

  INSERT INTO ops.batch_receipt
  SELECT p_batch,
         count(*),
         count(*) FILTER(WHERE decision='APPLY'),
         count(*) FILTER(WHERE decision='STALE'),
         count(*) FILTER(WHERE decision='SUPERSEDED_IN_BATCH'),
         (SELECT count(*) FROM core.catalog_state),
         (SELECT count(*) FROM core.catalog_state WHERE NOT is_deleted),
         (SELECT count(*) FROM core.catalog_state WHERE is_deleted)
  FROM ops.apply_decision WHERE batch_id=p_batch
  ON CONFLICT(batch_id) DO UPDATE SET
    raw_events=EXCLUDED.raw_events,applied=EXCLUDED.applied,stale=EXCLUDED.stale,
    superseded=EXCLUDED.superseded,state_rows=EXCLUDED.state_rows,
    active_rows=EXCLUDED.active_rows,deleted_rows=EXCLUDED.deleted_rows;
END $$;
