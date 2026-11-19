-- Final-review fixes. Idempotent: safe to re-run.

-- When the campaign was last paused: resume waits until its agent's executions have synced after this.
ALTER TABLE campaign ADD COLUMN IF NOT EXISTS paused_at timestamptz;
-- A campaign paused before this column existed: its last write is the pause or later (a later time only makes
-- resume wait for one more sync).
UPDATE campaign SET paused_at = updated_at WHERE status = 'PAUSED' AND paused_at IS NULL;

-- dnc_phone.source gains 'call_tag': a DNC tag on any finished call (ad-hoc campaign, test call, no campaign).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'dnc_phone'::regclass
                 AND conname = 'dnc_phone_source_check' AND pg_get_constraintdef(oid) LIKE '%call_tag%') THEN
    ALTER TABLE dnc_phone DROP CONSTRAINT IF EXISTS dnc_phone_source_check;
    ALTER TABLE dnc_phone ADD CONSTRAINT dnc_phone_source_check
      CHECK (source IN ('use_case_tag', 'rule', 'manual', 'backfill', 'call_tag'));
  END IF;
END $$;
