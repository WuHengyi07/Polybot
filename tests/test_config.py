"""Config robustness — an accidental inline `# comment` in an env value must not leak
into the value (this caused live 400s: the Open-Meteo model list and webhook URL got the
comment text appended)."""
from config import Config


def test_inline_comments_are_stripped_from_env_values(monkeypatch):
    monkeypatch.setenv("OPENMETEO_MODELS", "gfs025,ecmwf_ifs025   # weather models to pool")
    monkeypatch.setenv("ENTRY_EDGE_THRESHOLD", "0.12  # entry bar")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "   # (optional) discord/slack webhook")
    monkeypatch.setenv("DATA_SOURCE", "polymarket  # intl markets")
    cfg = Config.from_env()
    assert cfg.openmeteo_models == "gfs025,ecmwf_ifs025"   # no comment text in the URL param
    assert cfg.entry_edge_threshold == 0.12                # numeric still parses (not defaulted)
    assert cfg.alert_webhook_url == ""                     # blank, not the comment string
    assert cfg.data_source == "polymarket"


def test_values_without_comments_unchanged(monkeypatch):
    monkeypatch.setenv("OPENMETEO_MODELS", "gfs025")
    monkeypatch.setenv("DATA_SOURCE", "live")
    cfg = Config.from_env()
    assert cfg.openmeteo_models == "gfs025" and cfg.data_source == "live"
