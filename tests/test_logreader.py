"""Tests for the unified-logger read client (#148)."""

import io
import json
import urllib.error
import urllib.request

import pytest

from agent_hq import logreader
from agent_hq.logreader import LogPage, LogReadError, LogRow, fetch_logs, is_configured


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_token(tmp_path, monkeypatch):
    token = tmp_path / "logger-admin.token"
    token.write_text("tok-abc\n")
    monkeypatch.setattr(logreader, "_DEFAULT_ADMIN_TOKEN_FILE", token)
    return token


def _capture_fetch(monkeypatch, payload):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


PAYLOAD = {
    "logs": [
        {
            "id": 42,
            "app": "backup",
            "level": "error",
            "message": "boom",
            "ts": "2026-08-19T01:00:00Z",
            "source": "mac",
            "json_extra": '{"run_date": "2026-08-19"}',
        },
        {
            "id": 41,
            "app": "agent-hq",
            "level": "alert",
            "message": "odd",
            "ts": "2026-08-19T00:59:00Z",
            "source": None,
            "json_extra": None,
        },
    ],
    "count": 2,
    "max_id": 42,
}


def test_fetch_logs_builds_query_and_parses(tmp_path, monkeypatch):
    _install_token(tmp_path, monkeypatch)
    captured = _capture_fetch(monkeypatch, PAYLOAD)
    page = fetch_logs(levels=("error", "alert"), since="24h", limit=500)
    assert "level=error%2Calert" in captured["url"]
    assert "since=24h" in captured["url"]
    assert "limit=500" in captured["url"]
    assert "app=" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer tok-abc"
    assert captured["headers"]["User-agent"] == "agent-hq-logreader/1.0"
    assert isinstance(page, LogPage)
    assert page.count == 2 and page.max_id == 42
    row = page.rows[0]
    assert isinstance(row, LogRow)
    assert row.json_extra == {"run_date": "2026-08-19"}  # parsed from JSON text
    assert page.rows[1].json_extra is None


def test_fetch_logs_app_since_id_order(tmp_path, monkeypatch):
    _install_token(tmp_path, monkeypatch)
    captured = _capture_fetch(monkeypatch, {"logs": [], "count": 0, "max_id": None})
    fetch_logs(app="backup", since_id=41, order="asc")
    assert "app=backup" in captured["url"]
    assert "since_id=41" in captured["url"]
    assert "order=asc" in captured["url"]


def test_fetch_logs_keeps_unparseable_json_extra(tmp_path, monkeypatch):
    _install_token(tmp_path, monkeypatch)
    bad = {"logs": [dict(PAYLOAD["logs"][0], json_extra="{not json")], "count": 1, "max_id": 42}
    _capture_fetch(monkeypatch, bad)
    page = fetch_logs()
    assert page.rows[0].json_extra == "{not json"


def test_fetch_logs_wrong_shaped_logs_raises_logreaderror(tmp_path, monkeypatch):
    # Fix (multi-review): JSON-valid but wrong-shaped payloads (logs not a
    # list of objects) must surface as LogReadError, not AttributeError —
    # the routes only catch LogReadError, so anything else 500s the tab.
    _install_token(tmp_path, monkeypatch)
    for bad_logs in ("oops", [None], ["str-row"], [["nested"]]):
        _capture_fetch(monkeypatch, {"logs": bad_logs, "count": 0, "max_id": None})
        with pytest.raises(LogReadError):
            fetch_logs()


def test_fetch_logs_lenient_count_and_max_id(tmp_path, monkeypatch):
    # Fix (multi-review): count/max_id have no consumer yet — a missing or
    # reshaped field must not take down rows that parsed fine.
    _install_token(tmp_path, monkeypatch)
    _capture_fetch(monkeypatch, {"logs": PAYLOAD["logs"]})
    page = fetch_logs()
    assert len(page.rows) == 2
    assert page.count == 2  # falls back to len(rows)
    assert page.max_id is None
    _capture_fetch(monkeypatch, {"logs": PAYLOAD["logs"], "count": "junk", "max_id": "also-junk"})
    page = fetch_logs()
    assert page.count == 2 and page.max_id is None


def test_fetch_logs_missing_token_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(logreader, "_DEFAULT_ADMIN_TOKEN_FILE", tmp_path / "nope")
    with pytest.raises(LogReadError, match="token"):
        fetch_logs()


def test_fetch_logs_http_error_raises(tmp_path, monkeypatch):
    _install_token(tmp_path, monkeypatch)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LogReadError, match="401"):
        fetch_logs()


def test_fetch_logs_network_error_raises(tmp_path, monkeypatch):
    _install_token(tmp_path, monkeypatch)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LogReadError):
        fetch_logs()


def test_is_configured(tmp_path, monkeypatch):
    token = tmp_path / "logger-admin.token"
    monkeypatch.setattr(logreader, "_DEFAULT_ADMIN_TOKEN_FILE", token)
    assert is_configured() is False
    token.write_text("t")
    assert is_configured() is True


def test_admin_token_env_seam(tmp_path, monkeypatch):
    alt = tmp_path / "alt.token"
    alt.write_text("alt-tok")
    monkeypatch.setenv("LOGGER_ADMIN_TOKEN_FILE", str(alt))
    captured = _capture_fetch(monkeypatch, {"logs": [], "count": 0, "max_id": None})
    fetch_logs()
    assert captured["headers"]["Authorization"] == "Bearer alt-tok"


def _row(id, app="a", level="error"):
    return LogRow(id=id, app=app, level=level, message="m", ts="t", source=None, json_extra=None)


def test_group_rows_by_app_orders_groups_by_newest_row():
    rows = [_row(5, "backup"), _row(9, "agent-hq"), _row(3, "backup"), _row(1, "sentinel")]
    grouped = logreader.group_rows_by_app(rows)
    assert [g[0] for g in grouped] == ["agent-hq", "backup", "sentinel"]
    assert [r.id for r in dict(grouped)["backup"]] == [5, 3]


def test_errors_cache_fetched_at_is_when_the_loader_last_ran(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(logreader.time, "time", lambda: clock[0])
    cache = logreader.ErrorsCache(ttl=60.0)
    assert cache.fetched_at() is None
    page = LogPage(rows=[_row(1)], count=1, max_id=1)
    cache.fetch(lambda: page)
    clock[0] = 1010.0
    cache.fetch(lambda: page)  # TTL hit keeps the stamp
    assert cache.fetched_at() == 1000.0


def test_errors_cache_get_cached_and_ttl(monkeypatch):
    cache = logreader.ErrorsCache(ttl=60.0)
    assert cache.get_cached() is None
    page = LogPage(rows=[_row(1)], count=1, max_id=1)
    assert cache.fetch(lambda: page) is page
    got, fresh = cache.get_cached()
    assert got is page and fresh is True
    assert cache.fetch(lambda: (_ for _ in ()).throw(AssertionError)) is page  # TTL hit
    real_time = logreader.time.time
    monkeypatch.setattr(logreader.time, "time", lambda: real_time() + 10_000)
    got, fresh = cache.get_cached()
    assert got is page and fresh is False


def test_errors_cache_get_cached_not_blocked_by_inflight_fetch():
    # Fix (multi-review): fetch() must not hold the cache lock across the
    # loader — it's a WAN call (up to TIMEOUT_SECONDS), and holding the lock
    # would park every concurrent stale-serve get_cached() behind it,
    # defeating stale-while-revalidate exactly when the logger is slow.
    import threading

    cache = logreader.ErrorsCache(ttl=0.0)
    page = LogPage(rows=[], count=0, max_id=None)
    cache.fetch(lambda: page)

    entered = threading.Event()
    release = threading.Event()

    def slow_loader():
        entered.set()
        assert release.wait(timeout=5)
        return page

    fetcher = threading.Thread(target=lambda: cache.fetch(slow_loader, force=True))
    fetcher.start()
    try:
        assert entered.wait(timeout=5)
        result = {}
        reader = threading.Thread(target=lambda: result.setdefault("got", cache.get_cached()))
        reader.start()
        reader.join(timeout=2)
        assert not reader.is_alive(), "get_cached() blocked behind the in-flight loader"
        assert result["got"][0] is page
    finally:
        release.set()
        fetcher.join(timeout=5)


def test_errors_cache_loader_error_keeps_previous(monkeypatch):
    cache = logreader.ErrorsCache(ttl=0.0)
    page = LogPage(rows=[], count=0, max_id=None)
    cache.fetch(lambda: page)

    def boom():
        raise LogReadError("down")

    with pytest.raises(LogReadError):
        cache.fetch(boom, force=True)
    got, _fresh = cache.get_cached()
    assert got is page
