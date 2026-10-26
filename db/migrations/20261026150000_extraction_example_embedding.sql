-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- EMBED_MODEL's vector for the example's text (examples.py), compared with a new document's to
-- find the examples most like it. Null when embedding failed or the example predates it; those
-- fall back to comparing words.
ALTER TABLE extraction_example ADD COLUMN embedding real[];

-- migrate:down
ALTER TABLE extraction_example DROP COLUMN embedding;
