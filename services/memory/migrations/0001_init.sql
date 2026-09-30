CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS memory;

CREATE TABLE memory.schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE memory.chat_summaries (
  chat_id         text PRIMARY KEY,
  user_id         text        NOT NULL,
  title           text,
  summary         text        NOT NULL CHECK (char_length(summary) <= 600),
  key_points      jsonb       NOT NULL DEFAULT '[]',
  topics          text[]      NOT NULL DEFAULT '{}',
  weak_spots      jsonb       NOT NULL DEFAULT '[]',
  mastered        jsonb       NOT NULL DEFAULT '[]',
  open_questions  jsonb       NOT NULL DEFAULT '[]',
  message_count   int         NOT NULL DEFAULT 0,
  last_message_id text,
  embedding       vector(1024),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_summaries_user_recent ON memory.chat_summaries (user_id, updated_at DESC);
CREATE INDEX chat_summaries_embedding   ON memory.chat_summaries USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory.facts (
  id         bigserial PRIMARY KEY,
  user_id    text        NOT NULL,
  text       text        NOT NULL CHECK (char_length(text) <= 300),
  embedding  vector(1024),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX facts_user      ON memory.facts (user_id);
CREATE INDEX facts_embedding ON memory.facts USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory.learner_profile (
  user_id    text PRIMARY KEY,
  weak_spots jsonb NOT NULL DEFAULT '[]',
  mastered   jsonb NOT NULL DEFAULT '[]',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory.jobs (
  chat_id          text PRIMARY KEY,
  user_id          text        NOT NULL,
  title            text,
  payload          jsonb       NOT NULL,
  status           text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','dead')),
  first_pending_at timestamptz NOT NULL DEFAULT now(),
  run_after        timestamptz NOT NULL,
  attempts         int         NOT NULL DEFAULT 0,
  last_error       text,
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_due ON memory.jobs (status, run_after);
