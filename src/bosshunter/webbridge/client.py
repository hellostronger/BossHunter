"""Kimi WebBridge client.

Kimi WebBridge (a.k.a. Kimi 浏览器插件) is a Chrome extension plus a local
daemon. The daemon exposes a tiny JSON API on ``127.0.0.1:10086``::

    POST /command
    {"action": "evaluate", "args": {"code": "document.title"}, "session": "bosshunter"}

Every response looks like ``{"ok": true, "data": {"type": "string", "value": ...}}``
or ``{"ok": false, "error": "..."}``.

The extension performs actions inside the user's real, already logged-in
browser, so page views happen at a human pace and do not consume the page
budget BossHunter tracks for its own CDP collector.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx


DEFAULT_COMMAND_URL = "http://127.0.0.1:10086/command"


class WebBridgeError(RuntimeError):
	"""Raised when the daemon is unreachable or rejects a command."""


class WebBridgeClient:
	def __init__(
		self,
		url: str = DEFAULT_COMMAND_URL,
		session: str = "bosshunter",
		timeout: float = 60.0,
	) -> None:
		self.url = str(url or DEFAULT_COMMAND_URL)
		self.session = str(session or "bosshunter")
		self.timeout = float(timeout)

	def command(
		self,
		action: str,
		args: dict[str, Any] | None = None,
		*,
		timeout: float | None = None,
		retries: int = 1,
	) -> Any:
		"""Send one command and return ``data`` after checking ``ok``."""
		payload = {"action": action, "args": args or {}, "session": self.session}
		last_error: Exception | None = None
		for attempt in range(retries + 1):
			try:
				with httpx.Client(timeout=timeout or self.timeout) as client:
					response = client.post(self.url, json=payload)
				response.raise_for_status()
				body = response.json()
				break
			except Exception as exc:  # noqa: BLE001 - normalized below
				last_error = exc
				if attempt >= retries:
					raise WebBridgeError(f"WebBridge 调用失败：{exc}") from exc
				time.sleep(1.5 * (attempt + 1))
		else:  # pragma: no cover - loop always breaks or raises
			raise WebBridgeError(f"WebBridge 调用失败：{last_error}")

		if not isinstance(body, dict):
			raise WebBridgeError("WebBridge 返回了非 JSON 对象")
		if not body.get("ok"):
			message = body.get("error") or body.get("message") or body
			raise WebBridgeError(f"WebBridge 执行失败：{message}")
		return body.get("data") or {}

	def evaluate(self, code: str, *, timeout: float | None = None) -> Any:
		"""Evaluate JavaScript in the current tab and return its value."""
		data = self.command("evaluate", {"code": code}, timeout=timeout or max(self.timeout, 120.0))
		if isinstance(data, dict):
			return data.get("value")
		return data

	def evaluate_json(self, code: str, *, timeout: float | None = None) -> Any:
		"""Evaluate JS that returns ``JSON.stringify(...)`` and parse the value."""
		value = self.evaluate(code, timeout=timeout)
		if value in (None, ""):
			return None
		if isinstance(value, (dict, list)):
			return value
		return json.loads(str(value))

	def navigate(self, url: str, *, new_tab: bool = False, timeout: float | None = None) -> Any:
		args: dict[str, Any] = {"url": url}
		if new_tab:
			args["newTab"] = True
		return self.command("navigate", args, timeout=timeout or max(self.timeout, 90.0))

	def close_tab(self) -> Any:
		return self.command("close_tab", {}, retries=0)

	def list_tabs(self) -> Any:
		return self.command("list_tabs", {}, timeout=min(self.timeout, 20.0), retries=0)

	def screenshot(self, path: str) -> Any:
		return self.command("screenshot", {"path": path})
