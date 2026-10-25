"""WebBridge client and BOSS sidecar helpers (no real browser)."""

import json
from pathlib import Path

import pytest

from bosshunter.collection.platforms.boss_js import JS_DETECT_COLLECTION_RISK, JS_EXTRACT_LIST
from bosshunter.webbridge import client as client_module
from bosshunter.webbridge import boss as boss_module
from bosshunter.webbridge.boss import (
	BossWebBridgeCollector,
	decode_text,
	diagnose,
	install_digits,
	job_id_from_url,
)
from bosshunter.webbridge.client import WebBridgeClient, WebBridgeError


class _Response:
	def __init__(self, payload, status=200):
		self._payload = payload
		self.status_code = status

	def raise_for_status(self):
		if self.status_code >= 400:
			raise RuntimeError(f"HTTP {self.status_code}")

	def json(self):
		return self._payload


class _FakeHttpxClient:
	def __init__(self, response, calls):
		self._response = response
		self._calls = calls

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False

	def post(self, url, json=None):
		self._calls.append((url, json))
		return self._response


def test_command_posts_action_and_returns_data(monkeypatch):
	calls = []
	payload = {"ok": True, "data": {"type": "string", "value": "hello"}}
	monkeypatch.setattr(client_module.httpx, "Client", lambda **kwargs: _FakeHttpxClient(_Response(payload), calls))
	client = WebBridgeClient(url="http://127.0.0.1:10087/command", session="s1")
	assert client.command("evaluate", {"code": "1"}) == {"type": "string", "value": "hello"}
	assert calls == [("http://127.0.0.1:10087/command", {"action": "evaluate", "args": {"code": "1"}, "session": "s1"})]
	assert client.evaluate("1") == "hello"


def test_command_raises_on_rejected_action(monkeypatch):
	payload = {"ok": False, "error": "tab not found"}
	monkeypatch.setattr(client_module.httpx, "Client", lambda **kwargs: _FakeHttpxClient(_Response(payload), []))
	with pytest.raises(WebBridgeError, match="tab not found"):
		WebBridgeClient().command("close_tab", {})


def test_digit_table_and_job_id():
	text = chr(0xE035) + chr(0xE031) + "-" + chr(0xE038) + chr(0xE031) + "K"
	assert decode_text(text) == "40-70K"
	install_digits({chr(0xE031): "9"})
	assert decode_text(chr(0xE031)) == "9"
	install_digits({})
	assert decode_text(chr(0xE031)) == "9"  # empty map never clears the table
	install_digits({chr(0xE031 + i): str(i) for i in range(10)})
	assert job_id_from_url("https://www.zhipin.com/job_detail/abc123.html?x=1") == "abc123"


def test_collector_resolves_city_and_merges_candidate():
	collector = BossWebBridgeCollector({"search": {"city_codes": {"深圳": "101280600"}}}, client=object())
	assert collector._resolve_city_code("深圳") == "101280600"
	assert collector._resolve_city_code("广州") == "101280100"
	with pytest.raises(WebBridgeError):
		collector._resolve_city_code("不存在")

	candidate = collector._merge_candidate(
		{
			"title": "AI Agent 工程师",
			"company": "示例科技",
			"salary": "30-60K",
			"url": "/job_detail/xyz789.html",
			"city": "深圳",
			"city_code": "101280600",
		},
		None,
		"AI Agent",
	)
	assert candidate.storage_id == "xyz789"
	assert candidate.url.startswith("https://www.zhipin.com/job_detail/")
	assert candidate.source_keyword == "AI Agent"
	assert candidate.as_job_record()["source_platform"] == "boss"


def test_diagnose_reports_login_state():
	class FakeClient:
		url = "http://127.0.0.1:10086/command"
		session = "bosshunter"

		def list_tabs(self):
			return {"tabs": [1]}

		def evaluate(self, code, timeout=None):
			return json.dumps({"url": "https://www.zhipin.com/web/user/login", "login": True, "cards": 0})

	result = diagnose(FakeClient())
	assert result["tabs"] == {"tabs": [1]}
	assert result["page"]["login"] is True


def test_collect_dry_run_dedupes_and_decodes(monkeypatch, tmp_path):
	items = [
		{
			"title": "AI Agent" + chr(0xE031),
			"company": "A",
			"salary": "3" + chr(0xE031) + "-5" + chr(0xE031) + "K",
			"url": "/job_detail/dup1.html",
			"experience": "3-5年",
			"education": "本科",
			"location": "深圳",
		},
		{
			"title": "AI Agent" + chr(0xE031),
			"company": "A",
			"salary": "30-50K",
			"url": "/job_detail/dup1.html",
			"experience": "3-5年",
			"education": "本科",
			"location": "深圳",
		},
		{
			"title": "RAG 工程师",
			"company": "B",
			"salary": "40-60K",
			"url": "/job_detail/new2.html",
			"experience": "3-5年",
			"education": "本科",
			"location": "深圳",
		},
	]

	class FakeClient:
		def navigate(self, url, new_tab=False):
			return None

		def evaluate(self, code, timeout=None):
			if code == boss_module.JS_CARD_COUNT:
				return 3
			if code == JS_DETECT_COLLECTION_RISK:
				return '{"risk": null}'
			if code == JS_EXTRACT_LIST:
				return json.dumps(items, ensure_ascii=False)
			return "ok"

		def close_tab(self):
			return None

	class DummyConn:
		def close(self):
			return None

	inserted = []

	monkeypatch.setattr(boss_module, "get_db", lambda: DummyConn())
	monkeypatch.setattr(boss_module, "job_exists", lambda conn, job_id: False)
	monkeypatch.setattr(boss_module, "insert_job", lambda conn, record, rules=None: inserted.append(record) or True)

	collector = BossWebBridgeCollector({}, client=FakeClient())
	collector._refresh_digits = lambda: False
	collector._human_sleep = lambda low, high: None
	collector.sleep = lambda seconds: None
	result = collector.collect(
		["AI Agent"],
		["深圳"],
		pages=1,
		details=0,
		max_new=10,
		dry_run=True,
		use_filter=False,
		report_dir=tmp_path,
	)

	assert result["seen"] == 3
	assert result["new"] == 2
	assert result["duplicate"] == 1
	titles = sorted(job["title"] for job in result["jobs"])
	assert titles == ["AI Agent0", "RAG 工程师"]
	assert Path(result["report"]).exists()
