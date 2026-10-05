from trendlab.security.redaction import REDACTED, redact_structure, redact_text


def test_redacts_assignments_and_known_shapes(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    text = (
        "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz1234 && "
        "curl -H 'Authorization: Bearer eyJhbGciOi.payload.sig' https://x && "
        "echo supersecretvalue123 && password: hunter22 && token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
    )
    out = redact_text(text)
    assert "sk-abc" not in out and "eyJhbGci" not in out and "supersecretvalue123" not in out
    assert "hunter22" not in out and "ghp_" not in out
    assert out.count(REDACTED) >= 4
    assert "curl -H" in out  # non-secret structure preserved


def test_structure_redaction_drops_secret_keys():
    data = {
        "command": "pip install x",
        "api_key": "abc",
        "nested": {"token": "t", "ok": "fine"},
        "list": ["password=zzz"],
    }
    out = redact_structure(data)
    assert out["command"] == "pip install x"
    assert out["api_key"] == REDACTED and out["nested"]["token"] == REDACTED
    assert out["nested"]["ok"] == "fine" and REDACTED in out["list"][0]


def test_plain_text_untouched():
    assert redact_text("pip install pandas-ta") == "pip install pandas-ta"


def test_token_counters_are_not_treated_as_secrets(monkeypatch):
    monkeypatch.setenv(
        "INPUT_TOKENS_LIMIT", "123456789"
    )  # name contains TOKENS but is not a secret
    data = {
        "input_tokens": 1500,
        "est_tokens": 42,
        "cached_input_tokens": 7,
        "token_budget": 9,
        "decision_token": "abc",
        "api_key": "k",
        "GITHUB_TOKEN": "t",
        "db_password": "p",
        "tool_calls": 2,
        "max_tokens": 16000,
    }
    out = redact_structure(data)
    assert (
        out["input_tokens"] == 1500 and out["est_tokens"] == 42 and out["cached_input_tokens"] == 7
    )
    assert out["token_budget"] == 9 and out["tool_calls"] == 2 and out["max_tokens"] == 16000
    assert out["decision_token"] == REDACTED and out["api_key"] == REDACTED
    assert out["GITHUB_TOKEN"] == REDACTED and out["db_password"] == REDACTED
    assert "123456789" in redact_text(
        "count 123456789"
    )  # env var with a counter name is not a secret
