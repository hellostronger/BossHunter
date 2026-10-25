"""Job requirement skill graph (add-on analysis).

Pipeline:
1. ``extract``: ask the configured AI model to turn each JD into must/nice
   skills chosen from a shared taxonomy (result cached per job + JD hash).
2. ``build``: aggregate demand and co-occurrence, compare against the user's
   skill profile, and render an interactive HTML graph plus a gap report and
   a four-week study plan.

Outputs go to ``data/reports`` by default, which is the directory the web UI
and nginx deployments typically expose as static reports.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from bosshunter.ai.credentials import AIRequestError, call_anthropic_text

MODEL_FALLBACK = "claude-sonnet-4-6"
DEFAULT_PROFILE = Path("data") / "skills-profile.yaml"
DEFAULT_REPORT_DIR = Path("data") / "reports"
MAX_JOBS = 5000

TAXONOMY: dict[str, list[str]] = {
	"编程语言": ["Python", "Java", "Go", "C++", "TypeScript", "JavaScript", "SQL", "Shell"],
	"框架与工具": [
		"PyTorch",
		"TensorFlow",
		"HuggingFace Transformers",
		"LangChain",
		"LlamaIndex",
		"FastAPI",
		"Django",
		"Flask",
		"React",
		"Vue",
		"Spring Boot",
		"vLLM",
		"SGLang",
		"TensorRT",
		"ONNX",
		"Docker",
		"Kubernetes",
		"Git",
		"MCP",
	],
	"大模型技术": [
		"大模型/LLM",
		"RAG",
		"AI Agent",
		"提示工程",
		"大模型微调",
		"RLHF",
		"强化学习",
		"多模态",
		"模型蒸馏",
		"模型评测",
		"Function Calling",
		"知识库",
		"向量数据库",
		"上下文工程",
	],
	"数据与算法": ["机器学习", "深度学习", "NLP", "计算机视觉", "推荐系统", "数据挖掘", "特征工程", "数据分析"],
	"工程与基建": [
		"分布式系统",
		"微服务",
		"高并发",
		"云原生",
		"CI/CD",
		"MLOps",
		"推理优化",
		"性能调优",
		"数据平台",
		"消息队列",
		"Redis",
		"MySQL",
		"Linux",
		"全栈开发",
	],
	"产品与业务": ["产品设计", "需求分析", "项目管理", "业务理解", "解决方案", "客户交付", "技术文档", "跨团队协作"],
	"软技能": ["沟通能力", "学习能力", "问题解决", "自驱力", "英语能力", "抗压能力"],
}

CATEGORY_OF: dict[str, str] = {name: cat for cat, names in TAXONOMY.items() for name in names}

ALIASES = {
	"llm": "大模型/LLM",
	"large language model": "大模型/LLM",
	"大语言模型": "大模型/LLM",
	"大模型": "大模型/LLM",
	"大模型应用": "大模型/LLM",
	"大模型开发": "大模型/LLM",
	"agent": "AI Agent",
	"ai agent": "AI Agent",
	"aiagent": "AI Agent",
	"智能体": "AI Agent",
	"multi-agent": "AI Agent",
	"multi agent": "AI Agent",
	"rag": "RAG",
	"检索增强": "RAG",
	"检索增强生成": "RAG",
	"sft": "大模型微调",
	"微调": "大模型微调",
	"fine-tuning": "大模型微调",
	"finetune": "大模型微调",
	"prompt": "提示工程",
	"prompt engineering": "提示工程",
	"提示词": "提示工程",
	"prompting": "提示工程",
	"k8s": "Kubernetes",
	"kubernetes": "Kubernetes",
	"pytorch": "PyTorch",
	"torch": "PyTorch",
	"tensorflow": "TensorFlow",
	"transformers": "HuggingFace Transformers",
	"huggingface": "HuggingFace Transformers",
	"langchain": "LangChain",
	"llamaindex": "LlamaIndex",
	"llama index": "LlamaIndex",
	"fastapi": "FastAPI",
	"django": "Django",
	"flask": "Flask",
	"react": "React",
	"vue": "Vue",
	"spring boot": "Spring Boot",
	"vllm": "vLLM",
	"sglang": "SGLang",
	"tensorrt": "TensorRT",
	"onnx": "ONNX",
	"docker": "Docker",
	"git": "Git",
	"mcp": "MCP",
	"model context protocol": "MCP",
	"function calling": "Function Calling",
	"tools": "Function Calling",
	"工具调用": "Function Calling",
	"vector db": "向量数据库",
	"向量库": "向量数据库",
	"milvus": "向量数据库",
	"faiss": "向量数据库",
	"qdrant": "向量数据库",
	"pgvector": "向量数据库",
	"knowledge base": "知识库",
	"知识图谱": "知识库",
	"rlhf": "RLHF",
	"强化学习": "强化学习",
	"rl": "强化学习",
	"multimodal": "多模态",
	"多模态大模型": "多模态",
	"distillation": "模型蒸馏",
	"蒸馏": "模型蒸馏",
	"量化": "推理优化",
	"quantization": "推理优化",
	"evaluation": "模型评测",
	"评测": "模型评测",
	"evals": "模型评测",
	"context engineering": "上下文工程",
	"上下文管理": "上下文工程",
	"machine learning": "机器学习",
	"ml": "机器学习",
	"deep learning": "深度学习",
	"nlp": "NLP",
	"自然语言处理": "NLP",
	"computer vision": "计算机视觉",
	"cv": "计算机视觉",
	"recommendation": "推荐系统",
	"推荐算法": "推荐系统",
	"data mining": "数据挖掘",
	"feature engineering": "特征工程",
	"data analysis": "数据分析",
	"distributed system": "分布式系统",
	"distributed systems": "分布式系统",
	"microservice": "微服务",
	"microservices": "微服务",
	"高可用": "高并发",
	"cloud native": "云原生",
	"cloudnative": "云原生",
	"ci/cd": "CI/CD",
	"cicd": "CI/CD",
	"mlops": "MLOps",
	"inference optimization": "推理优化",
	"推理加速": "推理优化",
	"performance tuning": "性能调优",
	"data platform": "数据平台",
	"kafka": "消息队列",
	"redis": "Redis",
	"mysql": "MySQL",
	"linux": "Linux",
	"全栈": "全栈开发",
	"full stack": "全栈开发",
	"fullstack": "全栈开发",
	"full-stack": "全栈开发",
	"product design": "产品设计",
	"requirement analysis": "需求分析",
	"需求管理": "需求分析",
	"project management": "项目管理",
	"business understanding": "业务理解",
	"solution": "解决方案",
	"解决方案设计": "解决方案",
	"customer delivery": "客户交付",
	"technical writing": "技术文档",
	"技术写作": "技术文档",
	"collaboration": "跨团队协作",
	"communication": "沟通能力",
	"沟通": "沟通能力",
	"learning": "学习能力",
	"problem solving": "问题解决",
	"自驱": "自驱力",
	"self-driven": "自驱力",
	"english": "英语能力",
	"英语": "英语能力",
	"抗压": "抗压能力",
	"typescript": "TypeScript",
	"javascript": "JavaScript",
	"python": "Python",
	"java": "Java",
	"golang": "Go",
	"go": "Go",
	"c++": "C++",
	"sql": "SQL",
	"shell": "Shell",
}

PROMPT = """你是招聘岗位要求分析专家。请从下面的职位描述里抽取该岗位真正要求的能力，只输出一个 JSON 对象，不要输出解释或 Markdown。

输出格式：
{{"must":["Python","RAG"],"nice":["Docker"],"years":"3-5年","domains":["大模型应用"],"summary":"一句话总结岗位"}}

规则：
1. must 3-8 项（硬性要求），nice 0-6 项（加分项）。
2. 技能名必须优先从下面的标准词表里选择，使用词表中的原文；词表没有覆盖的再用简短通用术语（中文优先，不要拼接英文单词，例如写 "AI Agent" 而不是 "AIAgent"）。
3. 只写能力/技术/工具，不要写学历、公司、地点。
4. 如果职位描述很空泛，也必须给出 3 项最核心的推断能力。

标准词表：
{taxonomy}

职位：{title}
公司：{company}
职位描述：
{jd}
"""

PLAN_PROMPT = """你是一名务实的求职教练。下面是目标岗位（AI 应用 / 大模型方向）的技能需求统计，以及求职者当前已掌握的技能。

请输出一段 Markdown 学习计划（不要输出 JSON），要求：
- 先用 2-3 句话指出最关键的 3 个缺口和原因（结合岗位频次）。
- 给出 4 周突击计划：每周 1 个主题、3-5 个具体任务、可交付的小项目、验证方式。
- 任务必须具体到“学什么 + 做什么”，不要泛泛而谈。
- 优先安排“高频 + 当前缺失 + 能快速做出作品”的技能。
- 最后给 3 条面试表达建议（如何把学习成果讲成项目经验）。

技能需求（按加权需求排序，格式：技能(必须岗位数/加分岗位数)）：
{skills}

求职者已掌握：
{profile}
"""

LOWER_CANONICAL = {name.lower(): name for name in CATEGORY_OF}


def model_name(config: dict[str, Any] | None) -> str:
	ai_cfg = (config or {}).get("ai", {})
	return str(ai_cfg.get("model") or MODEL_FALLBACK)


def clean_jd(text: str) -> str:
	text = str(text or "")
	text = re.sub(r"(kanzhun|boss)", "", text, flags=re.I)
	text = re.sub(r"\s+", " ", text)
	return text.strip()


def parse_json_object(text: str) -> dict[str, Any]:
	text = re.sub(r"^```(?:json)?|```$", "", str(text or "").strip(), flags=re.M).strip()
	start = text.find("{")
	end = text.rfind("}")
	if start < 0 or end <= start:
		raise ValueError("模型没有返回 JSON 对象")
	return json.loads(text[start : end + 1])


def normalize_skill(raw: str) -> str | None:
	name = str(raw or "").strip().strip("，,。.;；:：")
	if not name:
		return None
	key = name.lower()
	if name in CATEGORY_OF:
		return name
	if key in LOWER_CANONICAL:
		return LOWER_CANONICAL[key]
	if key in ALIASES:
		return ALIASES[key]
	compact = re.sub(r"[\s/_\-.]+", "", key)
	if compact in ALIASES:
		return ALIASES[compact]
	if compact in LOWER_CANONICAL:
		return LOWER_CANONICAL[compact]
	if not key.isascii():
		for alias, canonical in ALIASES.items():
			if len(alias) >= 2 and alias in key:
				return canonical
	return f"其他:{name[:16]}"


def dedupe(items: list[str]) -> list[str]:
	seen, out = set(), []
	for item in items:
		if item not in seen:
			seen.add(item)
			out.append(item)
	return out


def jd_hash(text: str) -> str:
	return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def ensure_tables(conn: sqlite3.Connection) -> None:
	conn.execute(
		"""CREATE TABLE IF NOT EXISTS job_skill_tags (
            job_id TEXT PRIMARY KEY,
            jd_hash TEXT,
            payload TEXT,
            model TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
	)
	conn.commit()


def load_jobs(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
	sql = (
		"SELECT id, title, company, salary, city, url, jd FROM jobs "
		"WHERE deleted_at IS NULL AND length(trim(jd)) >= 80 "
		"ORDER BY created_at DESC"
	)
	limit = min(int(limit), MAX_JOBS) if limit else MAX_JOBS
	sql += f" LIMIT {limit}"
	return [dict(row) for row in conn.execute(sql)]


def extract_one(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
	prompt = PROMPT.format(
		taxonomy=json.dumps(TAXONOMY, ensure_ascii=False),
		title=job["title"],
		company=job["company"],
		jd=clean_jd(job["jd"])[:2600],
	)
	text = call_anthropic_text(prompt, config, max_tokens=6000, timeout=600, purpose="skill_graph")
	if not text:
		raise AIRequestError("empty_response", "技能抽取没有返回文本")
	data = parse_json_object(text)
	must = [name for name in (normalize_skill(x) for x in data.get("must", [])) if name]
	nice = [name for name in (normalize_skill(x) for x in data.get("nice", [])) if name]
	must = dedupe(must)[:8]
	nice = dedupe([name for name in nice if name not in must])[:6]
	return {
		"must": must,
		"nice": nice,
		"years": str(data.get("years") or "")[:20],
		"domains": [str(domain)[:20] for domain in (data.get("domains") or [])][:4],
		"summary": str(data.get("summary") or "")[:120],
	}


def run_extract(
	conn: sqlite3.Connection,
	config: dict[str, Any],
	*,
	limit: int | None = None,
	workers: int = 3,
	progress=None,
) -> dict[str, Any]:
	jobs = load_jobs(conn, limit)
	ensure_tables(conn)
	cached = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT job_id, jd_hash, payload FROM job_skill_tags")}
	todo = [job for job in jobs if cached.get(job["id"], ("", None))[0] != jd_hash(clean_jd(job["jd"]))]
	stats = {"jobs": len(jobs), "todo": len(todo), "extracted": 0, "errors": []}
	if progress:
		progress(f"岗位 {len(jobs)} 个，已缓存 {len(jobs) - len(todo)} 个，待抽取 {len(todo)} 个")

	def work(job: dict[str, Any]) -> dict[str, Any]:
		return extract_one(job, config)

	with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as pool:
		futures = {pool.submit(work, job): job for job in todo}
		for future in as_completed(futures):
			job = futures[future]
			try:
				result = future.result()
			except Exception as exc:  # noqa: BLE001 - one bad JD must not stop the run
				stats["errors"].append({"title": job["title"], "error": str(exc)})
				continue
			conn.execute(
				"INSERT INTO job_skill_tags(job_id, jd_hash, payload, model, updated_at) "
				"VALUES(?,?,?,?,CURRENT_TIMESTAMP) "
				"ON CONFLICT(job_id) DO UPDATE SET jd_hash=excluded.jd_hash, "
				"payload=excluded.payload, model=excluded.model, updated_at=CURRENT_TIMESTAMP",
				(
					job["id"],
					jd_hash(clean_jd(job["jd"])),
					json.dumps(result, ensure_ascii=False),
					model_name(config),
				),
			)
			conn.commit()
			stats["extracted"] += 1
			if progress and stats["extracted"] % 10 == 0:
				progress(f"已抽取 {stats['extracted']}/{len(todo)}")
	return stats


def load_profile(path: Path | None, inline: str | None = None) -> dict[str, str]:
	items: list[Any] = []
	if inline:
		items = [{"name": name, "level": "熟练"} for name in re.split(r"[,，;；\n]", inline) if name.strip()]
	elif path and Path(path).exists():
		import yaml

		data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
		items = data.get("skills") or []
	profile: dict[str, str] = {}
	for item in items:
		if isinstance(item, str):
			name, level = item, "熟练"
		elif isinstance(item, dict):
			name, level = item.get("name", ""), item.get("level", "熟练")
		else:
			continue
		canonical = normalize_skill(name)
		if canonical:
			profile[canonical] = "熟练" if str(level) not in ("了解", "入门", "basic") else "了解"
	return profile


def build_payload(
	conn: sqlite3.Connection,
	config: dict[str, Any],
	*,
	profile: dict[str, str],
	limit: int | None = None,
) -> dict[str, Any]:
	jobs = load_jobs(conn, limit)
	ensure_tables(conn)
	rows = {
		row["job_id"]: json.loads(row["payload"]) for row in conn.execute("SELECT job_id, payload FROM job_skill_tags")
	}
	skill_jobs: dict[str, list[dict[str, Any]]] = defaultdict(list)
	must_count: Counter = Counter()
	nice_count: Counter = Counter()
	analyzed = 0
	for job in jobs:
		data = rows.get(job["id"])
		if not data:
			continue
		analyzed += 1
		for name in data.get("must", []):
			must_count[name] += 1
			skill_jobs[name].append(job)
		for name in data.get("nice", []):
			nice_count[name] += 1
			if job not in skill_jobs[name]:
				skill_jobs[name].append(job)

	nodes = []
	for name, job_list in skill_jobs.items():
		demand = must_count[name] * 2 + nice_count[name]
		status = "unknown"
		if name in profile:
			status = "owned" if profile[name] == "熟练" else "partial"
		elif profile:
			status = "missing"
		nodes.append(
			{
				"id": name,
				"label": name,
				"category": CATEGORY_OF.get(name, "其他"),
				"must": must_count[name],
				"nice": nice_count[name],
				"demand": demand,
				"jobs": len(job_list),
				"status": status,
				"samples": [
					{"t": job["title"], "c": job["company"], "s": job["salary"], "u": job["url"]}
					for job in job_list[:6]
				],
			}
		)
	nodes.sort(key=lambda node: -node["demand"])

	top_ids = {node["id"] for node in nodes[:45]}
	pairs: Counter = Counter()
	for job in jobs:
		data = rows.get(job["id"])
		if not data:
			continue
		names = sorted({name for name in data.get("must", []) + data.get("nice", []) if name in top_ids})
		for index in range(len(names)):
			for other in range(index + 1, len(names)):
				pairs[(names[index], names[other])] += 1
	edges = [
		{"source": source, "target": target, "weight": weight}
		for (source, target), weight in pairs.most_common(160)
		if weight >= 2
	]

	total_demand = sum(node["demand"] for node in nodes)
	covered_demand = sum(node["demand"] for node in nodes if node["status"] == "owned")
	return {
		"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
		"job_count": analyzed,
		"profile_skills": sorted(profile),
		"stats": {
			"skills": len(nodes),
			"owned": sum(1 for node in nodes if node["status"] == "owned"),
			"partial": sum(1 for node in nodes if node["status"] == "partial"),
			"missing": sum(1 for node in nodes if node["status"] == "missing"),
			"coverage": round(covered_demand / total_demand * 100) if total_demand else 0,
		},
		"nodes": nodes,
		"edges": edges,
		"categories": sorted({node["category"] for node in nodes}),
	}


def generate_plan(payload: dict[str, Any], config: dict[str, Any], profile: dict[str, str]) -> str:
	skills = "\n".join(
		f"- {node['label']}({node['must']}/{node['nice']}) 类别:{node['category']} 状态:{node['status']}"
		for node in payload["nodes"][:16]
	)
	profile_text = "、".join(sorted(profile)) or "（尚未填写）"
	try:
		text = call_anthropic_text(
			PLAN_PROMPT.format(skills=skills, profile=profile_text),
			config,
			max_tokens=4000,
			timeout=600,
			purpose="skill_graph_plan",
		)
		return text or "> 学习计划生成失败：模型没有返回文本"
	except Exception as exc:  # noqa: BLE001 - report remains useful without a plan
		return f"> 学习计划生成失败：{exc}"


def md_to_html(text: str) -> str:
	text = str(text or "").replace("\ufffd", "")
	lines, out = text.splitlines(), []
	in_list = False
	i = 0
	while i < len(lines):
		line = lines[i].rstrip()
		if re.match(r"^#{1,6}\s", line):
			if in_list:
				out.append("</ul>")
				in_list = False
			level = min(len(line) - len(line.lstrip("#")), 2)
			out.append(f"<h{level + 2}>{inline_md(line.lstrip('#').strip())}</h{level + 2}>")
		elif line.strip().startswith("|") and "|" in line.strip()[1:]:
			if in_list:
				out.append("</ul>")
				in_list = False
			rows = []
			while i < len(lines) and lines[i].strip().startswith("|"):
				row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
				if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in row):
					rows.append(row)
				i += 1
			i -= 1
			if rows:
				head, body = rows[0], rows[1:]
				cells = "".join(f"<th>{inline_md(c)}</th>" for c in head)
				out.append(f"<table><thead><tr>{cells}</tr></thead><tbody>")
				for row in body:
					cells = "".join(f"<td>{inline_md(c)}</td>" for c in row)
					out.append(f"<tr>{cells}</tr>")
				out.append("</tbody></table>")
		elif re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
			if in_list:
				out.append("</ul>")
				in_list = False
			out.append("<hr>")
		elif re.match(r"^\s*[-*]\s+", line):
			if not in_list:
				out.append("<ul>")
				in_list = True
			out.append(f"<li>{inline_md(re.sub(r'^\s*[-*]\s+', '', line))}</li>")
		elif not line.strip():
			if in_list:
				out.append("</ul>")
				in_list = False
		else:
			if in_list:
				out.append("</ul>")
				in_list = False
			out.append(f"<p>{inline_md(line)}</p>")
		i += 1
	if in_list:
		out.append("</ul>")
	return "\n".join(out)


def inline_md(text: str) -> str:
	text = html.escape(text)
	text = re.sub(r"&lt;br\s*/?&gt;", "<br>", text, flags=re.I)
	text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
	text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
	return text


def write_reports(
	payload: dict[str, Any],
	*,
	report_dir: Path,
	plan_md: str,
	template_path: Path | None = None,
) -> dict[str, str]:
	report_dir.mkdir(parents=True, exist_ok=True)
	payload = {**payload, "plan_html": md_to_html(plan_md)}
	(report_dir / "skills-graph.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
	template = template_path or Path(__file__).with_name("skills_template.html")
	source = template.read_text(encoding="utf-8")
	data_js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
	(report_dir / "skills-graph.html").write_text(source.replace("/*__DATA__*/", data_js), encoding="utf-8")

	gap_lines = [
		"# 岗位技能差距报告",
		"",
		f"生成时间：{payload['generated_at']} · 分析岗位：{payload['job_count']} 个",
		"",
		f"- 共识别技能：{payload['stats']['skills']} 项；已具备 {payload['stats']['owned']} 项，"
		f"部分 {payload['stats']['partial']} 项，缺口 {payload['stats']['missing']} 项",
		f"- 按岗位加权覆盖率：{payload['stats']['coverage']}%",
		"",
		"## 需求最高的技能",
		"",
		"| 技能 | 类别 | 硬性岗位 | 加分岗位 | 需求权重 | 我的状态 |",
		"| --- | --- | ---: | ---: | ---: | --- |",
	]
	for node in payload["nodes"][:25]:
		gap_lines.append(
			f"| {node['label']} | {node['category']} | {node['must']} | {node['nice']} | "
			f"{node['demand']} | {node['status']} |"
		)
	gap_lines += ["", "## 学习计划", "", plan_md or "（未生成）"]
	(report_dir / "gap-report.md").write_text("\n".join(gap_lines), encoding="utf-8")
	return {
		"html": str(report_dir / "skills-graph.html"),
		"json": str(report_dir / "skills-graph.json"),
		"md": str(report_dir / "gap-report.md"),
	}


def build_skill_graph(
	config: dict[str, Any],
	*,
	db_path: Path | str | None = None,
	report_dir: Path | str = DEFAULT_REPORT_DIR,
	profile_path: Path | str | None = DEFAULT_PROFILE,
	skills: str | None = None,
	limit: int | None = None,
	workers: int = 3,
	skip_extract: bool = False,
	no_plan: bool = False,
	progress=None,
) -> dict[str, Any]:
	path = Path(db_path) if db_path else Path("data") / "bosshunter.db"
	if not path.exists():
		raise FileNotFoundError(f"找不到数据库：{path}")
	conn = sqlite3.connect(str(path), check_same_thread=False)
	conn.row_factory = sqlite3.Row
	try:
		if not skip_extract:
			extract_stats = run_extract(conn, config, limit=limit, workers=workers, progress=progress)
		else:
			extract_stats = {"jobs": len(load_jobs(conn, limit)), "todo": 0, "extracted": 0, "errors": []}
		profile = load_profile(Path(profile_path) if profile_path else None, skills)
		payload = build_payload(conn, config, profile=profile, limit=limit)
		plan_md = "" if no_plan else generate_plan(payload, config, profile)
		files = write_reports(payload, report_dir=Path(report_dir), plan_md=plan_md)
	finally:
		conn.close()
	return {
		"extract": extract_stats,
		"jobs": payload["job_count"],
		"skills": len(payload["nodes"]),
		"coverage": payload["stats"]["coverage"],
		"profile_skills": len(profile),
		**files,
	}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="构建岗位技能图谱与差距报告")
	parser.add_argument("--db", default=str(Path("data") / "bosshunter.db"))
	parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
	parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
	parser.add_argument("--skills", default=None, help="用逗号分隔的已掌握技能")
	parser.add_argument("--limit", type=int, default=None)
	parser.add_argument("--workers", type=int, default=3)
	parser.add_argument("--skip-extract", action="store_true")
	parser.add_argument("--no-plan", action="store_true")
	parser.add_argument("--config", default="config.yaml")
	args = parser.parse_args(argv)
	from bosshunter.config import load_config

	config = load_config(Path(args.config) if Path(args.config).exists() else None)
	result = build_skill_graph(
		config,
		db_path=args.db,
		report_dir=args.report_dir,
		profile_path=args.profile,
		skills=args.skills,
		limit=args.limit,
		workers=args.workers,
		skip_extract=args.skip_extract,
		no_plan=args.no_plan,
		progress=lambda message: print(message, flush=True),
	)
	print(json.dumps(result, ensure_ascii=False, indent=1))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
