import pytest

from backend.services import bolna
from backend.services.bolna import BolnaError, agent_template_variables, build_batch_csv, update_agent


class Resp:
    def __init__(self, status=200, body="{}", js=True):
        self.status_code, self.text, self._js = status, body, js

    def json(self):
        if not self._js:
            raise ValueError("no json")
        return {"ok": True}


def test_build_batch_csv_quotes_and_keeps_hindi():
    out = build_batch_csv([{"phone": "+919810043058", "variables": {"owner_name": "राम, जी"}}], ["owner_name"])
    assert out == 'contact_number,owner_name\n+919810043058,"राम, जी"'


def test_update_agent_strips_readonly_and_sets_prompt(monkeypatch):
    seen = {}

    def fake(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return Resp()

    monkeypatch.setattr(bolna.requests, "request", fake)
    agent = {"agent_id": "a1", "created_at": "x", "agent_name": "N",
             "tasks": [{"tools_config": {"llm_agent": {"llm_config": {"prompt": "old"}}}}]}
    update_agent("a1", agent, "NEW")
    body = seen["json"]
    assert seen["method"] == "PUT" and seen["url"].endswith("/v2/agent/a1")
    assert "agent_id" not in body["agent_config"] and "created_at" not in body["agent_config"]
    assert body["agent_prompts"]["task_1"]["system_prompt"] == "NEW"
    assert body["agent_config"]["tasks"][0]["tools_config"]["llm_agent"]["llm_config"]["prompt"] == "NEW"
    assert agent["agent_id"] == "a1"  # input not mutated


def test_non_json_success_raises(monkeypatch):
    monkeypatch.setattr(bolna.requests, "request", lambda *a, **k: Resp(200, "<html>", js=False))
    with pytest.raises(BolnaError):
        bolna.get_batch("b1")


def test_non_2xx_raises(monkeypatch):
    monkeypatch.setattr(bolna.requests, "request", lambda *a, **k: Resp(500, "boom"))
    with pytest.raises(BolnaError) as e:
        bolna.list_agents()
    assert e.value.status == 500


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(bolna.config, "BOLNA_API_KEY", "")
    with pytest.raises(RuntimeError):
        bolna.list_agents()


def test_template_variables():
    a = {"agent_welcome_message": "Hi {{ owner_name }}",
         "agent_prompts": {"task_1": {"system_prompt": "{{society_name}} and {{owner_name}}"}}}
    assert agent_template_variables(a) == ["owner_name", "society_name"]


def test_stub_patches_module_attributes(bolna_stub):
    bolna_stub.executions["a"] = [[{"id": "1"}], [{"id": "2"}]]
    assert bolna.list_agent_executions("a", 1, 50) == ([{"id": "1"}], True)
    assert bolna.list_agent_executions("a", 2, 50) == ([{"id": "2"}], False)
    assert bolna.list_agent_executions("zz", 1, 50) == ([], False)
    assert bolna.create_batch("a", "+91", "csv", "f.csv") == {"batch_id": "bb1"}
    bolna_stub.fail_agents.add("a")
    with pytest.raises(BolnaError):
        bolna.list_agent_executions("a")
    assert bolna_stub.calls[0][0] == "list_agent_executions"
