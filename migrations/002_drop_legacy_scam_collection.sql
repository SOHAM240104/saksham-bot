-- Remove legacy PGVector collection named ``scam`` after migrating scam content to
-- ``scam_kb`` (or whatever SCAM_VECTOR_COLLECTION is set to).

DELETE FROM langchain_pg_embedding
WHERE collection_id IN (
    SELECT uuid FROM langchain_pg_collection WHERE name = 'scam'
);

DELETE FROM langchain_pg_collection WHERE name = 'scam';
