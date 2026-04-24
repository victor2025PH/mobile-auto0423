"""
Unified LLM Client — provider-agnostic interface for text + vision models.

Supports:
- DeepSeek (default, cheapest for Chinese + English)
- OpenAI-compatible APIs (GPT-4o, local vLLM, Ollama, etc.)
- Automatic retry with exponential backoff
- Token usage tracking for cost monitoring
- Response caching (SHA256 key → SQLite)

Design: All AI modules (MessageRewriter, AutoReply, VisionFallback) use this
single client. Switch providers by changing config, not code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import yaml
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.host.device_registry import config_file, data_file
from src.openclaw_env import local_api_base

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_sec: float = 30.0
    max_retries: int = 3
    cache_enabled: bool = True
    cache_db_path: str = ""

    def __post_init__(self):
        if not self.api_key:
            env_map = {
                "deepseek": "DEEPSEEK_API_KEY",
                "openai": "OPENAI_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "zhipu": "ZHIPU_API_KEY",
                "ollama": "",
            }
            env_var = env_map.get(self.provider, "")
            self.api_key = os.environ.get(env_var, "") if env_var else ""
            if not self.api_key:
                self.api_key = os.environ.get("ZHIPU_API_KEY", "") or \
                               os.environ.get("DEEPSEEK_API_KEY", "") or \
                               os.environ.get("OPENAI_API_KEY", "")

        if not self.base_url:
            providers = {
                "deepseek": "https://api.deepseek.com/v1",
                "openai": "https://api.openai.com/v1",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
                "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                "ollama": "http://localhost:11434/v1",
                "local": f"{local_api_base('localhost')}/v1",
            }
            self.base_url = providers.get(self.provider, self.base_url)

        if not self.model:
            models = {
                "deepseek": "deepseek-chat",
                "openai": "gpt-4o-mini",
                "gemini": "gemini-2.5-flash",
                "zhipu": "glm-4-flash",
                "ollama": "llava:7b",
                "local": "default",
            }
            self.model = models.get(self.provider, "default")

        if not self.vision_model:
            vision = {
                "deepseek": "deepseek-chat",
                "openai": "gpt-4o",
                "gemini": "gemini-2.5-flash",
                "zhipu": "glm-4v-flash",
                "ollama": "llava:7b",
                "local": "default",
            }
            self.vision_model = vision.get(self.provider, "default")

        if not self.cache_db_path:
            self.cache_db_path = str(data_file("llm_cache.db"))


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

@dataclass
class UsageStats:
    total_calls: int = 0
    cached_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    errors: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, input_tokens: int, output_tokens: int, cached: bool = False):
        with self._lock:
            self.total_calls += 1
            if cached:
                self.cached_calls += 1
            else:
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                self.total_cost_usd += self._estimate_cost(input_tokens, output_tokens)

    def record_error(self):
        with self._lock:
            self.errors += 1

    @staticmethod
    def _estimate_cost(inp: int, out: int) -> float:
        # DeepSeek pricing: ~$0.14/M input, ~$0.28/M output (2025)
        return (inp * 0.14 + out * 0.28) / 1_000_000

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_calls": self.total_calls,
                "cached_calls": self.cached_calls,
                "cache_hit_rate": f"{self.cached_calls/max(1,self.total_calls)*100:.1f}%",
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "estimated_cost_usd": round(self.total_cost_usd, 4),
                "errors": self.errors,
            }


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Unified LLM client for all AI modules.

    Usage:
        client = LLMClient()
        response = client.chat("Rewrite this message in a friendly tone: ...")
        response = client.chat_with_system("You are a helpful assistant.", "Hello")
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self.stats = UsageStats()
        self._http = httpx.Client(
            timeout=self.config.timeout_sec,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._cache_lock = threading.Lock()
        # P5c (2026-04-24): 失败时记最后一次 HTTP error code + body 给 caller
        # 做 provider swap 决策 (e.g. VLM Gemini 503 → Ollama fallback)。
        # 成功 call 会清为 None/""; 只保留最后一次 retry 的 error 信息。
        self.last_error_code: Optional[int] = None
        self.last_error_body: str = ""
        if self.config.cache_enabled:
            self._init_cache()

    def close(self):
        self._http.close()

    # -- Core API -----------------------------------------------------------

    def chat(self, user_message: str, temperature: Optional[float] = None,
             max_tokens: Optional[int] = None, use_cache: bool = True) -> str:
        """Simple single-turn chat. Returns assistant message text."""
        return self.chat_with_system("", user_message, temperature, max_tokens, use_cache)

    def chat_with_system(self, system: str, user: str,
                         temperature: Optional[float] = None,
                         max_tokens: Optional[int] = None,
                         use_cache: bool = True) -> str:
        """Chat with system prompt. Returns assistant message text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return self.chat_messages(messages, temperature, max_tokens, use_cache)

    def chat_messages(self, messages: List[Dict[str, Any]],
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None,
                      use_cache: bool = True) -> str:
        """Full messages API (sync). Returns assistant message text.

        Note: Core AI routing is intentionally NOT here (sync would block event loop).
        Use chat_messages_async() from async FastAPI handlers for Core routing.
        """
        temp = temperature if temperature is not None else self.config.temperature
        tokens = max_tokens or self.config.max_tokens

        cache_key = self._cache_key(messages, temp) if use_cache else None
        if cache_key and self.config.cache_enabled:
            cached = self._get_cache(cache_key)
            if cached is not None:
                self.stats.record(0, 0, cached=True)
                return cached

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }

        response_text = self._call_api(payload)

        if cache_key and self.config.cache_enabled and response_text:
            self._set_cache(cache_key, response_text)

        return response_text

    async def chat_messages_async(self, messages: List[Dict[str, Any]],
                                  temperature: Optional[float] = None,
                                  max_tokens: Optional[int] = None,
                                  use_cache: bool = True) -> str:
        """
        P0-1 Fix: async版本，供FastAPI async路由使用。
        先异步尝试Core AI，失败后在线程池里运行本地同步LLM，不阻塞事件循环。
        """
        import asyncio

        # 1. 异步尝试 Core AI（不阻塞）
        core_reply = await self._try_core_chat_async(messages)
        if core_reply:
            return core_reply

        # 2. 本地LLM在线程池运行（不阻塞asyncio event loop）
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.chat_messages,
            messages, temperature, max_tokens, use_cache,
        )

    async def _try_core_chat_async(self, messages: List[Dict[str, Any]]) -> str:
        """P0-1: 异步Core AI路由，使用httpx.AsyncClient，不阻塞事件循环。"""
        core_url = os.environ.get("OPENCLAW_CORE_URL", "").rstrip("/")
        core_token = os.environ.get("OPENCLAW_CORE_TOKEN", "")
        if not core_url:
            return ""
        try:
            headers = {"Content-Type": "application/json"}
            if core_token:
                headers["Authorization"] = f"Bearer {core_token}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{core_url}/api/chat",
                    json={"messages": messages, "stream": False},
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                text = (
                    (choices[0].get("message", {}).get("content", "") if choices else "")
                    or data.get("reply", "")
                    or data.get("text", "")
                ).strip()
                if text:
                    log.debug("Core AI 路由成功 (%d chars)", len(text))
                    return text
        except Exception as e:
            log.debug("Core AI 路由失败，降级本地: %s", e)
        return ""

    def chat_vision(self, text_prompt: str, image_base64: str,
                    max_tokens: Optional[int] = None) -> str:
        """Vision API: send text + image, get response."""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_base64}"
                }},
            ],
        }]

        tokens = max_tokens or 256
        # Gemini 2.5 thinking models consume extra tokens for reasoning
        if "gemini" in self.config.provider and "2.5" in self.config.vision_model:
            tokens = max(tokens, 2048)

        payload = {
            "model": self.config.vision_model,
            "messages": messages,
            "max_tokens": tokens,
        }

        return self._call_api(payload)

    # -- HTTP with retry ----------------------------------------------------

    def _call_api(self, payload: dict) -> str:
        """HTTP call with retry. 2026-04-24 P5c: 失败时保留 last_error_code
        / last_error_body 供 caller debug + provider swap 决策 (e.g. VLM
        Gemini 503 → Ollama fallback 判定)。

        成功时 reset to None/""; 失败时写入最后一次 error 信息。返 "" 表示
        所有 retry 用完, caller 应看 last_error_code 判断根因。
        """
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"

        for attempt in range(self.config.max_retries):
            try:
                resp = self._http.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    usage = data.get("usage", {})
                    self.stats.record(
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                    )
                    # 成功 — 清 error state
                    self.last_error_code = None
                    self.last_error_body = ""
                    return text

                # 记 error (每次 retry 覆盖, 最终保留最后一次)
                self.last_error_code = resp.status_code
                self.last_error_body = resp.text[:500] if resp.text else ""

                if resp.status_code == 429:
                    wait = min(2 ** attempt * 5, 60)
                    log.warning("LLM rate limited, waiting %ds", wait)
                    time.sleep(wait)
                    continue

                log.error("LLM API error %d: %s", resp.status_code, resp.text[:200])
                self.stats.record_error()
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return ""

            except httpx.TimeoutException:
                self.last_error_code = None
                self.last_error_body = "timeout"
                log.warning("LLM timeout (attempt %d/%d)", attempt + 1, self.config.max_retries)
                time.sleep(2 ** attempt)
            except Exception as e:
                self.last_error_code = None
                self.last_error_body = str(e)[:500]
                log.error("LLM call failed: %s", e)
                self.stats.record_error()
                time.sleep(2 ** attempt)

        log.error("LLM call exhausted all retries")
        self.stats.record_error()
        return ""

    # -- Cache --------------------------------------------------------------

    def _init_cache(self):
        Path(self.config.cache_db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.cache_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            )
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()

    def _cache_key(self, messages: list, temperature: float) -> str:
        raw = json.dumps({"m": messages, "t": temperature, "model": self.config.model},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cache(self, key: str) -> Optional[str]:
        if not self.config.cache_enabled:
            return None
        with self._cache_lock:
            try:
                conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
                row = conn.execute("SELECT value FROM llm_cache WHERE key=?", (key,)).fetchone()
                conn.close()
                return row[0] if row else None
            except Exception:
                return None

    def _set_cache(self, key: str, value: str):
        with self._cache_lock:
            try:
                conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, value, ts) VALUES (?, ?, ?)",
                    (key, value, time.time()),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("Cache write failed: %s", e)

    def clear_cache(self, older_than_days: int = 30):
        cutoff = time.time() - older_than_days * 86400
        try:
            conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
            conn.execute("DELETE FROM llm_cache WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("Cache cleanup failed: %s", e)

    # -- Test / health check ------------------------------------------------

    def test_connection(self) -> Tuple[bool, str]:
        """Quick health check: send a tiny prompt and see if we get a response."""
        if not self.config.api_key:
            return False, "No API key configured (set DEEPSEEK_API_KEY or OPENAI_API_KEY)"
        try:
            resp = self.chat("Reply with exactly: OK", max_tokens=5, use_cache=False)
            if resp:
                return True, f"Connected to {self.config.provider} ({self.config.model})"
            return False, "Empty response from LLM"
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: Optional[LLMClient] = None
_client_lock = threading.Lock()


def _llm_config_from_ai_yaml() -> LLMConfig:
    """从 config/ai.yaml 的 llm 段构建配置；缺省项仍由 LLMConfig.__post_init__ 补全。"""
    path = config_file("ai.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.debug("ai.yaml 未读取 (%s)，使用环境变量", e)
        return LLMConfig()
    llm = data.get("llm") or {}
    if not llm:
        return LLMConfig()
    kwargs: Dict[str, Any] = {}
    for k in (
        "provider", "api_key", "model", "vision_model", "temperature", "max_tokens",
        "timeout_sec", "max_retries", "cache_enabled",
    ):
        if k in llm and llm[k] is not None:
            kwargs[k] = llm[k]
    return LLMConfig(**kwargs)


def get_llm_client(config: Optional[LLMConfig] = None) -> LLMClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = LLMClient(config or _llm_config_from_ai_yaml())
    return _client


# ---------------------------------------------------------------------------
# 免费 Vision 客户端 (用于 AI 精筛头像/资料页)
# ---------------------------------------------------------------------------

_vision_client: Optional[LLMClient] = None
_vision_lock = threading.Lock()


def get_free_vision_client() -> Optional[LLMClient]:
    """
    获取免费的 Vision LLM 客户端, 按优先级尝试:
      1. Google Gemini (GEMINI_API_KEY) — 免费 1500次/天
      2. Ollama 本地 (自动检测是否运行中) — 完全免费无限次
      3. 回退到默认 LLM client
    """
    global _vision_client
    if _vision_client is not None:
        return _vision_client

    with _vision_lock:
        if _vision_client is not None:
            return _vision_client

        # 1. 优先 Gemini (免费额度最大)
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            log.info("AI精筛: 使用 Google Gemini (免费)")
            _vision_client = LLMClient(LLMConfig(
                provider="gemini",
                api_key=gemini_key,
                timeout_sec=15.0,
                max_retries=2,
                cache_enabled=True,
            ))
            return _vision_client

        # 2. 检测 Ollama 是否在运行
        try:
            probe = httpx.get("http://localhost:11434/api/tags", timeout=3)
            if probe.status_code == 200:
                models = [m["name"] for m in probe.json().get("models", [])]
                vision_models = [m for m in models
                                 if any(v in m for v in ("llava", "moondream",
                                                         "minicpm", "bakllava"))]
                if vision_models:
                    chosen = vision_models[0]
                    log.info("AI精筛: 使用 Ollama 本地模型 %s (免费)", chosen)
                    _vision_client = LLMClient(LLMConfig(
                        provider="ollama",
                        vision_model=chosen,
                        model=chosen,
                        timeout_sec=30.0,
                        max_retries=1,
                        cache_enabled=True,
                    ))
                    return _vision_client
                else:
                    log.info("Ollama 运行中但无 vision 模型, 可运行: ollama pull llava:7b")
        except Exception:
            pass

        # 3. 回退: 用默认 client (可能收费)
        log.info("AI精筛: 无免费 provider, 回退到默认 LLM")
        return None
