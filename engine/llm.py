"""AI router: free providers first (Groq, Gemini, OpenRouter), optional paid Claude.

Every provider except Anthropic speaks the OpenAI "chat completions" format, so one client
covers them all. For each job ("triage", "writer", "verify") config.yaml lists models in order
of preference; the router skips models without an API key or over their free daily limit,
paces requests to stay under per-minute limits, and falls back to the next model on errors.
"""
import json
import logging
import os
import re
import time
from collections import deque

import requests

log = logging.getLogger("engine.llm")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class BudgetExceeded(RuntimeError):
    pass


class NoModelAvailable(RuntimeError):
    pass


def est_tokens(*texts: str) -> int:
    return int(sum(len(t) for t in texts) / 3.6) + 50


class Router:
    def __init__(self, llm_cfg: dict, store, daily_max_usd: float = 0.0, env=None, sleep=None, clock=None):
        self.cfg = llm_cfg
        self.store = store
        self.daily_max_usd = daily_max_usd
        self.env = env if env is not None else os.environ
        self.sleep = sleep or (lambda sec: time.sleep(sec))
        self.clock = clock or (lambda: time.time())
        self.window: dict[str, deque] = {}   # model key -> deque[(t, tokens)]
        self.dead: set[str] = set()          # models exhausted or failing during this run

    # ---------------------------------------------------------------- helpers
    def _model(self, key: str) -> dict:
        m = dict(self.cfg["models"][key])
        p = self.cfg["providers"][m["provider"]]
        m.setdefault("type", p.get("type", "openai"))
        m["base_url"] = p.get("base_url", "")
        m["api_key"] = self.env.get(p.get("key_env", ""), "")
        m["key"] = key
        return m

    def available(self, role: str | None = None) -> list[str]:
        roles = [role] if role else list(self.cfg["roles"].keys())
        out = []
        for r in roles:
            for k in self.cfg["roles"].get(r, []):
                if k in self.cfg["models"] and self._model(k)["api_key"] and k not in out:
                    out.append(k)
        return out

    def _over_daily(self, m: dict) -> bool:
        reqs, toks = self.store.usage_today(m["key"])
        if m.get("rpd") and reqs >= m["rpd"]:
            return True
        if m.get("tpd") and toks >= m["tpd"]:
            return True
        return False

    def _pace(self, m: dict, tokens: int) -> bool:
        """Wait until this request fits the per-minute limits. False if it can never fit."""
        tpm, rpm = m.get("tpm"), m.get("rpm")
        if tpm and tokens > tpm:
            return False
        q = self.window.setdefault(m["key"], deque())
        waited = 0.0
        while True:
            now = self.clock()
            while q and now - q[0][0] >= 60:
                q.popleft()
            used = sum(t for _, t in q)
            if (not rpm or len(q) < rpm) and (not tpm or used + tokens <= tpm):
                q.append((now, tokens))
                return True
            wait = max(1.0, 60 - (now - q[0][0]) + 0.5)
            if waited + wait > 120:
                return False
            log.info("pacing %s: waiting %.0fs for per-minute limit", m["key"], wait)
            self.sleep(wait)
            waited += wait

    def _cost(self, m: dict, inp: int, out: int) -> float:
        return inp / 1e6 * float(m.get("price_in", 0)) + out / 1e6 * float(m.get("price_out", 0))

    # ---------------------------------------------------------------- calls
    def _call(self, m: dict, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
        if m["type"] == "anthropic":
            body = {"model": m["model"], "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
            r = requests.post(ANTHROPIC_URL, json=body, timeout=180, headers={
                "x-api-key": m["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
            self._raise(r)
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            u = d.get("usage", {})
            return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        if m["type"] == "gemini":
            # Gemini's own endpoint: works with the new "AQ." keys (sent as x-goog-api-key, nothing else).
            url = f"{m['base_url'].rstrip('/')}/models/{m['model']}:generateContent"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens}}
            body.update(m.get("extra") or {})
            r = requests.post(url, json=body, timeout=180,
                              headers={"x-goog-api-key": m["api_key"], "Content-Type": "application/json"})
            self._raise(r)
            d = r.json()
            cand = (d.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
            u = d.get("usageMetadata") or {}
            return text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))
        body = {"model": m["model"], "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        body.update(m.get("extra") or {})
        headers = {"Authorization": f"Bearer {m['api_key']}", "Content-Type": "application/json"}
        if "openrouter.ai" in m["base_url"]:
            headers["HTTP-Referer"] = "https://press.autos"
            headers["X-Title"] = "press.autos engine"
        r = requests.post(m["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers, timeout=180)
        self._raise(r)
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = d.get("usage") or {}
        return text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))

    @staticmethod
    def _raise(r):
        if r.status_code >= 400:
            err = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            err.status = r.status_code
            raise err

    def ask(self, role: str, system: str, user: str, max_tokens: int = 3000) -> str:
        if self.daily_max_usd and self.store.spend_today() >= self.daily_max_usd:
            raise BudgetExceeded(f"daily budget ${self.daily_max_usd} reached")
        tried = []
        for key in self.cfg["roles"].get(role, []):
            if key in self.dead or key not in self.cfg["models"]:
                continue
            m = self._model(key)
            if not m["api_key"]:
                continue
            if self._over_daily(m):
                tried.append(f"{key}: daily limit used")
                continue
            need = est_tokens(system, user) + min(max_tokens, m.get("expect_out", 1500))
            if not self._pace(m, need):
                tried.append(f"{key}: request too large for its per-minute limit")
                continue
            for attempt in range(2):
                try:
                    text, inp, out = self._call(m, system, user, max_tokens)
                    self.store.add_spend(key, inp or need, out, self._cost(m, inp, out))
                    if not text.strip():
                        raise RuntimeError("empty reply")
                    return text
                except Exception as exc:  # noqa: BLE001
                    status = getattr(exc, "status", None)
                    msg = str(exc).lower()
                    if status == 429 and attempt == 0 and "day" not in msg and "quota" not in msg:
                        self.sleep(20)
                        continue
                    if status in (401, 403, 404) or status == 429 or "quota" in msg:
                        self.dead.add(key)
                    tried.append(f"{key}: {exc}"[:160])
                    log.warning("model %s failed: %s", key, exc)
                    break
        raise NoModelAvailable(f"no model could handle '{role}': " + "; ".join(tried or ["no API keys set"]))

    def ask_json(self, role: str, system: str, user: str, max_tokens: int = 3000):
        sys2 = system + "\n\nReply with valid JSON only. No commentary, no markdown fences."
        text = self.ask(role, sys2, user, max_tokens)
        try:
            return parse_json(text)
        except (json.JSONDecodeError, ValueError):
            text = self.ask(role, sys2, user + "\n\nIMPORTANT: your previous reply was not valid JSON. Return only the JSON.", max_tokens)
            return parse_json(text)


def parse_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # some open models print their reasoning
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            return json.loads(text[start:end + 1])
        raise
