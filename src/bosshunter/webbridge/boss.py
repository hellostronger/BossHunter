"""BOSS collection through Kimi WebBridge.

The regular collector drives Chrome over CDP and is subject to BossHunter's
internal page guard. This module performs the same two-step collection
(search list -> detail page) inside the user's already logged-in Chrome
through the WebBridge extension, then reuses the project's own
``JobCandidate`` / ``quick_score`` / ``insert_job`` pipeline so the data lands
in the regular ``jobs`` table with the same dedupe and outsourcing rules.

It is an opt-in add-on: nothing changes for users who keep using the CDP
collector.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from bosshunter.ai.prefilter import quick_score
from bosshunter.collection.models import JobCandidate
from bosshunter.collection.platforms.boss_font import (
	JS_COLLECT_FONT_SOURCES,
	build_boss_digit_map,
	collect_font_sources,
	parse_font_face_urls,
)
from bosshunter.collection.platforms.boss_js import (
	JS_DETECT_COLLECTION_RISK,
	JS_EXTRACT_DETAIL,
	JS_EXTRACT_LIST,
)
from bosshunter.config import CITY_CODES
from bosshunter.db import get_db, insert_job, job_exists
from bosshunter.outsourcing import load_rules
from bosshunter.webbridge.client import WebBridgeClient, WebBridgeError


SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={query}&city={city_code}&page={page}"
MAX_PAGES = 10
MAX_DETAILS = 50
DEFAULT_DETAIL_DELAY = (2.0, 4.0)
DEFAULT_PAGE_DELAY = (7.0, 14.0)

JS_CARD_COUNT = "document.querySelectorAll('.job-card-wrap').length"
JS_SCROLL_DOWN = """
(() => {
    const list = document.querySelector('.page-jobs .job-list-container .rec-job-list');
    if (list) list.scrollTop = list.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
    window.dispatchEvent(new Event('scroll'));
    return 'ok';
})()
"""

RISK_MESSAGES = {
	"captcha": "BOSS 弹出了验证码/安全验证",
	"login_required": "BOSS 跳到了登录页，请先在 Chrome 中登录",
	"blocked": "页面被拦截（403/账号受限）",
	"rate_limit": "访问频率受限，建议暂停后再试",
}

# BOSS renders salary digits with a private-use font. U+E031..U+E03A maps to
# 0..9 for the current kanzhun font; the dynamic rebuild below overrides this
# table when the font still carries ASCII reference glyphs.
_DIGITS: dict[str, str] = {chr(0xE031 + i): str(i) for i in range(10)}
_FONT_HEADERS = {
	"User-Agent": (
		"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
	),
	"Referer": "https://www.zhipin.com/web/geek/jobs",
}


def install_digits(mapping: dict[str, str]) -> bool:
	"""Replace the digit table when the dynamic font rebuild produced one."""
	global _DIGITS
	fresh = {
		code: digit
		for code, digit in (mapping or {}).items()
		if isinstance(code, str) and len(code) == 1 and "0" <= str(digit) <= "9"
	}
	if not fresh:
		return False
	changed = fresh != _DIGITS
	_DIGITS = fresh
	return changed


def decode_text(value: str) -> str:
	text = str(value or "")
	if not _DIGITS:
		return text
	return text.translate({ord(code): digit for code, digit in _DIGITS.items()})


def job_id_from_url(url: str) -> str:
	match = re.search(r"/job_detail/([^.]+)", str(url or ""))
	if match:
		return match.group(1)
	return str(abs(hash(str(url or ""))))[:16]


class BossWebBridgeCollector:
	def __init__(
		self,
		config: dict[str, Any],
		*,
		client: WebBridgeClient | None = None,
		url: str | None = None,
		session: str | None = None,
		delay_scale: float = 1.0,
		sleep: Callable[[float], None] = time.sleep,
	) -> None:
		self.config = config or {}
		wb_cfg = self.config.get("webbridge", {}) if isinstance(self.config.get("webbridge"), dict) else {}
		self.client = client or WebBridgeClient(
			url=url or wb_cfg.get("command_url") or "http://127.0.0.1:10086/command",
			session=session or wb_cfg.get("session") or "bosshunter",
			timeout=float(wb_cfg.get("timeout_seconds") or 60),
		)
		self.delay_scale = max(float(delay_scale or 1.0), 0.1)
		self.sleep = sleep
		self._rule_cache = load_rules(self.config)

	# -- browser helpers -------------------------------------------------
	def _human_sleep(self, low: float, high: float) -> None:
		self.sleep(random.uniform(low, high) * self.delay_scale)

	def _evaluate(self, code: str, *, timeout: float | None = None) -> Any:
		return self.client.evaluate(code, timeout=timeout)

	def _check_risk(self) -> None:
		try:
			payload = json.loads(str(self._evaluate(JS_DETECT_COLLECTION_RISK) or "{}"))
		except Exception:  # noqa: BLE001 - risk detection is best effort
			return
		risk = payload.get("risk")
		if risk:
			raise WebBridgeError(RISK_MESSAGES.get(risk, f"页面风险信号：{risk}"))

	def _wait_for_cards(self, timeout: float = 30.0) -> int:
		deadline = time.time() + timeout
		last = 0
		while time.time() < deadline:
			try:
				last = int(self._evaluate(JS_CARD_COUNT) or 0)
			except Exception:  # noqa: BLE001
				last = 0
			if last > 0:
				return last
			self.sleep(2)
		return last

	def _load_more(self, target: int = 30) -> None:
		count = int(self._evaluate(JS_CARD_COUNT) or 0)
		for _ in range(6):
			if count >= target:
				break
			self._evaluate(JS_SCROLL_DOWN)
			self.sleep(random.uniform(1.5, 2.6) * self.delay_scale)
			new_count = int(self._evaluate(JS_CARD_COUNT) or 0)
			if new_count <= count:
				break
			count = new_count

	def _refresh_digits(self) -> bool:
		"""Rebuild the salary font map from the font the tab actually loaded."""
		try:
			raw = self._evaluate(JS_COLLECT_FONT_SOURCES)
			urls, css_sources = collect_font_sources(raw)
			css = "\n".join(source for source in css_sources if not source.startswith("http"))
			for href in [source for source in css_sources if source.startswith("http")][:3]:
				try:
					response = httpx.get(href, headers=_FONT_HEADERS, timeout=10, follow_redirects=True)
					if response.status_code == 200:
						css += "\n" + response.text
				except Exception:  # noqa: BLE001
					continue
			candidates = urls + [url for url in parse_font_face_urls(css) if url not in urls]
			for font_url in candidates[:3]:
				if font_url.startswith("//"):
					font_url = "https:" + font_url
				try:
					response = httpx.get(font_url, headers=_FONT_HEADERS, timeout=10, follow_redirects=True)
					font_bytes = response.content if response.status_code == 200 else b""
				except Exception:  # noqa: BLE001
					continue
				if font_bytes and install_digits(build_boss_digit_map(font_bytes)):
					return True
		except Exception:  # noqa: BLE001 - keep the static table
			return False
		return False

	# -- page steps ------------------------------------------------------
	def _collect_page(self, keyword: str, city: str, city_code: str, page: int) -> list[dict[str, Any]]:
		url = SEARCH_URL.format(query=urllib.parse.quote(keyword), city_code=city_code, page=page)
		self.client.navigate(url)
		self._human_sleep(2.5, 4.0)
		cards = self._wait_for_cards()
		if cards == 0:
			self._check_risk()
			raise WebBridgeError("列表页没有解析到岗位卡片，可能页面结构变化或加载失败")
		self._check_risk()
		self._load_more(30)
		self._refresh_digits()
		raw = self._evaluate(JS_EXTRACT_LIST)
		jobs = json.loads(raw) if raw else []
		for item in jobs:
			for field in ("title", "salary", "experience", "education", "company", "location"):
				item[field] = decode_text(item.get(field) or "")
			item["city"] = city
			item["city_code"] = city_code
		return jobs

	def _wait_for_detail(self, timeout: float = 15.0) -> bool:
		deadline = time.time() + timeout
		while time.time() < deadline:
			try:
				length = int(
					self._evaluate("(document.querySelector('.job-sec-text')?.textContent||'').trim().length") or 0
				)
			except Exception:  # noqa: BLE001
				length = 0
			if length > 40:
				return True
			self.sleep(1.5)
		return False

	def _collect_detail(self, url: str) -> dict[str, Any]:
		self.client.navigate(url, new_tab=True)
		try:
			self._human_sleep(1.5, 2.8)
			if not self._wait_for_detail():
				self._check_risk()
				raise WebBridgeError("JD 未加载")
			self._refresh_digits()
			detail = json.loads(str(self._evaluate(JS_EXTRACT_DETAIL) or "{}"))
			for field in (
				"title",
				"salary",
				"experience",
				"education",
				"company",
				"jd",
				"company_size",
				"company_industry",
				"hr_name",
				"hr_title",
				"hr_active",
			):
				if isinstance(detail.get(field), str):
					detail[field] = decode_text(detail[field])
			return detail
		finally:
			try:
				self.client.close_tab()
			except Exception:  # noqa: BLE001 - tab close is best effort
				pass

	def _merge_candidate(self, item: dict[str, Any], detail: dict[str, Any] | None, keyword: str) -> JobCandidate:
		detail = detail or {}
		path = str(item.get("url") or "")
		full_url = path if path.startswith("http") else "https://www.zhipin.com" + path
		return JobCandidate(
			platform="boss",
			source_job_id=job_id_from_url(full_url),
			title=detail.get("title") or item.get("title") or "",
			company=detail.get("company") or item.get("company") or "",
			salary=detail.get("salary") or item.get("salary") or "",
			city=item.get("city") or "",
			city_code=item.get("city_code") or "",
			experience=detail.get("experience") or item.get("experience") or "",
			education=detail.get("education") or item.get("education") or "",
			recruitment_type=detail.get("recruitment_type") or "unknown",
			jd=detail.get("jd") or "",
			hr_name=detail.get("hr_name") or "",
			hr_title=detail.get("hr_title") or "",
			hr_active=detail.get("hr_active") or "",
			company_size=detail.get("company_size") or "",
			company_industry=detail.get("company_industry") or "",
			url=full_url,
			source_keyword=keyword,
		)

	def _resolve_city_code(self, city: str) -> str:
		search_cfg = self.config.get("search", {})
		codes = search_cfg.get("city_codes", {}) if isinstance(search_cfg, dict) else {}
		if isinstance(codes, dict) and codes.get(city):
			return str(codes[city])
		code = CITY_CODES.get(city)
		if not code:
			raise WebBridgeError(f"未知城市：{city}（可在 config.yaml 的 search.city_codes 中补充编码）")
		return str(code)

	# -- public entry ----------------------------------------------------
	def collect(
		self,
		keywords: list[str],
		cities: list[str],
		*,
		pages: int = 1,
		details: int = 0,
		max_new: int = 20,
		dry_run: bool = False,
		use_filter: bool = True,
		progress: Callable[[str], None] | None = None,
		report_dir: Path | str = Path("data") / "webbridge-runs",
	) -> dict[str, Any]:
		pages = max(1, min(int(pages or 1), MAX_PAGES))
		details = max(0, min(int(details or 0), MAX_DETAILS))
		max_new = max(1, int(max_new or 1))
		keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
		cities = [str(city).strip() for city in cities if str(city).strip()]
		if not keywords or not cities:
			raise ValueError("至少需要一个关键词和一个城市")

		conn = get_db()
		stats: dict[str, Any] = {
			"started_at": datetime.now().isoformat(timespec="seconds"),
			"keywords": keywords,
			"cities": cities,
			"pages": pages,
			"details": details,
			"seen": 0,
			"new": 0,
			"duplicate": 0,
			"filtered": 0,
			"detail_requested": 0,
			"detail_failed": 0,
			"errors": [],
			"jobs": [],
		}
		detail_budget = details
		seen_ids: set[str] = set()
		stop_reason: str | None = None

		def note(message: str) -> None:
			if progress:
				progress(message)

		try:
			for keyword in keywords:
				if stop_reason:
					break
				for city in cities:
					if stop_reason:
						break
					note(f"组合：{keyword} / {city}")
					try:
						city_code = self._resolve_city_code(city)
					except WebBridgeError as exc:
						stats["errors"].append(str(exc))
						continue
					for page in range(1, pages + 1):
						if stop_reason:
							break
						note(f"  列表第 {page}/{pages} 页")
						try:
							items = self._collect_page(keyword, city, city_code, page)
						except WebBridgeError as exc:
							stats["errors"].append(f"{keyword}/{city} 第 {page} 页：{exc}")
							stop_reason = str(exc)
							break
						if not items:
							break
						for item in items:
							stats["seen"] += 1
							candidate = self._merge_candidate(item, None, keyword)
							if candidate.storage_id in seen_ids:
								stats["duplicate"] += 1
								continue
							seen_ids.add(candidate.storage_id)
							if job_exists(conn, candidate.storage_id):
								stats["duplicate"] += 1
								continue
							detail_failed = False
							if detail_budget > 0:
								detail_budget -= 1
								stats["detail_requested"] += 1
								note(f"    详情：{candidate.title[:24]}")
								try:
									detail = self._collect_detail(candidate.url)
									candidate = self._merge_candidate(item, detail, keyword)
									self._human_sleep(*DEFAULT_DETAIL_DELAY)
								except Exception as exc:  # noqa: BLE001
									detail_failed = True
									stats["detail_failed"] += 1
									stats["errors"].append(f"详情 {candidate.url}：{exc}")
							if detail_failed and details > 0:
								stats["filtered"] += 1
								stats["jobs"].append(
									{
										"title": candidate.title,
										"company": candidate.company,
										"action": "detail_failed",
									}
								)
								continue
							if use_filter:
								score, reason = quick_score(
									{
										"title": candidate.title,
										"company": candidate.company,
										"salary": candidate.salary,
										"jd": candidate.jd,
									},
									self.config,
								)
								if score == 0:
									stats["filtered"] += 1
									stats["jobs"].append(
										{
											"title": candidate.title,
											"company": candidate.company,
											"action": "filtered",
											"reason": reason,
										}
									)
									continue
							if dry_run:
								stats["new"] += 1
								stats["jobs"].append(
									{
										"title": candidate.title,
										"company": candidate.company,
										"salary": candidate.salary,
										"city": candidate.city,
										"url": candidate.url,
										"id": candidate.storage_id,
										"action": "dry-run",
									}
								)
							else:
								try:
									inserted = insert_job(conn, candidate.as_job_record(), rules=self._rule_cache)
								except Exception as exc:  # noqa: BLE001
									stats["errors"].append(f"保存 {candidate.url}：{exc}")
									continue
								if inserted:
									stats["new"] += 1
									stats["jobs"].append(
										{
											"title": candidate.title,
											"company": candidate.company,
											"salary": candidate.salary,
											"city": candidate.city,
											"url": candidate.url,
											"id": candidate.storage_id,
										}
									)
								else:
									stats["duplicate"] += 1
							if stats["new"] >= max_new:
								stop_reason = f"达到本次新增上限 {max_new}"
								break
						if stop_reason:
							break
						if page < pages:
							self._human_sleep(*DEFAULT_PAGE_DELAY)
		finally:
			conn.close()

		stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
		stats["stop_reason"] = stop_reason
		try:
			out_dir = Path(report_dir)
			out_dir.mkdir(parents=True, exist_ok=True)
			out = out_dir / f"collect-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
			out.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
			stats["report"] = str(out)
		except OSError as exc:
			stats["errors"].append(f"运行报告写入失败：{exc}")
		return stats


def diagnose(client: WebBridgeClient) -> dict[str, Any]:
	"""Return a small health check for the daemon and the active tab."""
	result: dict[str, Any] = {"url": client.url, "session": client.session}
	tabs = client.list_tabs()
	result["tabs"] = tabs
	try:
		state = client.evaluate(
			"JSON.stringify({url: location.href, title: document.title, "
			"cards: document.querySelectorAll('.job-card-wrap').length, "
			"login: /\\/web\\/user\\/login/.test(location.href)})"
		)
		result["page"] = json.loads(str(state or "{}"))
	except Exception as exc:  # noqa: BLE001
		result["error"] = str(exc)
	return result
