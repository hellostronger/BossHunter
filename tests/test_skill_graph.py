"""Skill graph aggregation and report rendering."""

import json
import sqlite3

from bosshunter.analysis.skill_graph import (
	build_payload,
	inline_md,
	load_profile,
	md_to_html,
	normalize_skill,
	parse_json_object,
	write_reports,
)


def _conn():
	conn = sqlite3.connect(":memory:")
	conn.row_factory = sqlite3.Row
	conn.execute(
		"""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, company TEXT, salary TEXT, city TEXT,
            url TEXT, jd TEXT, deleted_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
	)
	conn.execute(
		"""CREATE TABLE job_skill_tags (
            job_id TEXT PRIMARY KEY, jd_hash TEXT, payload TEXT, model TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
	)
	return conn


def test_normalize_skill_uses_taxonomy_aliases_and_fallback():
	assert normalize_skill("Python") == "Python"
	assert normalize_skill("k8s") == "Kubernetes"
	assert normalize_skill("大语言模型") == "大模型/LLM"
	assert normalize_skill("智能体") == "AI Agent"
	assert normalize_skill("向量库") == "向量数据库"
	assert normalize_skill("AIAgent") == "AI Agent"
	assert normalize_skill("Prompt") == "提示工程"
	assert normalize_skill("自研检索引擎") == "其他:自研检索引擎"
	assert normalize_skill("   ") is None


def test_parse_json_object_tolerates_markdown_fences():
	text = '```json\n{"must": ["RAG"], "nice": []}\n```'
	assert parse_json_object(text) == {"must": ["RAG"], "nice": []}


def test_load_profile_accepts_inline_and_levels(tmp_path):
	inline = load_profile(None, "Python,k8s, 智能体")
	assert inline == {"Python": "熟练", "Kubernetes": "熟练", "AI Agent": "熟练"}

	path = tmp_path / "profile.yaml"
	path.write_text(
		"skills:\n  - {name: Python, level: 熟练}\n  - {name: Docker, level: 了解}\n  - Redis\n",
		encoding="utf-8",
	)
	profile = load_profile(path)
	assert profile == {"Python": "熟练", "Docker": "了解", "Redis": "熟练"}


def test_build_payload_marks_missing_and_computes_coverage():
	conn = _conn()
	for index in range(3):
		conn.execute(
			"INSERT INTO jobs(id, title, company, salary, city, url, jd) VALUES(?,?,?,?,?,?,?)",
			(f"job{index}", f"AI 工程师 {index}", "某公司", "30-50K", "深圳", "https://x", "JD " * 60),
		)
	conn.commit()
	payloads = {
		"job0": {"must": ["Python", "RAG"], "nice": ["Docker"]},
		"job1": {"must": ["Python", "AI Agent"], "nice": []},
		"job2": {"must": ["RAG"], "nice": ["Docker"]},
	}
	for job_id, payload in payloads.items():
		conn.execute(
			"INSERT INTO job_skill_tags(job_id, jd_hash, payload, model) VALUES(?,?,?,?)",
			(job_id, "hash", json.dumps(payload), "test"),
		)
	conn.commit()

	profile = {"Python": "熟练", "Docker": "了解"}
	payload = build_payload(conn, {}, profile=profile)

	assert payload["job_count"] == 3
	nodes = {node["label"]: node for node in payload["nodes"]}
	assert nodes["Python"]["must"] == 2
	assert nodes["Python"]["status"] == "owned"
	assert nodes["Docker"]["status"] == "partial"
	assert nodes["RAG"]["status"] == "missing"
	assert payload["stats"]["missing"] >= 1
	assert any({edge["source"], edge["target"]} == {"RAG", "Docker"} for edge in payload["edges"])

	empty = build_payload(conn, {}, profile={})
	assert {node["status"] for node in empty["nodes"]} == {"unknown"}


def test_write_reports_renders_template_and_markdown(tmp_path):
	payload = {
		"generated_at": "2026-01-01 00:00",
		"job_count": 2,
		"profile_skills": [],
		"stats": {"skills": 1, "owned": 0, "partial": 0, "missing": 0, "coverage": 0},
		"nodes": [
			{
				"id": "RAG",
				"label": "RAG",
				"category": "大模型技术",
				"must": 2,
				"nice": 0,
				"demand": 4,
				"jobs": 2,
				"status": "unknown",
				"samples": [],
			}
		],
		"edges": [],
		"categories": ["大模型技术"],
	}
	files = write_reports(payload, report_dir=tmp_path, plan_md="## 第一周\n- 学 RAG\n- 做一个小项目")
	html = (tmp_path / "skills-graph.html").read_text(encoding="utf-8")
	md = (tmp_path / "gap-report.md").read_text(encoding="utf-8")
	assert "const DATA =" in html
	assert "<h4>第一周" in html
	assert "学 RAG" in md
	assert files["json"].endswith("skills-graph.json")


def test_markdown_helpers_escape_html():
	assert inline_md("<b>X</b>") == "&lt;b&gt;X&lt;/b&gt;"
	assert md_to_html("**粗体**") == "<p><strong>粗体</strong></p>"
