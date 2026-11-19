-- One hand-off per phone per campaign: create_handoff inserts with ON CONFLICT DO NOTHING. Idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS handoff_phone_campaign_uq ON handoff (phone, campaign_id) WHERE campaign_id IS NOT NULL;
