-- Projects (Open WebUI folders): memory is scoped per project.
-- project_id = '' is the global scope (chats outside any project).
ALTER TABLE memory.chat_summaries ADD COLUMN project_id text NOT NULL DEFAULT '';
ALTER TABLE memory.facts          ADD COLUMN project_id text NOT NULL DEFAULT '';
ALTER TABLE memory.jobs           ADD COLUMN project_id text NOT NULL DEFAULT '';

ALTER TABLE memory.learner_profile ADD COLUMN project_id text NOT NULL DEFAULT '';
ALTER TABLE memory.learner_profile DROP CONSTRAINT learner_profile_pkey;
ALTER TABLE memory.learner_profile ADD PRIMARY KEY (user_id, project_id);

CREATE INDEX chat_summaries_scope ON memory.chat_summaries (user_id, project_id, updated_at DESC);
CREATE INDEX facts_scope          ON memory.facts (user_id, project_id);
