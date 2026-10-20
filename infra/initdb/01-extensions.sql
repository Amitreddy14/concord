-- pgvector backs hybrid retrieval in Phase 4. Created at container init so a
-- fresh volume is usable without a manual step.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
