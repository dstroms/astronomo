"""Model-agnostic LLM client.

One class, :class:`LLMClient`, reads ``LLM_PROVIDER`` / ``LLM_MODEL_NAME`` / ``LLM_BASE_URL``
(plus a few budget knobs) from :mod:`config` and talks to:

* ``ollama``     -- a local Ollama server (``/api/chat``), default ``http://localhost:11434``
* ``openai``     -- OpenAI, or any OpenAI-compatible server via ``LLM_BASE_URL``
                    (vLLM, LM Studio, llama.cpp, Ollama's ``/v1`` ...)
* ``anthropic``  -- Anthropic Messages API
* ``none``       -- disabled; nothing is ever sent (the default)

Graph nodes call only :meth:`LLMClient.ask`, so switching provider changes configuration,
never node logic.  Plain ``requests`` is used on purpose: no vendor SDKs to install, and the
wire format of each provider is a few lines in one adapter class.

Token-waste controls (all enforced here, not left to callers)
-------------------------------------------------------------
* **Data firewall.**  ``ask`` renders structured data through :mod:`core.digest`, which rejects
  arrays, long numeric runs, FITS dumps and oversize payloads *before* any network call.
* **Hard caps** on prompt size (characters) and completion size (tokens).
* **Per-mission budget** on calls and tokens, checked *before* sending (including a worst-case
  estimate of the request about to be made) and persisted via the DB ledger so it survives
  restarts.
* **Response cache** keyed on the exact request, so a node re-run after a crash costs nothing.
* **Retries only for transient failures** (timeouts, connection errors, 429/5xx).  Client
  errors (bad model, bad key) fail immediately.
* **Ledger.**  Every call -- including failures and cache hits -- is recorded with token counts
  and the exact prompt, so "what did we send, and what did it cost?" is always answerable.

The model's output is *advisory prose only*.  Nothing in Astronomo lets it gate weather,
hardware, detections or classifications.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from config import Settings
from core.digest import LLMPayloadError, PayloadLimits, assert_text_safe, render_payload
from db.database import summarise_llm_calls

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Base class for language-model failures (recoverable unless ``llm_strict``)."""


class LLMConfigError(LLMError):
    """Provider/model/key configuration is unusable."""


class LLMUnavailable(LLMError):
    """The endpoint could not be reached or kept failing after retries."""


class LLMBudgetExceeded(LLMError):
    """The per-mission call or token budget would be exceeded."""


#: What graph nodes catch.  ``LLMPayloadError`` = a plugin tried to send bulk data.
LLM_ERRORS = (LLMError, LLMPayloadError)

PROVIDERS = ("none", "ollama", "openai", "anthropic")
DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
DEFAULT_OLLAMA_MODEL = "llama3.1"
ANTHROPIC_VERSION = "2023-06-01"
_API_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_RETRY_DELAYS_S = (1.0, 3.0)


def estimate_tokens(chars: int) -> int:
    """Deliberately pessimistic (JSON-heavy text is ~3 chars/token)."""
    return chars // 3 + 1


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cached: bool = False
    estimated: bool = False  # token counts estimated because the server did not report them
    truncated: bool = False  # hit the output-token cap (text was cut back to a full sentence)
    prompt_chars: int = 0
    data_json: str = ""  # the exact data payload that was sent (for grounding checks)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# --------------------------------------------------------------------------- #
# Provider adapters: build the HTTP request, parse the reply.  Nothing else.
# --------------------------------------------------------------------------- #
@dataclass
class _Parsed:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    truncated: bool


class _Adapter:
    #: Context window the server will actually use (to detect silent prompt truncation).
    context_limit: int | None = None

    def __init__(self, model: str, base_url: str, api_key: str | None, settings: Settings):
        self.model, self.base_url, self.api_key, self.settings = model, base_url.rstrip("/"), api_key, settings

    def request(self, system: str, prompt: str, max_tokens: int, temperature: float | None) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def parse(self, data: dict) -> _Parsed:
        raise NotImplementedError


class _OllamaAdapter(_Adapter):
    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.context_limit = self.settings.llm_num_ctx

    def request(self, system, prompt, max_tokens, temperature):
        options: dict[str, Any] = {"num_predict": max_tokens, "num_ctx": self.settings.llm_num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "options": options,
        }
        return f"{self.base_url}/api/chat", {"Content-Type": "application/json"}, body

    def parse(self, data):
        if data.get("error"):
            raise LLMError(f"Ollama error: {data['error']}")
        message = data.get("message") or {}
        return _Parsed(
            text=message.get("content") or "",
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            truncated=data.get("done_reason") == "length",
        )


class _OpenAIAdapter(_Adapter):
    def request(self, system, prompt, max_tokens, temperature):
        official = urlparse(self.base_url).hostname == "api.openai.com"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            # OpenAI's newer models reject ``max_tokens``; most compatible servers only know it.
            ("max_completion_tokens" if official else "max_tokens"): max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return f"{self.base_url}/chat/completions", headers, body

    def parse(self, data):
        if data.get("error"):
            err = data["error"]
            raise LLMError(f"API error: {err.get('message') if isinstance(err, dict) else err}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("unexpected response: no 'choices'")
        usage = data.get("usage") or {}
        return _Parsed(
            text=(choices[0].get("message") or {}).get("content") or "",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            truncated=choices[0].get("finish_reason") == "length",
        )


class _AnthropicAdapter(_Adapter):
    def request(self, system, prompt, max_tokens, temperature):
        base = self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return f"{base}/v1/messages", headers, body

    def parse(self, data):
        if data.get("type") == "error" or data.get("error"):
            err = data.get("error") or {}
            raise LLMError(f"API error: {err.get('message', err) if isinstance(err, dict) else err}")
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        usage = data.get("usage") or {}
        return _Parsed(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            truncated=data.get("stop_reason") == "max_tokens",
        )


_ADAPTERS = {"ollama": _OllamaAdapter, "openai": _OpenAIAdapter, "anthropic": _AnthropicAdapter}


def _clean_text(text: str, truncated: bool) -> str:
    """Strip reasoning-model ``<think>`` blocks; cut a length-truncated reply back to a sentence."""
    text = _THINK_BLOCK.sub("", text)
    if "<think>" in text.lower():  # unterminated: the model ran out of tokens while thinking
        text = text[: text.lower().index("<think>")]
    text = text.strip()
    if truncated and text and text[-1] not in ".!?":
        cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
        if cut > 0:
            text = text[: cut + 1]
    return text


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(
        self,
        settings: Settings,
        db: Any = None,
        *,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.db = db
        self.sleep_fn = sleep_fn
        self.session = session or requests.Session()
        self.provider = (settings.llm_provider or "none").strip().lower()
        if self.provider not in PROVIDERS:
            raise LLMConfigError(f"unknown LLM_PROVIDER '{self.provider}' (choose from {', '.join(PROVIDERS)})")
        self.enabled = self.provider != "none"
        self.model = settings.llm_model_name
        self.base_url = ""
        self._adapter: _Adapter | None = None
        self._send_temperature = True
        self._memory: list[dict] = []  # ledger used when no DB is attached
        if self.enabled:
            self._configure()

    # -- configuration ------------------------------------------------------ #
    def _configure(self) -> None:
        s = self.settings
        self.base_url = (s.llm_base_url or DEFAULT_BASE_URLS[self.provider]).rstrip("/")
        parsed = urlparse(self.base_url)
        # NB: urlparse("localhost:11434") reports scheme="localhost", so check for http(s) explicitly.
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LLMConfigError(f"LLM_BASE_URL must look like http://host:port (got '{self.base_url}')")
        self.model = s.llm_model_name or (DEFAULT_OLLAMA_MODEL if self.provider == "ollama" else "")
        if not self.model:
            raise LLMConfigError(f"LLM_PROVIDER={self.provider} needs LLM_MODEL_NAME (e.g. set ASTRONOMO_LLM_MODEL_NAME)")
        key = s.llm_api_key or os.environ.get(_API_KEY_ENV.get(self.provider, ""), "") or None
        custom_endpoint = self.base_url != DEFAULT_BASE_URLS[self.provider]
        if self.provider in _API_KEY_ENV and not key and not custom_endpoint:
            raise LLMConfigError(
                f"LLM_PROVIDER={self.provider} needs an API key: set {_API_KEY_ENV[self.provider]} (or ASTRONOMO_LLM_API_KEY)"
            )
        if s.llm_max_input_chars < 500:
            raise LLMConfigError("LLM_MAX_INPUT_CHARS is too small to be useful (< 500)")
        self._adapter = _ADAPTERS[self.provider](self.model, self.base_url, key, s)

    def describe(self) -> str:
        return "disabled" if not self.enabled else f"{self.provider}:{self.model} @ {self.base_url}"

    # -- public API ----------------------------------------------------------- #
    def ask(
        self,
        task: str,
        data: Any = None,
        *,
        system: str,
        mission_id: str | None = None,
        node: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send ``task`` plus an optional structured ``data`` digest and return the reply.

        ``data`` is rendered through the data firewall (:func:`core.digest.render_payload`);
        a violation raises :class:`~core.digest.LLMPayloadError` and *nothing is sent*.
        """
        if not self.enabled:
            raise LLMError("LLM is disabled (LLM_PROVIDER=none)")
        limits = self.settings.llm_max_input_chars
        overhead = len(system) + len(task) + 40
        if overhead >= limits:
            raise LLMConfigError("system prompt + task alone exceed LLM_MAX_INPUT_CHARS")
        try:
            data_json = render_payload(data, PayloadLimits(max_chars=limits - overhead)) if data is not None else ""
        except LLMPayloadError as exc:
            # A blocked leak is exactly what the ledger should show: record it, send nothing.
            self._record(mission_id, node, prompt=f"[BLOCKED before sending] {task.strip()}"[:2000], response="", resp=None,
                         ok=False, error=str(exc)[:1000], latency_s=0.0)
            raise
        prompt = f"{task.strip()}\n\nDATA (JSON):\n{data_json}" if data is not None else task.strip()
        return self._complete(system, prompt, data_json, mission_id, node, max_tokens, use_cache=self.settings.llm_cache)

    def check(self) -> dict:
        """Verify connectivity and the model with a tiny real request.  Raises :class:`LLMError`.

        For Ollama this first lists installed models (free) to give an actionable error if the
        model has not been pulled.  The ping itself is capped at 64 completion tokens (a few
        tokens' worth of cost on a paid API).
        """
        if not self.enabled:
            raise LLMError("LLM is disabled (LLM_PROVIDER=none): nothing to check")
        info: dict[str, Any] = {"provider": self.provider, "model": self.model, "base_url": self.base_url}
        if self.provider == "ollama":
            installed = self._ollama_models()
            info["installed_models"] = installed
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            if self.model not in installed and wanted not in installed:
                raise LLMError(
                    f"Ollama is running but model '{self.model}' is not installed "
                    f"(installed: {', '.join(installed) or 'none'}). Run: ollama pull {self.model}"
                )
        reply = self._complete(
            "Reply with exactly one word.", "Say: OK", "", None, "llm_check", 64, use_cache=False
        )
        info.update(reply=reply.text[:40], prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
                    latency_s=round(reply.latency_s, 2))
        return info

    def usage(self, mission_id: str) -> dict:
        """Calls/tokens used by ``mission_id`` (from the DB ledger when attached)."""
        if self.db is not None:
            return self.db.llm_usage(mission_id)
        return summarise_llm_calls([r for r in self._memory if r.get("mission_id") == mission_id])

    # -- internals ---------------------------------------------------------- #
    def _ollama_models(self) -> list[str]:
        try:
            resp = self.session.get(f"{self.base_url}/api/tags", timeout=(5, 15))
        except requests.RequestException as exc:
            raise LLMUnavailable(self._unreachable(exc)) from exc
        if resp.status_code != 200:
            raise LLMError(f"Ollama /api/tags returned HTTP {resp.status_code}")
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def _unreachable(self, exc: Exception) -> str:
        hint = " Is Ollama running? Start it with `ollama serve`." if self.provider == "ollama" else ""
        return f"cannot reach {self.base_url}: {type(exc).__name__}{hint}"

    def _cache_path(self, key: str) -> Path:
        return self.settings.llm_cache_dir / f"{key}.json"

    def _cache_key(self, system: str, prompt: str, max_tokens: int) -> str:
        blob = json.dumps(
            [self.provider, self.model, self.base_url, system, prompt, max_tokens, self.settings.llm_temperature,
             self.settings.llm_num_ctx if self.provider == "ollama" else None],
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _record(self, mission_id: str | None, node: str | None, *, prompt: str, response: str, resp: LLMResponse | None,
                ok: bool, error: str | None, latency_s: float) -> None:
        row = {
            "mission_id": mission_id, "node": node, "provider": self.provider, "model": self.model,
            "prompt_tokens": resp.prompt_tokens if resp else 0,
            "completion_tokens": resp.completion_tokens if resp else 0,
            "prompt_chars": len(prompt), "response_chars": len(response), "latency_s": latency_s,
            "cached": bool(resp and resp.cached), "estimated": bool(resp and resp.estimated),
            "ok": ok, "error": error, "prompt": prompt, "response": response,
        }
        self._memory.append(row)
        if self.db is not None and mission_id:
            try:
                self.db.add_llm_call(**row)
            except Exception:  # noqa: BLE001 - the ledger must never take the mission down
                log.debug("failed to persist LLM ledger row", exc_info=True)

    def _check_budget(self, mission_id: str | None, prompt_chars: int, max_tokens: int) -> None:
        if not mission_id:
            return
        s = self.settings
        used = self.usage(mission_id)
        if used["calls"] >= s.llm_max_calls_per_mission:
            raise LLMBudgetExceeded(f"mission already made {used['calls']} LLM call(s) (limit {s.llm_max_calls_per_mission})")
        worst_case = used["total_tokens"] + estimate_tokens(prompt_chars) + max_tokens
        if worst_case > s.llm_max_tokens_per_mission:
            raise LLMBudgetExceeded(
                f"token budget: {used['total_tokens']} used + up to {estimate_tokens(prompt_chars) + max_tokens} "
                f"for this call exceeds {s.llm_max_tokens_per_mission}"
            )

    def _complete(self, system: str, prompt: str, data_json: str, mission_id: str | None, node: str | None,
                  max_tokens: int | None, *, use_cache: bool) -> LLMResponse:
        assert self._adapter is not None
        # LLM_MAX_OUTPUT_TOKENS is a hard ceiling; callers can only ask for less.
        cap = max(1, min(int(max_tokens or self.settings.llm_max_output_tokens), self.settings.llm_max_output_tokens))
        full = f"{system}\n{prompt}"
        try:
            assert_text_safe(full, self.settings.llm_max_input_chars)
        except LLMPayloadError as exc:
            self._record(mission_id, node, prompt=prompt[:2000], response="", resp=None, ok=False, error=str(exc), latency_s=0.0)
            raise

        key = self._cache_key(system, prompt, cap)
        if use_cache:
            hit = self._read_cache(key)
            if hit is not None:
                resp = LLMResponse(hit["text"], self.provider, self.model, 0, 0, 0.0, cached=True,
                                   prompt_chars=len(full), data_json=data_json)
                self._record(mission_id, node, prompt=prompt, response=resp.text, resp=resp, ok=True, error=None, latency_s=0.0)
                log.info("LLM [%s] cache hit (%d chars), 0 tokens spent", node or "-", len(resp.text))
                return resp

        try:
            self._check_budget(mission_id, len(full), cap)
        except LLMBudgetExceeded as exc:
            self._record(mission_id, node, prompt=prompt, response="", resp=None, ok=False, error=str(exc), latency_s=0.0)
            raise

        started = time.monotonic()
        try:
            parsed = self._post_with_retries(system, prompt, cap)
        except LLMError as exc:
            self._record(mission_id, node, prompt=prompt, response="", resp=None, ok=False, error=str(exc),
                         latency_s=time.monotonic() - started)
            raise
        latency = time.monotonic() - started

        text = _clean_text(parsed.text, parsed.truncated)
        estimated = parsed.prompt_tokens is None or parsed.completion_tokens is None
        p_tok = parsed.prompt_tokens if parsed.prompt_tokens is not None else estimate_tokens(len(full))
        c_tok = parsed.completion_tokens if parsed.completion_tokens is not None else estimate_tokens(len(parsed.text))
        resp = LLMResponse(text, self.provider, self.model, int(p_tok), int(c_tok), latency, estimated=estimated,
                           truncated=parsed.truncated, prompt_chars=len(full), data_json=data_json)

        limit = self._adapter.context_limit
        if limit and parsed.prompt_tokens is not None and parsed.prompt_tokens >= limit:
            err = (f"prompt used {parsed.prompt_tokens} tokens, filling the {limit}-token context window; the server "
                   f"truncates silently. Raise ASTRONOMO_LLM_NUM_CTX or shrink the data summary.")
            self._record(mission_id, node, prompt=prompt, response=text, resp=resp, ok=False, error=err, latency_s=latency)
            raise LLMError(err)
        if not text:
            err = "model returned no usable text (empty reply, or it spent the whole token cap on <think> reasoning)"
            self._record(mission_id, node, prompt=prompt, response="", resp=resp, ok=False, error=err, latency_s=latency)
            raise LLMError(err)

        self._record(mission_id, node, prompt=prompt, response=text, resp=resp, ok=True, error=None, latency_s=latency)
        if use_cache:
            self._write_cache(key, {"text": text})
        log.info("LLM [%s] %s: %d prompt + %d completion tokens%s in %.1fs", node or "-", self.describe(), resp.prompt_tokens,
                 resp.completion_tokens, " (estimated)" if estimated else "", latency)
        return resp

    def _post_with_retries(self, system: str, prompt: str, max_tokens: int) -> _Parsed:
        assert self._adapter is not None
        temperature: float | None = self.settings.llm_temperature if self._send_temperature else None
        last: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            url, headers, body = self._adapter.request(system, prompt, max_tokens, temperature)
            try:
                resp = self.session.post(url, headers=headers, json=body, timeout=(10, self.settings.llm_timeout_s))
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = LLMUnavailable(self._unreachable(exc) if isinstance(exc, requests.ConnectionError)
                                      else f"timed out after {self.settings.llm_timeout_s:.0f}s waiting for {self.base_url}")
            except requests.RequestException as exc:
                raise LLMError(f"request failed: {type(exc).__name__}: {exc}") from exc
            else:
                if resp.status_code == 200:
                    try:
                        return self._adapter.parse(resp.json())
                    except ValueError as exc:
                        raise LLMError(f"invalid JSON from {self.base_url}: {exc}") from exc
                detail = _error_detail(resp)
                if resp.status_code == 400 and temperature is not None and "temperature" in detail.lower():
                    log.info("Model rejects the temperature parameter; retrying once without it")
                    self._send_temperature = False
                    temperature = None
                    continue
                if resp.status_code in _TRANSIENT_STATUS:
                    last = LLMUnavailable(f"HTTP {resp.status_code} from {self.base_url}: {detail}")
                else:
                    raise LLMError(self._http_error(resp.status_code, detail))
            if attempt < len(_RETRY_DELAYS_S):
                log.warning("LLM request failed (%s); retry %d/%d", last, attempt + 1, len(_RETRY_DELAYS_S))
                self.sleep_fn(_RETRY_DELAYS_S[attempt])
        raise last or LLMError("request failed")

    def _http_error(self, status: int, detail: str) -> str:
        if status == 404 and self.provider == "ollama":
            return f"HTTP 404: {detail}. If the model is missing, run: ollama pull {self.model}"
        if status in (401, 403):
            return f"HTTP {status}: authentication failed ({detail}); check the API key"
        return f"HTTP {status} from {self.base_url}: {detail}"

    # -- cache ---------------------------------------------------------------- #
    def _read_cache(self, key: str) -> dict | None:
        try:
            return json.loads(self._cache_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cache(self, key: str, payload: dict) -> None:
        try:
            path = self._cache_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.debug("could not write LLM cache", exc_info=True)


def _error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        text = err.get("message", err) if isinstance(err, dict) else err
        return str(text)[:300]
    except ValueError:
        return resp.text[:300]


def format_usage(usage: Mapping[str, Any]) -> str:
    return (
        f"{usage['calls']} call(s), {usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion tokens, "
        f"{usage['cached_calls']} cache hit(s), {usage['failed_calls']} failed, largest prompt {usage['max_prompt_chars']} chars"
    )


def build_llm_client(settings: Settings, db: Any = None, **kw: Any) -> LLMClient:
    """Factory used by ``main.py``; raises :class:`LLMConfigError` on unusable config."""
    return LLMClient(settings, db, **kw)
