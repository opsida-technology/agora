"""Smoke tests for the DebateServer HTTP endpoints."""
import json
import threading
import time
import urllib.request
import urllib.error

from agora.web import DebateServer, Handler


def _start_test_server(port=18420):
    """Start a DebateServer on a test port and return it."""
    server = DebateServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    return server


def _get(path, port=18420):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.urlopen(url, timeout=5)
    return req.status, req.read().decode()


def _post(path, data=None, port=18420, content_type="application/json"):
    url = f"http://127.0.0.1:{port}{path}"
    body = json.dumps(data).encode() if data else b""
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": content_type})
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, json.loads(resp.read().decode())


class TestServerEndpoints:

    server = None

    @classmethod
    def setup_class(cls):
        cls.server = _start_test_server(18421)

    @classmethod
    def teardown_class(cls):
        if cls.server:
            cls.server.shutdown()

    def test_dashboard_page(self):
        status, body = _get("/", 18421)
        assert status == 200
        assert "<!DOCTYPE html>" in body

    def test_debate_page(self):
        status, body = _get("/debate", 18421)
        assert status == 200
        assert "<!DOCTYPE html>" in body

    def test_history_page(self):
        status, body = _get("/history", 18421)
        assert status == 200
        assert "<!DOCTYPE html>" in body

    def test_history_detail_page(self):
        status, body = _get("/history/any-dir", 18421)
        assert status == 200
        assert "<!DOCTYPE html>" in body

    def test_configs_page(self):
        status, body = _get("/configs", 18421)
        assert status == 200
        assert "<!DOCTYPE html>" in body
        assert "Debate Configs" in body

    def test_api_configs(self):
        status, body = _get("/api/configs", 18421)
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)
        # Verify _prefixed configs are filtered out
        for c in data:
            assert not c["name"].startswith("_")

    def test_api_configs_have_mtime(self):
        status, body = _get("/api/configs", 18421)
        data = json.loads(body)
        for c in data:
            assert "mtime" in c

    def test_api_status_no_debate(self):
        status, body = _get("/api/status", 18421)
        assert status == 200
        data = json.loads(body)
        assert data["running"] is False
        assert data["config"] is None

    def test_api_debates(self):
        status, body = _get("/api/debates", 18421)
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)

    def test_api_stop_no_debate(self):
        status, data = _post("/api/stop", port=18421)
        assert status == 200
        assert data["ok"] is True

    def test_api_pause_resume(self):
        status, data = _post("/api/pause", port=18421)
        assert status == 200
        assert data["paused"] is True
        assert self.server.debate_paused.is_set()

        status, data = _post("/api/resume", port=18421)
        assert status == 200
        assert data["paused"] is False
        assert not self.server.debate_paused.is_set()

    def test_api_parse_yaml(self):
        url = f"http://127.0.0.1:18421/api/parse-yaml"
        body = b"topic: test\nmax_turns: 5"
        req = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "text/plain"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        assert data["topic"] == "test"
        assert data["max_turns"] == 5

    def test_api_to_yaml(self):
        url = f"http://127.0.0.1:18421/api/to-yaml"
        body = json.dumps({"topic": "test", "max_turns": 5}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        text = resp.read().decode()
        assert "topic: test" in text

    def test_api_intervene_no_debate(self):
        status, data = _post("/api/intervene",
                             {"message": "hello"}, port=18421)
        assert status == 200
        assert data["ok"] is False
        assert "No debate running" in data["error"]

    def test_api_start_missing_config(self):
        try:
            _post("/api/start", {"config": "nonexistent.yaml"}, port=18421)
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_static_i18n_js(self):
        status, body = _get("/static/i18n.js", 18421)
        assert status == 200
        assert "I18N" in body

    def test_static_lang_en(self):
        status, body = _get("/static/lang/en.json", 18421)
        assert status == 200
        data = json.loads(body)
        assert "app.title" in data
        assert data["app.title"] == "Agora Protocol"

    def test_static_lang_tr(self):
        status, body = _get("/static/lang/tr.json", 18421)
        assert status == 200
        data = json.loads(body)
        assert "app.title" in data

    def test_lang_files_key_parity(self):
        """en.json and tr.json must have the same keys."""
        _, en_body = _get("/static/lang/en.json", 18421)
        _, tr_body = _get("/static/lang/tr.json", 18421)
        en_keys = set(json.loads(en_body).keys())
        tr_keys = set(json.loads(tr_body).keys())
        assert en_keys == tr_keys, f"Missing in tr: {en_keys - tr_keys}"

    def test_templates_include_i18n(self):
        """All page templates must include i18n.js."""
        for path in ["/", "/debate", "/history", "/configs"]:
            _, body = _get(path, 18421)
            assert "i18n.js" in body, f"{path} missing i18n.js include"

    def test_debate_server_state_isolation(self):
        """Each DebateServer has independent state."""
        s1 = DebateServer(("127.0.0.1", 0), Handler)
        s2 = DebateServer(("127.0.0.1", 0), Handler)
        s1.active_config = "test1.yaml"
        assert s2.active_config is None
        s1.debate_paused.set()
        assert not s2.debate_paused.is_set()
