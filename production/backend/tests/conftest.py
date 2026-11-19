import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://localhost/calling_console_test"
)
os.environ["JWT_SECRET"] = "test-secret"
os.environ["FRONTEND_ORIGIN"] = "http://localhost:5173"
os.environ["BOLNA_API_KEY"] = "test"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = ""
os.environ["ALLOWED_EMAIL_DOMAIN"] = "openhouse.in"
os.environ["BOLNA_BASE_URL"] = "https://api.bolna.ai"
os.environ["LOG_LEVEL"] = "INFO"
# backend/.env holds PRODUCTION settings and config.py loads it: blank every key it has (load_dotenv never overrides
# a set variable), so a test can never reach real DI, R2, Interakt or Neon. Tests that need one monkeypatch config.
from dotenv import dotenv_values  # noqa: E402
for _k in dotenv_values(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")):
    os.environ.setdefault(_k, "")

import pytest  # noqa: E402



def pytest_configure(config):
    # botocore still calls datetime.utcnow(); silence only that library's warning.
    config.addinivalue_line("filterwarnings", "ignore::DeprecationWarning:botocore")

from backend.app import create_app  # noqa: E402


@pytest.fixture
def client():
    return create_app().test_client()


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    from backend.scripts import migrate
    assert migrate.main() == 0


def _reset(conn):
    """Truncate every table except department, then restore department defaults."""
    with conn.cursor() as cur:
        cur.execute("select tablename from pg_tables where schemaname='public' and tablename <> 'department'")
        tables = ", ".join(f'"{r["tablename"]}"' for r in cur.fetchall())
        cur.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM department WHERE key = 'BOTH'")
        cur.execute(
            "INSERT INTO department (key, name) VALUES ('SUPPLY','Supply'),('DEMAND','Demand') "
            "ON CONFLICT (key) DO UPDATE SET window_start='10:00', window_end='19:00', "
            "line_share=5, cooling_days=7, settings=NULL"
        )
    conn.commit()


@pytest.fixture
def conn():
    from backend.db import get_conn
    c = get_conn()
    _reset(c)
    try:
        yield c
    finally:
        c.rollback()
        _reset(c)
        c.close()


class BolnaStub:
    """Replaces the network functions of backend.services.bolna."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.executions: dict[str, list[list[dict]]] = {}
        self.agents: list[dict] = []
        self.fail_on: set[str] = set()
        self.fail_agents: set[str] = set()
        self.batches: dict[str, dict] = {}
        self._n = {"batch": 0, "call": 0}

    def _rec(self, name, *args):
        from backend.services.bolna import BolnaError
        self.calls.append((name, args))
        if name in self.fail_on:
            raise BolnaError(500, "<html>")

    def list_agents(self):
        self._rec("list_agents")
        return self.agents

    def get_agent(self, agent_id):
        from backend.services.bolna import BolnaError
        self._rec("get_agent", agent_id)
        for a in self.agents:
            if agent_id in (a.get("agent_id"), a.get("id")):
                return a
        raise BolnaError(404, "not found")

    def update_agent(self, agent_id, agent, system_prompt=None):
        self._rec("update_agent", agent_id, agent, system_prompt)
        return {"agent_id": agent_id, "state": "updated"}

    def list_agent_executions(self, agent_id, page=1, page_size=50):
        from backend.services.bolna import BolnaError
        self._rec("list_agent_executions", agent_id, page, page_size)
        if agent_id in self.fail_agents:
            raise BolnaError(502, "<html>")
        pages = self.executions.get(agent_id, [])
        if page < 1 or page > len(pages):
            return [], False
        return pages[page - 1], page < len(pages)

    def get_batch(self, batch_id):
        self._rec("get_batch", batch_id)
        return self.batches.get(batch_id, {"batch_id": batch_id, "status": "completed"})

    def create_batch(self, agent_id, from_number, csv_text, filename="contacts.csv"):
        self._rec("create_batch", agent_id, from_number, csv_text, filename)
        self._n["batch"] += 1
        return {"batch_id": f"bb{self._n['batch']}"}

    def schedule_batch(self, batch_id, when):
        self._rec("schedule_batch", batch_id, when)
        return {"state": "scheduled"}

    def stop_batch(self, batch_id):
        self._rec("stop_batch", batch_id)
        return {"state": "stopped"}

    def place_call(self, agent_id, to, from_number, user_data):
        self._rec("place_call", agent_id, to, from_number, user_data)
        self._n["call"] += 1
        return {"status": "queued", "execution_id": f"ex{self._n['call']}"}

    def list_phone_numbers(self):
        self._rec("list_phone_numbers")
        return []


@pytest.fixture
def bolna_stub(monkeypatch):
    from backend.services import bolna
    stub = BolnaStub()
    for name in ("list_agents", "get_agent", "update_agent", "list_agent_executions", "get_batch",
                 "create_batch", "schedule_batch", "stop_batch", "place_call", "list_phone_numbers"):
        monkeypatch.setattr(bolna, name, getattr(stub, name))
    return stub


def auth_headers(user) -> dict:
    from backend.api.auth import issue_jwt
    return {"Authorization": f"Bearer {issue_jwt(user)}"}
