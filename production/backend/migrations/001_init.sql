-- Calling Console schema. Idempotent: safe to re-run.

DO $$ BEGIN CREATE TYPE batch_kind AS ENUM ('FIRST','REDIAL','SEQUENCE','TRIGGER'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE campaign_status AS ENUM ('DRAFT','SCHEDULED','RUNNING','PAUSED','COMPLETED','STOPPED'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE dataset_source AS ENUM ('UPLOAD','CRM','API'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE handoff_status AS ENUM ('OPEN','CONTACTED','VISIT_FIXED','CLOSED_WON','CLOSED_LOST','INVALID'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE lead_status AS ENUM ('PENDING','QUEUED','CONNECTED','NOT_CONNECTED','BLOCKED','VOICEMAIL','FAILED','DONE','DNC'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE outcome AS ENUM ('CONNECTED','NOT_PICKED','CARRIER_BLOCKED','VOICEMAIL','FAILED','BALANCE_LOW','UNKNOWN'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE role AS ENUM ('ADMIN','HEAD','MANAGER','VIEWER'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE run_status AS ENUM ('PLANNED','QUEUED','RUNNING','DONE','CANCELLED'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE trigger_type AS ENUM ('MANUAL','SCHEDULE','ON_NEW_LEAD','AFTER_STEP'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE vertical AS ENUM ('SUPPLY','DEMAND','BOTH'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS "user" (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  name text,
  email text NOT NULL UNIQUE,
  image text,
  role role NOT NULL DEFAULT 'VIEWER',
  vertical vertical NOT NULL DEFAULT 'BOTH',
  use_case_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  title text,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  bolna_agent_id text NOT NULL UNIQUE,
  name text NOT NULL,
  vertical vertical NOT NULL DEFAULT 'SUPPLY',
  description text,
  caller_name text,
  default_from_number text,
  variables jsonb NOT NULL DEFAULT '[]'::jsonb,
  welcome_message text,
  config jsonb,
  last_synced_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phone_number (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  number text NOT NULL UNIQUE,
  provider text,
  label text,
  active boolean NOT NULL DEFAULT true,
  bolna_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS setting (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  user_id text REFERENCES "user"(id),
  action text NOT NULL,
  entity text,
  entity_id text,
  meta jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log (created_at);

CREATE TABLE IF NOT EXISTS department (
  key vertical PRIMARY KEY,
  name text NOT NULL,
  window_start text NOT NULL DEFAULT '10:00',
  window_end text NOT NULL DEFAULT '19:00',
  line_share integer NOT NULL DEFAULT 5,
  cooling_days integer NOT NULL DEFAULT 7,
  settings jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS use_case (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  department vertical NOT NULL,
  key text NOT NULL,
  name text NOT NULL,
  description text,
  default_agent_id text REFERENCES agent(id),
  tag_taxonomy jsonb NOT NULL,
  priority integer NOT NULL DEFAULT 5,
  color text NOT NULL DEFAULT '#12315C',
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS use_case_dept_key_idx ON use_case (department, key);

CREATE TABLE IF NOT EXISTS dataset (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  department vertical NOT NULL,
  use_case_id text REFERENCES use_case(id) ON DELETE SET NULL,
  name text NOT NULL,
  description text,
  source dataset_source NOT NULL DEFAULT 'UPLOAD',
  source_config jsonb,
  columns jsonb NOT NULL DEFAULT '[]'::jsonb,
  row_count integer NOT NULL DEFAULT 0,
  last_synced_at timestamptz,
  created_by_id text REFERENCES "user"(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dataset_dept_idx ON dataset (department);

CREATE TABLE IF NOT EXISTS dataset_lead (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  dataset_id text NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  phone text NOT NULL,
  name text,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  city text,
  society text,
  segment text,
  last_called_at timestamptz,
  last_outcome outcome,
  last_tag text,
  total_attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS dataset_lead_phone_idx ON dataset_lead (dataset_id, phone);
CREATE INDEX IF NOT EXISTS dataset_lead_segment_idx ON dataset_lead (dataset_id, segment);
CREATE INDEX IF NOT EXISTS dataset_lead_phone_only_idx ON dataset_lead (phone);

CREATE TABLE IF NOT EXISTS workflow (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  department vertical NOT NULL,
  use_case_id text NOT NULL REFERENCES use_case(id) ON DELETE CASCADE,
  name text NOT NULL,
  purpose text,
  agent_id text NOT NULL REFERENCES agent(id),
  dataset_id text REFERENCES dataset(id) ON DELETE SET NULL,
  segment jsonb NOT NULL DEFAULT '{}'::jsonb,
  from_number text NOT NULL,
  variable_map jsonb NOT NULL DEFAULT '{}'::jsonb,
  trigger jsonb NOT NULL,
  redial_policy jsonb NOT NULL,
  rules jsonb NOT NULL DEFAULT '[]'::jsonb,
  parent_id text,
  step_order integer NOT NULL DEFAULT 0,
  priority integer NOT NULL DEFAULT 5,
  active boolean NOT NULL DEFAULT true,
  last_run_at timestamptz,
  created_by_id text REFERENCES "user"(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_dept_idx ON workflow (department);
CREATE INDEX IF NOT EXISTS workflow_parent_idx ON workflow (parent_id);

CREATE TABLE IF NOT EXISTS campaign (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  name text NOT NULL,
  agent_id text NOT NULL REFERENCES agent(id),
  vertical vertical NOT NULL DEFAULT 'SUPPLY',
  from_number text NOT NULL,
  status campaign_status NOT NULL DEFAULT 'DRAFT',
  scheduled_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  column_map jsonb NOT NULL DEFAULT '{}'::jsonb,
  redial_policy jsonb NOT NULL,
  notes text,
  use_case_id text,
  workflow_id text,
  dataset_id text,
  purpose text,
  priority integer NOT NULL DEFAULT 5,
  step_order integer NOT NULL DEFAULT 0,
  skip_cooling boolean NOT NULL DEFAULT false,
  created_by_id text REFERENCES "user"(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS campaign_agent_idx ON campaign (agent_id);
CREATE INDEX IF NOT EXISTS campaign_status_idx ON campaign (status);
CREATE INDEX IF NOT EXISTS campaign_usecase_idx ON campaign (use_case_id);

CREATE TABLE IF NOT EXISTS lead (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  campaign_id text NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
  phone text NOT NULL,
  name text,
  variables jsonb NOT NULL DEFAULT '{}'::jsonb,
  status lead_status NOT NULL DEFAULT 'PENDING',
  attempts integer NOT NULL DEFAULT 0,
  last_call_at timestamptz,
  next_dial_at timestamptz,
  last_outcome outcome,
  final_tag text,
  dataset_lead_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS lead_campaign_phone_idx ON lead (campaign_id, phone);
CREATE INDEX IF NOT EXISTS lead_campaign_status_idx ON lead (campaign_id, status);

CREATE TABLE IF NOT EXISTS batch (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  campaign_id text NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
  bolna_batch_id text UNIQUE,
  round integer NOT NULL DEFAULT 1,
  kind batch_kind NOT NULL DEFAULT 'FIRST',
  from_number text NOT NULL,
  scheduled_at timestamptz,
  status text NOT NULL DEFAULT 'created',
  total integer NOT NULL DEFAULT 0,
  dialed integer NOT NULL DEFAULT 0,
  connected integer NOT NULL DEFAULT 0,
  stats jsonb,
  last_synced_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS batch_campaign_idx ON batch (campaign_id);

CREATE TABLE IF NOT EXISTS call (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  bolna_execution_id text NOT NULL UNIQUE,
  agent_id text REFERENCES agent(id),
  campaign_id text REFERENCES campaign(id) ON DELETE SET NULL,
  lead_id text REFERENCES lead(id) ON DELETE SET NULL,
  batch_id text REFERENCES batch(id) ON DELETE SET NULL,
  bolna_batch_id text,
  phone text NOT NULL,
  from_number text,
  status text NOT NULL,
  outcome outcome NOT NULL DEFAULT 'UNKNOWN',
  hangup_code integer,
  hangup_reason text,
  duration_sec integer NOT NULL DEFAULT 0,
  human_turns integer NOT NULL DEFAULT 0,
  cost double precision,
  started_at timestamptz,
  transcript text,
  recording_url text,
  recording_key text,
  extracted jsonb,
  tag text,
  summary text,
  raw jsonb,
  pushed_to_di_at timestamptz,
  rules_applied_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS call_campaign_outcome_idx ON call (campaign_id, outcome);
CREATE INDEX IF NOT EXISTS call_agent_started_idx ON call (agent_id, started_at);
CREATE INDEX IF NOT EXISTS call_tag_idx ON call (tag);
CREATE INDEX IF NOT EXISTS call_phone_idx ON call (phone);

CREATE TABLE IF NOT EXISTS handoff (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  department vertical NOT NULL,
  use_case_id text REFERENCES use_case(id) ON DELETE SET NULL,
  campaign_id text REFERENCES campaign(id) ON DELETE SET NULL,
  call_id text REFERENCES call(id) ON DELETE SET NULL,
  phone text NOT NULL,
  name text,
  tag text,
  summary text,
  details jsonb,
  team text,
  assigned_to_id text REFERENCES "user"(id),
  status handoff_status NOT NULL DEFAULT 'OPEN',
  rm_notes text,
  due_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS handoff_dept_status_idx ON handoff (department, status);

CREATE TABLE IF NOT EXISTS scheduled_run (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  department vertical NOT NULL,
  use_case_id text REFERENCES use_case(id) ON DELETE SET NULL,
  workflow_id text REFERENCES workflow(id) ON DELETE SET NULL,
  campaign_id text REFERENCES campaign(id) ON DELETE CASCADE,
  purpose text,
  priority integer NOT NULL DEFAULT 5,
  requested_start timestamptz NOT NULL,
  planned_start timestamptz,
  planned_end timestamptz,
  est_leads integer NOT NULL DEFAULT 0,
  est_minutes integer NOT NULL DEFAULT 0,
  lines integer NOT NULL DEFAULT 5,
  status run_status NOT NULL DEFAULT 'PLANNED',
  note text,
  created_by_id text REFERENCES "user"(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_dept_start_idx ON scheduled_run (department, planned_start);

CREATE TABLE IF NOT EXISTS dnc_phone (
  phone text PRIMARY KEY,
  reason text,
  source text NOT NULL CHECK (source IN ('use_case_tag','rule','manual','backfill')),
  created_by_id text REFERENCES "user"(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO department (key, name) VALUES ('SUPPLY','Supply'),('DEMAND','Demand') ON CONFLICT DO NOTHING;
