import base64
import logging
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlparse
import requests

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import (
    get_configured_ollama_host,
    load_env_vars,
    load_settings,
    update_settings_section,
)

logger = logging.getLogger("image_analysis_pipeline")

_OLLAMA_CAPS_LOCK = threading.Lock()
_OLLAMA_VISION_CAPS = {}
_VISION_NAME_HINTS = (
    "vision", "llava", "bakllava", "moondream", "minicpm-v",
    "qwen2-vl", "qwen2.5vl", "pixtral", "gemma3:4b", "gemma3:12b", "gemma3:27b",
)

ASSISTANT_KEY_TO_ROLE = {
    "8a79f5ec-c5dd-448c-9611-99610792a04b": "analysis",
    "959058ad-4417-49e3-9e71-252ee2fb033d": "poem",
    "analysis": "analysis",
    "poem": "poem",
}

MAX_CHAT_BUDGET_SEC = 65.0
CONNECT_TIMEOUT_SEC = 5.0
READ_TIMEOUT_SEC = 45.0


class AIGenerationError(RuntimeError):
    """Raised when primary and failover AI providers all fail for a role."""

    def __init__(self, code: str, message: str, provider: str = "", model: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.model = model


def _is_cloud_openai_compatible_host(base_url: str) -> str:
    """Return canonical cloud vendor ('groq', 'openrouter', 'openai') or '' for local/custom."""
    host = (urlparse(base_url or "").hostname or "").lower()
    if host == "groq.com" or host.endswith(".groq.com"):
        return "groq"
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    if host == "openai.com" or host.endswith(".openai.com"):
        return "openai"
    return ""


_SENSITIVE_QUERY_RE = re.compile(r"([?&](?:key|api_key|access_token|token)=)[^&\s'\"]+", re.IGNORECASE)


def _redact_secret_text(text: str, *secret_vals: str) -> str:
    """Strip query-string credentials and known API keys from error messages before logging/surfacing."""
    cleaned = _SENSITIVE_QUERY_RE.sub(r"\1***", str(text or ""))
    for sec in secret_vals:
        if sec and len(sec) >= 4:
            cleaned = cleaned.replace(sec, "***")
    return cleaned


def _should_skip_primary_retry(exc: Exception) -> bool:
    """Skip redundant retries on non-transient configuration/auth/client/connect failures."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code in (400, 401, 403, 404):
            return True
    msg = str(exc)
    return "not configured" in msg or "Unknown AI provider" in msg or "BUas init failed" in msg


class LLMModel:
    """
    Multi-provider LLM adapter for Latent Space.
    Supports:
      - 'ollama' (Local Ollama server via /v1/chat/completions)
      - 'openai_compatible' (OpenAI, LM Studio, vLLM, Groq, OpenRouter)
      - 'gemini' (Google Gemini API, native vision)
      - 'anthropic' (Anthropic Claude API, native vision)
      - 'buas' (Legacy BUas AI platform)
    """

    def __init__(self, key="analysis"):
        self.assistant_key = key
        self.role = ASSISTANT_KEY_TO_ROLE.get(key, "analysis")
        self._name = ""
        self._description = ""
        self._purpose = ""
        self._expertise = ""
        self._capabilities = ""
        self._restrictions = ""
        self._additional = ""
        self._llm = ""
        self._creativity = 0.7
        self._temperature = 0.7
        self._provider = "ollama"
        self._base_url = "http://localhost:11434/v1"
        self._api_key = ""
        self._fallback_provider = "auto"
        self._fallback_model = ""
        self._vision_mode = "auto"
        self._last_used_provider = ""
        self._last_used_model = ""
        self.__chat_key = None
        self._buas_remote_loaded = False
        self._buas_overrides = {}

        # Legacy BUas endpoints
        self.__url_api = "https://ai-assistants.buas.nl/aioda-api"
        self.__headers = {"Content-Type": "application/json"}
        self.__init_endpoint_url = f"{self.__url_api}/actions/init_assistant"
        self.__assistant_configs_endpoint_url = f"{self.__url_api}/actions/get_assistant"
        self.__assistant_update_endpoint_url = f"{self.__url_api}/actions/update_assistant"
        self.__ask_endpoint_url = f"{self.__url_api}/actions/ask_assistant"

        self._load_local_config()

    def _resolve_openai_compatible_key(self, base_url: str, settings: dict, env: dict) -> str:
        """Resolve API key matching the specific OpenAI-compatible base_url (never cross-sending keys)."""
        norm_url = (base_url or "").rstrip("/")
        ai_cfg = settings.get("ai", {})
        other_role = "poem" if self.role == "analysis" else "analysis"
        for role_name in (self.role, other_role):
            rc = ai_cfg.get(role_name, {})
            if (rc.get("provider") or "").lower() == "openai_compatible" and rc.get("api_key"):
                rc_url = (rc.get("base_url") or "").rstrip("/")
                if norm_url and rc_url and rc_url == norm_url:
                    return rc["api_key"]
                if role_name == self.role and not norm_url and not rc_url:
                    return rc["api_key"]

        vendor = _is_cloud_openai_compatible_host(norm_url)
        if vendor == "groq":
            return env.get("GROQ_API_KEY", "")
        if vendor == "openrouter":
            return env.get("OPENROUTER_API_KEY", "")
        if vendor == "openai":
            return env.get("OPENAI_API_KEY", "")
        return env.get("OPENAI_COMPATIBLE_API_KEY", "")

    def _load_local_config(self):
        settings = load_settings()
        env = load_env_vars()
        ai_cfg = settings.get("ai", {})
        self._vision_mode = ai_cfg.get("vision_mode", "auto")
        role_cfg = ai_cfg.get(self.role, {})

        new_provider = role_cfg.get("provider", "ollama").lower()
        new_key = role_cfg.get("assistant_key") or self.assistant_key
        buas_preserved = (
            self._buas_remote_loaded
            and new_provider == "buas"
            and new_key == self.assistant_key
        )

        self._provider = new_provider
        self._llm = role_cfg.get("model", "llama3.2")
        self._base_url = role_cfg.get("base_url", "http://localhost:11434/v1")
        self._api_key = role_cfg.get("api_key", "")
        self._fallback_provider = (role_cfg.get("fallback_provider") or "auto").lower()
        self._fallback_model = role_cfg.get("fallback_model", "") or ""
        self._name = role_cfg.get("name", f"Latent Space {self.role.capitalize()}")
        self._description = role_cfg.get("description", "")
        self._purpose = role_cfg.get("purpose", "")
        self._expertise = role_cfg.get("expertise", "")
        self._capabilities = role_cfg.get("capabilities", "")
        self._restrictions = role_cfg.get("restrictions", "")
        self._additional = role_cfg.get("additional", "")
        self._temperature = float(role_cfg.get("temperature", 0.7))
        self._creativity = self._temperature
        self.assistant_key = new_key

        if buas_preserved and self._buas_overrides:
            for attr, val in self._buas_overrides.items():
                setattr(self, attr, val)
            self._creativity = self._temperature
        elif new_provider != "buas":
            self._buas_remote_loaded = False
            self._buas_overrides = {}
            self.__chat_key = None
        else:
            self.__chat_key = None

        if not self._api_key and self._provider in ("gemini", "anthropic", "openai_compatible"):
            self._api_key = self._resolve_key_for_provider(
                self._provider, settings, env, base_url=self._base_url
            )

        self._access_token = env.get("ACCESS_TOKEN", "")
        self._user_key = env.get("USER_KEY", "")

    def _build_system_prompt(self) -> str:
        parts = [self._description]
        for label, val in [
            ("Purpose", self._purpose),
            ("Expertise", self._expertise),
            ("Capabilities", self._capabilities),
            ("Restrictions", self._restrictions),
            ("Additional Instructions", self._additional),
        ]:
            if val and str(val).strip():
                parts.append(f"{label}: {val.strip()}")
        return "\n\n".join(p for p in parts if p).strip()

    def initialize(self):
        """Initialize model configuration (loads local settings or connects to BUas if selected)."""
        self._load_local_config()

        if self._provider == "buas":
            if not self._user_key or not self._access_token:
                raise RuntimeError("BUas provider selected, but ACCESS_TOKEN or USER_KEY is missing in .env")
            init_data = {"user_key": self._user_key, "assistant_key": self.assistant_key}
            resp = requests.post(self.__init_endpoint_url, json=init_data, headers=self.__headers, timeout=15)
            resp.raise_for_status()
            init_response = resp.json()
            if init_response.get("status") == "Failed":
                raise RuntimeError(f"BUas init failed: {init_response.get('error')}")
            self.__chat_key = init_response["chat_key"]

            params_data = {
                "assistant_key": self.assistant_key,
                "user_key": self._user_key,
                "access_token": self._access_token,
                "chat_key": self.__chat_key,
            }
            p_resp = requests.post(self.__assistant_configs_endpoint_url, json=params_data, headers=self.__headers, timeout=15)
            p_resp.raise_for_status()
            p_json = p_resp.json()
            overrides = {
                "_name": p_json.get("name", self._name),
                "_description": p_json.get("description", self._description),
                "_purpose": p_json.get("purpose", self._purpose),
                "_expertise": p_json.get("expertise", self._expertise),
                "_capabilities": p_json.get("capabilities", self._capabilities),
                "_restrictions": p_json.get("restrictions", self._restrictions),
                "_additional": p_json.get("additional", self._additional),
                "_llm": p_json.get("llm") or self._llm,
                "_temperature": float(p_json.get("temperature") if p_json.get("temperature") is not None else self._temperature),
            }
            for attr, val in overrides.items():
                setattr(self, attr, val)
            self._creativity = self._temperature
            self._buas_overrides = overrides
            self._buas_remote_loaded = True
            return init_response

        return {
            "status": "Success",
            "provider": self._provider,
            "model": self._llm,
            "role": self.role,
        }

    @staticmethod
    def _encode_image_base64(image_path: str):
        if not image_path or not Path(image_path).exists():
            return None
        try:
            return base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")
        except Exception:
            return None

    def _resolve_key_for_provider(self, provider: str, settings: dict, env: dict, base_url: str = "") -> str:
        """Find an API key for a given provider from settings or environment, matching URL for OpenAI-compatible."""
        if provider == "openai_compatible":
            return self._resolve_openai_compatible_key(base_url, settings, env)
        ai_cfg = settings.get("ai", {})
        other_role = "poem" if self.role == "analysis" else "analysis"
        for role_name in (self.role, other_role):
            rc = ai_cfg.get(role_name, {})
            if (rc.get("provider") or "").lower() == provider and rc.get("api_key"):
                return rc["api_key"]
        if provider == "gemini":
            return env.get("GEMINI_API_KEY", "")
        if provider == "anthropic":
            return env.get("ANTHROPIC_API_KEY", "")
        return ""

    def _build_failover_candidates(self) -> list:
        """Build ordered list of alternate (provider, model, base_url, api_key) failover candidates."""
        if self._fallback_provider == "none":
            return []

        settings = load_settings()
        env = load_env_vars()
        ai_cfg = settings.get("ai", {})
        other_role = "poem" if self.role == "analysis" else "analysis"
        other_cfg = ai_cfg.get(other_role, {})
        other_prov = (other_cfg.get("provider") or "").lower()
        default_ollama_v1 = f"{get_configured_ollama_host(settings)}/v1"

        candidates = []
        seen = {(self._provider, self._llm, (self._base_url or "").rstrip("/"))}

        def add_candidate(prov: str, model: str, base_url: str, api_key: str):
            key = (prov, model, (base_url or "").rstrip("/"))
            if key not in seen:
                seen.add(key)
                candidates.append((prov, model, base_url, api_key))

        # 1. Explicit fallback provider configured for this role
        if self._fallback_provider not in ("", "auto"):
            fb_prov = self._fallback_provider
            fb_model = self._fallback_model
            if fb_prov == "ollama":
                fb_model = fb_model or (other_cfg.get("model") if other_prov == "ollama" else "") or "llama3.2"
                fb_url = (other_cfg.get("base_url") if other_prov == "ollama" else "") or default_ollama_v1
                add_candidate("ollama", fb_model, fb_url, "")
            elif fb_prov == "gemini":
                key = self._resolve_key_for_provider("gemini", settings, env)
                if key:
                    add_candidate("gemini", fb_model or "gemini-2.0-flash", "", key)
            elif fb_prov == "anthropic":
                key = self._resolve_key_for_provider("anthropic", settings, env)
                if key:
                    add_candidate("anthropic", fb_model or "claude-3-5-haiku-latest", "", key)
            elif fb_prov == "openai_compatible":
                fb_mod_low = (fb_model or "").lower()
                other_oai_url = other_cfg.get("base_url", "") if other_prov == "openai_compatible" else ""
                other_vendor = _is_cloud_openai_compatible_host(other_oai_url)
                if fb_mod_low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")) and env.get("OPENAI_API_KEY"):
                    add_candidate("openai_compatible", fb_model, "https://api.openai.com/v1", env["OPENAI_API_KEY"])
                elif fb_mod_low.startswith(("llama-3", "llama3-", "mixtral-", "gemma2-")) and "/" not in fb_mod_low and env.get("GROQ_API_KEY"):
                    add_candidate("openai_compatible", fb_model, "https://api.groq.com/openai/v1", env["GROQ_API_KEY"])
                elif other_prov == "openai_compatible" and other_oai_url and fb_model and fb_model == other_cfg.get("model"):
                    key = other_cfg.get("api_key") or self._resolve_openai_compatible_key(other_oai_url, settings, env)
                    if key or not other_vendor:
                        add_candidate("openai_compatible", fb_model, other_oai_url, key)
                elif (fb_mod_low.endswith(":free") or "/" in fb_mod_low) and env.get("OPENROUTER_API_KEY"):
                    add_candidate("openai_compatible", fb_model, "https://openrouter.ai/api/v1", env["OPENROUTER_API_KEY"])
                elif other_prov == "openai_compatible" and other_oai_url:
                    url = other_oai_url
                    key = other_cfg.get("api_key") or self._resolve_openai_compatible_key(url, settings, env)
                    if key or not other_vendor:
                        add_candidate("openai_compatible", fb_model or other_cfg.get("model") or "llama-3.3-70b-versatile", url, key)
                elif env.get("GROQ_API_KEY"):
                    add_candidate("openai_compatible", fb_model or "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1", env["GROQ_API_KEY"])
                elif env.get("OPENROUTER_API_KEY"):
                    add_candidate("openai_compatible", fb_model or "meta-llama/llama-3.2-11b-vision-instruct:free", "https://openrouter.ai/api/v1", env["OPENROUTER_API_KEY"])
                elif env.get("OPENAI_API_KEY"):
                    add_candidate("openai_compatible", fb_model or "gpt-4o-mini", "https://api.openai.com/v1", env["OPENAI_API_KEY"])
            elif fb_prov == "buas" and env.get("ACCESS_TOKEN") and env.get("USER_KEY"):
                add_candidate("buas", fb_model or "llama3.3:latest", self.__url_api, "")

        # 2. Other role's configured provider/endpoint if distinct from current failed primary
        if other_prov:
            other_url = other_cfg.get("base_url", "")
            if (other_prov, other_url.rstrip("/")) != (self._provider, (self._base_url or "").rstrip("/")):
                if other_prov != "openai_compatible" or bool(other_url):
                    other_key = other_cfg.get("api_key") or self._resolve_key_for_provider(other_prov, settings, env, base_url=other_url)
                    is_local_openai = other_prov == "openai_compatible" and bool(other_url) and not _is_cloud_openai_compatible_host(other_url)
                    is_buas_ready = other_prov == "buas" and bool(env.get("ACCESS_TOKEN") and env.get("USER_KEY"))
                    if other_prov == "ollama" or other_key or is_local_openai or is_buas_ready:
                        default_other_model = (
                            "llama3.2" if other_prov == "ollama" else
                            "gemini-2.0-flash" if other_prov == "gemini" else
                            "claude-3-5-haiku-latest" if other_prov == "anthropic" else
                            "llama3.3:latest" if other_prov == "buas" else ""
                        )
                        other_model = other_cfg.get("model") or default_other_model
                        if other_model:
                            add_candidate(
                                other_prov,
                                other_model,
                                other_url,
                                other_key,
                            )

        # 3. Cloud API failovers (from .env or settings) — always use matching keys & provider default models
        gemini_key = self._resolve_key_for_provider("gemini", settings, env)
        if gemini_key:
            add_candidate("gemini", "gemini-2.0-flash", "", gemini_key)

        if env.get("GROQ_API_KEY"):
            add_candidate("openai_compatible", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1", env["GROQ_API_KEY"])
        if env.get("OPENROUTER_API_KEY"):
            add_candidate("openai_compatible", "meta-llama/llama-3.2-11b-vision-instruct:free", "https://openrouter.ai/api/v1", env["OPENROUTER_API_KEY"])
        if env.get("OPENAI_API_KEY"):
            add_candidate("openai_compatible", "gpt-4o-mini", "https://api.openai.com/v1", env["OPENAI_API_KEY"])

        anth_key = self._resolve_key_for_provider("anthropic", settings, env)
        if anth_key:
            add_candidate("anthropic", "claude-3-5-haiku-latest", "", anth_key)

        if env.get("ACCESS_TOKEN") and env.get("USER_KEY"):
            add_candidate("buas", "llama3.3:latest", self.__url_api, "")

        # 4. Self-hosted Ollama failover when a Cloud provider failed
        if self._provider != "ollama":
            ollama_model = (other_cfg.get("model") if other_prov == "ollama" else "") or "llama3.2"
            ollama_url = (other_cfg.get("base_url") if other_prov == "ollama" else "") or default_ollama_v1
            add_candidate("ollama", ollama_model, ollama_url, "")

        return candidates

    def _call_provider(self, provider: str, model: str, base_url: str, api_key: str,
                       message: str, img_b64: str = None, timeout=None) -> tuple:
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        used_model = model
        if provider == "gemini":
            same_prov_model = self._llm if self._provider == "gemini" else ""
            used_model = model or same_prov_model or "gemini-2.0-flash"
            reply = self._chat_gemini(message, img_b64, model_name=used_model, api_key=api_key, timeout=req_timeout)
        elif provider == "anthropic":
            same_prov_model = self._llm if self._provider == "anthropic" else ""
            used_model = model or same_prov_model or "claude-3-5-haiku-latest"
            reply = self._chat_anthropic(message, img_b64, model_name=used_model, api_key=api_key, timeout=req_timeout)
        elif provider == "buas":
            reply = self._chat_buas(message, timeout=req_timeout)
        elif provider in ("ollama", "openai_compatible"):
            reply, used_model = self._chat_openai_or_ollama(
                message, img_b64, provider=provider, model_name=model,
                base_url=base_url, api_key=api_key, timeout=req_timeout,
            )
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

        if not reply or not reply.strip():
            raise RuntimeError(f"Empty response from {provider} ({used_model})")
        return reply.strip(), used_model

    def chat(self, message: str, image_path: str = None, allow_failover: bool = True,
             cancel_event=None) -> str:
        """
        Send a prompt (and optional drawing image for Vision LLMs) to the configured provider.
        Retries transient primary failures and fails over across providers within a bounded time budget.
        Raises AIGenerationError if all attempts fail or cancel_event is set.
        """
        self._load_local_config()
        use_vision = (
            image_path is not None
            and self.role == "analysis"
            and self._vision_mode in ("auto", "vision")
        )
        img_b64 = self._encode_image_base64(image_path) if use_vision else None
        deadline = time.monotonic() + MAX_CHAT_BUDGET_SEC

        # Step A: Primary provider (1 quick retry only on transient failures)
        primary_err = None
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                raise AIGenerationError(
                    code=f"ERR_{self.role.upper()}_CANCELLED",
                    message=f"{self.role} generation cancelled after timeout",
                    provider=self._provider,
                    model=self._llm,
                )
            rem = max(3.0, deadline - time.monotonic())
            req_timeout = (min(CONNECT_TIMEOUT_SEC, rem), min(READ_TIMEOUT_SEC, rem))
            try:
                reply, used_model = self._call_provider(
                    self._provider, self._llm, self._base_url, self._api_key,
                    message, img_b64, timeout=req_timeout,
                )
                self._last_used_provider = self._provider
                self._last_used_model = used_model
                return reply
            except Exception as e:
                primary_err = _redact_secret_text(str(e), self._api_key, self._access_token, self._user_key)
                logger.warning(f"[chat] {self.role} primary attempt {attempt + 1} ({self._provider}/{self._llm}) failed: {primary_err}")
                if _should_skip_primary_retry(e) or time.monotonic() >= deadline:
                    break
                if attempt == 0:
                    time.sleep(0.5)

        # Step B: Cross-provider failover within remaining time budget
        failover_errs = []
        if allow_failover:
            for fb_prov, fb_model, fb_url, fb_key in self._build_failover_candidates():
                if cancel_event is not None and cancel_event.is_set():
                    failover_errs.append("cancelled")
                    break
                rem = deadline - time.monotonic()
                if rem <= 1.0:
                    failover_errs.append("budget exhausted")
                    break
                req_timeout = (min(CONNECT_TIMEOUT_SEC, rem), min(15.0, rem))
                try:
                    logger.warning(f"[chat] {self.role} failing over to {fb_prov} ({fb_model})...")
                    reply, used_model = self._call_provider(
                        fb_prov, fb_model, fb_url, fb_key, message, img_b64, timeout=req_timeout
                    )
                    self._last_used_provider = fb_prov
                    self._last_used_model = used_model
                    logger.info(f"[chat] {self.role} failover succeeded via {fb_prov} ({used_model})")
                    return reply
                except Exception as fb_e:
                    safe_fb_err = _redact_secret_text(str(fb_e), fb_key, self._api_key, self._access_token, self._user_key)
                    logger.warning(f"[chat] {self.role} failover {fb_prov} ({fb_model}) failed: {safe_fb_err}")
                    failover_errs.append(f"{fb_prov}/{fb_model}: {safe_fb_err}")

        code = f"ERR_{self.role.upper()}_AI_FAILED"
        detail = _redact_secret_text(
            f"Primary {self._provider} ({self._llm}) failed: {primary_err}",
            self._api_key, self._access_token, self._user_key,
        )
        if failover_errs:
            detail += f" | Failovers failed: {'; '.join(failover_errs)}"
        logger.error(f"[chat] {code}: {detail}")
        raise AIGenerationError(code=code, message=detail, provider=self._provider, model=self._llm)

    def _discover_ollama_text_model(self, base_url: str, failed_model: str) -> str:
        """Pick an installed non-embedding Ollama model, preferring the other role's model."""
        host_root = base_url[:-3] if base_url.endswith("/v1") else base_url
        host_root = host_root.rstrip("/")
        tags_resp = requests.get(f"{host_root}/api/tags", timeout=3)
        if not tags_resp.ok:
            return ""
        installed = [
            m.get("name")
            for m in tags_resp.json().get("models", [])
            if m.get("name") and "embed" not in m.get("name", "").lower()
        ]
        if not installed:
            return ""
        settings = load_settings()
        other_role = "poem" if self.role == "analysis" else "analysis"
        other_cfg = settings.get("ai", {}).get(other_role, {})
        if other_cfg.get("provider") == "ollama" and other_cfg.get("model") in installed:
            return other_cfg["model"]
        for m in installed:
            if m != failed_model:
                return m
        return installed[0]

    @staticmethod
    def _ollama_supports_vision(clean_base: str, model_name: str) -> bool:
        """Check (thread-safe cached) whether a local Ollama model supports multimodal image inputs."""
        if not model_name:
            return False
        low = model_name.lower()
        if low.startswith("gemma3:1b"):
            return False
        host_root = clean_base[:-3] if clean_base.endswith("/v1") else clean_base
        host_root = host_root.rstrip("/")
        cache_key = (host_root, model_name)
        with _OLLAMA_CAPS_LOCK:
            if cache_key in _OLLAMA_VISION_CAPS:
                return _OLLAMA_VISION_CAPS[cache_key]

        supported = None
        if host_root.startswith(("http://", "https://")):
            try:
                resp = requests.post(f"{host_root}/api/show", json={"model": model_name}, timeout=1.0)
                if resp.ok:
                    caps = resp.json().get("capabilities")
                    if isinstance(caps, list):
                        supported = "vision" in [str(c).lower() for c in caps]
            except Exception:
                supported = None

        if supported is None:
            supported = any(hint in low for hint in _VISION_NAME_HINTS)

        with _OLLAMA_CAPS_LOCK:
            _OLLAMA_VISION_CAPS[cache_key] = bool(supported)
        return bool(supported)

    def _chat_openai_or_ollama(
        self,
        message: str,
        img_b64: str = None,
        provider: str = None,
        model_name: str = None,
        base_url: str = None,
        api_key: str = None,
        timeout=None,
    ) -> tuple:
        prov = (provider or self._provider).lower()
        same_prov_model = self._llm if prov == self._provider else ""
        mod = model_name or same_prov_model or ("llama3.2" if prov == "ollama" else "")
        b_url = (base_url if base_url is not None else self._base_url) or f"{get_configured_ollama_host()}/v1"
        key = api_key if api_key is not None else self._api_key
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)

        system_prompt = self._build_system_prompt()
        clean_base = b_url.rstrip("/")
        if not clean_base.endswith("/v1") and "11434" in clean_base and "/api" not in clean_base:
            clean_base = clean_base + "/v1"
        endpoint = f"{clean_base}/chat/completions"

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        send_img_b64 = img_b64
        if send_img_b64 and prov == "ollama" and self._vision_mode == "auto":
            if not self._ollama_supports_vision(clean_base, mod):
                send_img_b64 = None

        def build_messages(include_img: bool) -> list:
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            if include_img and img_b64:
                user_content = [
                    {"type": "text", "text": message},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ]
            else:
                user_content = message
            msgs.append({"role": "user", "content": user_content})
            return msgs

        payload = {
            "model": mod,
            "messages": build_messages(bool(send_img_b64)),
            "temperature": float(self._temperature),
        }

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=req_timeout)

        # Only retry without image when server specifically rejects the multimodal request shape
        if resp.status_code in (400, 422) and send_img_b64 is not None:
            payload["messages"] = build_messages(False)
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=req_timeout)

        # If Ollama returns 404 because the configured model tag is not pulled locally,
        # discover an installed model and preserve image payload if that model supports vision
        used_model = mod
        if resp.status_code == 404 and prov == "ollama":
            try:
                fallback_model = self._discover_ollama_text_model(clean_base, mod)
                if fallback_model:
                    logger.warning(f"[chat] Ollama model '{mod}' not found locally; retrying with '{fallback_model}'")
                    payload["model"] = fallback_model
                    keep_img = bool(
                        img_b64 and (
                            self._vision_mode == "vision"
                            or (self._vision_mode == "auto" and self._ollama_supports_vision(clean_base, fallback_model))
                        )
                    )
                    payload["messages"] = build_messages(keep_img)
                    resp = requests.post(endpoint, json=payload, headers=headers, timeout=req_timeout)
                    if resp.ok:
                        used_model = fallback_model
            except Exception:
                pass

        resp.raise_for_status()
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Empty or null message content from {prov} ({used_model})")
        return content.strip(), used_model

    def _chat_gemini(self, message: str, img_b64: str = None, model_name: str = None,
                     api_key: str = None, timeout=None) -> str:
        key = api_key if api_key is not None else self._api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        same_prov_model = self._llm if self._provider == "gemini" else ""
        mod = model_name or same_prov_model or "gemini-2.0-flash"
        model_path = mod if mod.startswith("models/") else f"models/{mod}"

        url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        }
        system_prompt = self._build_system_prompt()

        parts = [{"text": message}]
        if img_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": img_b64,
                }
            })

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": float(self._temperature)},
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        resp = requests.post(url, json=payload, headers=headers, timeout=timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC))
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        text_parts = [p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])]
        return "".join(text_parts).strip()

    def _chat_anthropic(self, message: str, img_b64: str = None, model_name: str = None,
                        api_key: str = None, timeout=None) -> str:
        key = api_key if api_key is not None else self._api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        system_prompt = self._build_system_prompt()

        content = []
        if img_b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            })
        content.append({"type": "text", "text": message})

        same_prov_model = self._llm if self._provider == "anthropic" else ""
        payload = {
            "model": model_name or same_prov_model or "claude-3-5-haiku-latest",
            "max_tokens": 600,
            "temperature": float(self._temperature),
            "messages": [{"role": "user", "content": content}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        resp = requests.post(url, json=payload, headers=headers, timeout=timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC))
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    def _chat_buas(self, message: str, timeout=None) -> str:
        if not self._user_key or not self._access_token:
            raise RuntimeError("BUas credentials (ACCESS_TOKEN / USER_KEY) are not configured")
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        if not self.__chat_key:
            init_data = {"user_key": self._user_key, "assistant_key": self.assistant_key}
            resp = requests.post(self.__init_endpoint_url, json=init_data, headers=self.__headers, timeout=min(15.0, req_timeout[1]))
            resp.raise_for_status()
            init_response = resp.json()
            if init_response.get("status") == "Failed":
                raise RuntimeError(f"BUas init failed: {init_response.get('error')}")
            self.__chat_key = init_response["chat_key"]

        chat_data = {
            "assistant_key": self.assistant_key,
            "chat_key": self.__chat_key,
            "message": message,
            "user_key": self._user_key,
        }
        resp = requests.post(self.__ask_endpoint_url, json=chat_data, headers=self.__headers, timeout=req_timeout)
        resp.raise_for_status()
        text = resp.text.strip()
        if text.startswith("{") and '"status": "Failed"' in text:
            self.__chat_key = None
            raise RuntimeError(f"BUas server error: {text}")
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise RuntimeError("BUas returned unexpected HTML error response")
        return text

    def update_assistant(
        self,
        name: str = None,
        description: str = None,
        purpose: str = None,
        expertise: str = None,
        capabilities: str = None,
        restrictions: str = None,
        additional: str = None,
        llm: str = None,
        temperature: float = None,
        provider: str = None,
        base_url: str = None,
        api_key: str = None,
        fallback_provider: str = None,
        fallback_model: str = None,
        vision_mode: str = None,
        ollama_vision_model: str = None,
    ):
        """Save updated configuration locally (and on BUas if provider == 'buas')."""
        role_updates = {
            "name": name if name is not None else self._name,
            "description": description if description is not None else self._description,
            "purpose": purpose if purpose is not None else self._purpose,
            "expertise": expertise if expertise is not None else self._expertise,
            "capabilities": capabilities if capabilities is not None else self._capabilities,
            "restrictions": restrictions if restrictions is not None else self._restrictions,
            "additional": additional if additional is not None else self._additional,
            "model": llm if llm is not None else self._llm,
            "temperature": float(temperature) if temperature is not None else float(self._temperature),
            "provider": (provider or self._provider).lower(),
            "base_url": base_url if base_url is not None else self._base_url,
            "fallback_provider": (fallback_provider if fallback_provider is not None else self._fallback_provider).lower(),
            "fallback_model": fallback_model if fallback_model is not None else self._fallback_model,
        }
        if api_key is not None and api_key != "***":
            role_updates["api_key"] = api_key

        ai_patch = {self.role: role_updates}
        if vision_mode is not None:
            ai_patch["vision_mode"] = vision_mode
        if ollama_vision_model is not None:
            ai_patch["ollama_vision_model"] = ollama_vision_model

        self._buas_remote_loaded = False
        self._buas_overrides = {}
        update_settings_section("ai", ai_patch)
        self._load_local_config()

        if self._provider == "buas" and self._access_token:
            try:
                update_data = {
                    "access_token": self._access_token,
                    "assistant_key": self.assistant_key,
                    "name": self._name,
                    "description": self._description,
                    "purpose": self._purpose,
                    "expertise": self._expertise,
                    "capabilities": self._capabilities,
                    "restrictions": self._restrictions,
                    "additional": self._additional,
                    "llm": self._llm,
                    "api_key": "",
                    "temperature": self._temperature,
                    "image_generation": False,
                    "visibility": 0,
                }
                u_resp = requests.post(self.__assistant_update_endpoint_url, json=update_data, headers=self.__headers, timeout=15)
                u_resp.raise_for_status()
            except Exception as e:
                print(f"[chat] Warning: BUas remote update failed ({e}), saved locally.")

        return {"status": "Success", "config": self.get_config_dict()}

    def get_config_dict(self) -> dict:
        self._load_local_config()
        settings = load_settings()
        return {
            "role": self.role,
            "provider": self._provider,
            "model": self._llm,
            "llm": self._llm,
            "base_url": self._base_url,
            "has_api_key": bool(self._api_key),
            "fallback_provider": self._fallback_provider,
            "fallback_model": self._fallback_model,
            "name": self._name,
            "description": self._description,
            "purpose": self._purpose,
            "expertise": self._expertise,
            "capabilities": self._capabilities,
            "restrictions": self._restrictions,
            "additional": self._additional,
            "temperature": self._temperature,
            "vision_mode": self._vision_mode,
            "ollama_vision_model": settings.get("ai", {}).get("ollama_vision_model", "moondream"),
        }
