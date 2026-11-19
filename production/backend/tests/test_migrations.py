EXPECTED = {"user", "agent", "phone_number", "campaign", "lead", "batch", "call", "setting", "audit_log", "department",
            "use_case", "dataset", "dataset_lead", "workflow", "scheduled_run", "handoff", "dnc_phone"}


def test_tables(conn):
    with conn.cursor() as cur:
        cur.execute("select table_name from information_schema.tables where table_schema='public'")
        assert {r["table_name"] for r in cur.fetchall()} == EXPECTED


def test_migrate_is_idempotent():
    from backend.scripts import migrate
    assert migrate.main() == 0 and migrate.main() == 0


def test_departments_seeded_with_defaults(conn):
    with conn.cursor() as cur:
        cur.execute("select key, window_start, window_end, line_share, cooling_days from department order by key::text")
        assert [tuple(r.values()) for r in cur.fetchall()] == [("DEMAND", "10:00", "19:00", 5, 7), ("SUPPLY", "10:00", "19:00", 5, 7)]


def test_removed_and_added_columns(conn):
    with conn.cursor() as cur:
        cur.execute("select table_name||'.'||column_name c from information_schema.columns where table_schema='public'")
        cols = {r["c"] for r in cur.fetchall()}
    assert "campaign.skip_cooling" in cols and "call.pushed_to_di_at" in cols
    assert "dataset_lead.dnc" not in cols and "call.pushed_to_crm_at" not in cols and "user.email_verified" not in cols


def test_003_paused_at_and_call_tag_source(conn):
    with conn.cursor() as cur:
        cur.execute("select data_type from information_schema.columns where table_schema='public' "
                    "and table_name='campaign' and column_name='paused_at'")
        assert cur.fetchone()["data_type"] == "timestamp with time zone"
        cur.execute("INSERT INTO dnc_phone (phone, source) VALUES ('+919810000001', 'call_tag')")
    conn.rollback()
