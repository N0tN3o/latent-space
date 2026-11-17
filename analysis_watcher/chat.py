import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
import requests

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import load_settings, update_settings_section, load_env_vars

logger = logging.getLogger("image_analysis_pipeline")

_OLLAMA_VISION_CAPS = {}
_VISION_NAME_HINTS = (
    "vision", "llava", "bakllava", "moondream", "minicpm-v",
    "qwen2-vl", "qwen2.5vl", "pixtral", "gemma3",
)

ASSISTANT_KEY_TO_ROLE = {
    "8a79f5ec-c5dd-448c-9611-99610792a04b": "analysis",
    "959058ad-4417-49e3-9e71-252ee2fb033d": "poem",
    "analysis": "analysis",
    "poem": "poem",
}


class AIGenerationError(RuntimeError):
    """Raised when primary and failover AI providers all fail for a role."""

    def __init__(self, code: str, message: str, provider: str = "", model: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.model = model


class LLMModel:
    """
    Multi-provider LLM adapter for Latent Space.
    Supports:
      - 'ollama' (Local Ollama server via /api/chat or /v1/chat/completions)
      - 'openai_compatible' (OpenAI, LM Studio, vLLM, Groq, OpenRouter)
      - 'gemini' (Google Gemini API - free tier & pro/flash models, native vision)
      - 'anthropic' (Anthropic Claude API, native vision)
      - 'buas' (Legacy BUas AI platform)
    """

    _attributes = [
        "name", "description", "purpose", "expertise",
        "capabilities", "restrictions", "additional",
        "llm", "creativity", "provider", "base_url"
    ]

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

        # Legacy BUas endpoints
        self.__url_api = "https://ai-assistants.buas.nl/aioda-api"
        self.__headers = {"Content-Type": "application/json"}
        self.__init_endpoint_url = f"{self.__url_api}/actions/init_assistant"
        self.__assistant_configs_endpoint_url = f"{self.__url_api}/actions/get_assistant"
        self.__assistant_update_endpoint_url = f"{self.__url_api}/actions/update_assistant"
        self.__ask_endpoint_url = f"{self.__url_api}/actions/ask_assistant"

        self._load_local_config()

    def _load_local_config(self):
        settings = load_settings()
        env = load_env_vars()
        ai_cfg = settings.get("ai", {})
        self._vision_mode = ai_cfg.get("vision_mode", "auto")
        role_cfg = ai_cfg.get(self.role, {})

        self._provider = role_cfg.get("provider", "ollama").lower()
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
        if role_cfg.get("assistant_key"):
            self.assistant_key = role_cfg["assistant_key"]

        # Resolve API keys from environment if not set in settings.json
        if not self._api_key:
            if self._provider == "gemini":
                self._api_key = env.get("GEMINI_API_KEY", "")
            elif self._provider == "anthropic":
                self._api_key = env.get("ANTHROPIC_API_KEY", "")
            elif self._provider == "openai_compatible":
                self._api_key = (
                    env.get("OPENAI_API_KEY")
                    or env.get("OPENROUTER_API_KEY")
                    or env.get("GROQ_API_KEY")
                    or ""
                )

        # Legacy BUas credentials
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
            p_json = p_resp.json()
            self._name = p_json.get("name", self._name)
            self._description = p_json.get("description", self._description)
            self._purpose = p_json.get("purpose", self._purpose)
            self._expertise = p_json.get("expertise", self._expertise)
            self._capabilities = p_json.get("capabilities", self._capabilities)
            self._restrictions = p_json.get("restrictions", self._restrictions)
            self._additional = p_json.get("additional", self._additional)
            self._llm = p_json.get("llm", self._llm)
            self._creativity = p_json.get("temperature", self._temperature)
            self._temperature = self._creativity
            return init_response

        return {
            "status": "Success",
            "provider": self._provider,
            "model": self._llm,
            "role": self.role
        }

    @staticmethod
    def _encode_image_base64(image_path: str):
        if not image_path or not Path(image_path).exists():
            return None
        try:
            return base64.b64encode(Path(image_path).read_bytes()).decode("utf-8")
        except Exception:
            return None

    def _resolve_key_for_provider(self, provider: str, settings: dict, env: dict) -> str:
        """Find an API key for a given provider from settings (either role) or environment."""
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
        if provider == "openai_compatible":
            return env.get("GROQ_API_KEY") or env.get("OPENROUTER_API_KEY") or env.get("OPENAI_API_KEY") or ""
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
                fb_model = fb_model or (other_cfg.get("model") if other_cfg.get("provider") == "ollama" else "") or "llama3.2"
                add_candidate("ollama", fb_model, "http://localhost:11434/v1", "")
            elif fb_prov == "gemini":
                key = self._resolve_key_for_provider("gemini", settings, env)
                if key:
                    add_candidate("gemini", fb_model or "gemini-2.0-flash", "", key)
            elif fb_prov == "anthropic":
                key = self._resolve_key_for_provider("anthropic", settings, env)
                if key:
                    add_candidate("anthropic", fb_model or "claude-3-5-haiku-latest", "", key)
            elif fb_prov == "openai_compatible":
                key = self._resolve_key_for_provider("openai_compatible", settings, env)
                base_url = other_cfg.get("base_url") if other_cfg.get("provider") == "openai_compatible" else ""
                if key:
                    add_candidate("openai_compatible", fb_model or "llama-3.3-70b-versatile", base_url or "https://api.groq.com/openai/v1", key)
            elif fb_prov == "buas" and env.get("ACCESS_TOKEN") and env.get("USER_KEY"):
                add_candidate("buas", fb_model or "llama3.3:latest", self.__url_api, "")

        # 2. Other role's configured provider if different from current failed primary
        other_prov = (other_cfg.get("provider") or "").lower()
        if other_prov and other_prov != self._provider:
            other_key = other_cfg.get("api_key") or self._resolve_key_for_provider(other_prov, settings, env)
            if other_prov == "ollama" or other_key:
                add_candidate(
                    other_prov,
                    other_cfg.get("model", ""),
                    other_cfg.get("base_url", ""),
                    other_key,
                )

        # 3. Cloud API failovers (from .env or settings) — always use each provider's own default model
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
            ollama_model = (
                (other_cfg.get("model") if other_prov == "ollama" else "")
                or "llama3.2"
            )
            add_candidate("ollama", ollama_model, "http://localhost:11434/v1", "")

        return candidates

    def _call_provider(self, provider: str, model: str, base_url: str, api_key: str, message: str, img_b64: str = None) -> tuple:
        used_model = model
        if provider == "gemini":
            used_model = model or self._llm or "gemini-2.0-flash"
            reply = self._chat_gemini(message, img_b64, model_name=used_model, api_key=api_key)
        elif provider == "anthropic":
            used_model = model or self._llm or "claude-3-5-haiku-latest"
            reply = self._chat_anthropic(message, img_b64, model_name=used_model, api_key=api_key)
        elif provider == "buas":
            reply = self._chat_buas(message)
        elif provider in ("ollama", "openai_compatible"):
            reply, used_model = self._chat_openai_or_ollama(
                message, img_b64, provider=provider, model_name=model, base_url=base_url, api_key=api_key
            )
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

        if not reply or not reply.strip():
            raise RuntimeError(f"Empty response from {provider} ({used_model})")
        return reply.strip(), used_model

    def chat(self, message: str, image_path: str = None, allow_failover: bool = True) -> str:
        """
        Send a prompt (and optional drawing image for Vision LLMs) to the configured provider.
        Retries transient primary failures and fails over across providers if enabled.
        Raises AIGenerationError if all attempts fail.
        """
        self._load_local_config()
        use_vision = (
            image_path is not None
            and self.role == "analysis"
            and self._vision_mode in ("auto", "vision")
        )
        img_b64 = self._encode_image_base64(image_path) if use_vision else None

        # Step A: Primary provider (with 1 quick retry on transient errors)
        primary_err = None
        for attempt in range(2):
            try:
                reply, used_model = self._call_provider(self._provider, self._llm, self._base_url, self._api_key, message, img_b64)
                self._last_used_provider = self._provider
                self._last_used_model = used_model
                return reply
            except Exception as e:
                primary_err = e
                logger.warning(f"[chat] {self.role} primary attempt {attempt + 1} ({self._provider}/{self._llm}) failed: {e}")
                # Skip futile retry if missing API key or unknown provider
                if "not configured" in str(e) or "Unknown AI provider" in str(e):
                    break
                if attempt == 0:
                    time.sleep(0.5)

        # Step B: Cross-provider failover
        failover_errs = []
        if allow_failover:
            for fb_prov, fb_model, fb_url, fb_key in self._build_failover_candidates():
                try:
                    logger.warning(f"[chat] {self.role} failing over to {fb_prov} ({fb_model})...")
                    reply, used_model = self._call_provider(fb_prov, fb_model, fb_url, fb_key, message, img_b64)
                    self._last_used_provider = fb_prov
                    self._last_used_model = used_model
                    logger.info(f"[chat] {self.role} failover succeeded via {fb_prov} ({used_model})")
                    return reply
                except Exception as fb_e:
                    logger.warning(f"[chat] {self.role} failover {fb_prov} ({fb_model}) failed: {fb_e}")
                    failover_errs.append(f"{fb_prov}/{fb_model}: {fb_e}")

        # Step C: Definitive failure — surface explicit error code (no silent canned fallback)
        code = f"ERR_{self.role.upper()}_AI_FAILED"
        detail = f"Primary {self._provider} ({self._llm}) failed: {primary_err}"
        if failover_errs:
            detail += f" | Failovers failed: {'; '.join(failover_errs)}"
        logger.error(f"[chat] {code}: {detail}")
        raise AIGenerationError(code=code, message=detail, provider=self._provider, model=self._llm)

    def _discover_ollama_text_model(self, base_url: str, failed_model: str) -> str:
        """Pick an installed non-embedding Ollama model, preferring the other role's model."""
        host_root = base_url.replace("/v1", "").rstrip("/")
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
        """Check (cached) whether a local Ollama model supports multimodal image inputs."""
        if not model_name:
            return False
        low = model_name.lower()
        if any(hint in low for hint in _VISION_NAME_HINTS):
            return True
        host_root = clean_base[:-3] if clean_base.endswith("/v1") else clean_base
        cache_key = (host_root, model_name)
        if cache_key in _OLLAMA_VISION_CAPS:
            return _OLLAMA_VISION_CAPS[cache_key]
        supported = False
        try:
            resp = requests.post(f"{host_root}/api/show", json={"model": model_name}, timeout=1.0)
            if resp.ok:
                caps = [str(c).lower() for c in resp.json().get("capabilities", [])]
                supported = "vision" in caps
        except Exception:
            supported = False
        _OLLAMA_VISION_CAPS[cache_key] = supported
        return supported

    def _chat_openai_or_ollama(
        self,
        message: str,
        img_b64: str = None,
        provider: str = None,
        model_name: str = None,
        base_url: str = None,
        api_key: str = None,
    ) -> tuple:
        prov = (provider or self._provider).lower()
        mod = model_name if model_name is not None else self._llm
        b_url = (base_url if base_url is not None else self._base_url) or "http://localhost:11434/v1"
        key = api_key if api_key is not None else self._api_key

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

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if send_img_b64:
            user_content = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{send_img_b64}"}}
            ]
        else:
            user_content = message

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": mod,
            "messages": messages,
            "temperature": float(self._temperature),
        }

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)

        # If a text-only model on Ollama/OpenAI rejects image_url, retry with text only
        if resp.status_code >= 400 and send_img_b64 is not None:
            messages[-1]["content"] = message
            payload["messages"] = messages
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)

        # If Ollama returns 404 because the configured model tag is not pulled locally,
        # automatically discover an installed non-embedding Ollama model and retry
        used_model = mod
        if resp.status_code == 404 and prov == "ollama":
            try:
                fallback_model = self._discover_ollama_text_model(clean_base, mod)
                if fallback_model:
                    logger.warning(f"[chat] Ollama model '{mod}' not found locally; retrying with '{fallback_model}'")
                    payload["model"] = fallback_model
                    messages[-1]["content"] = message
                    payload["messages"] = messages
                    resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
                    if resp.ok:
                        used_model = fallback_model
            except Exception:
                pass

        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip(), used_model

    def _chat_gemini(self, message: str, img_b64: str = None, model_name: str = None, api_key: str = None) -> str:
        key = api_key if api_key is not None else self._api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        mod = model_name or self._llm or "gemini-2.0-flash"
        model_path = mod if mod.startswith("models/") else f"models/{mod}"

        url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={key}"
        system_prompt = self._build_system_prompt()

        parts = [{"text": message}]
        if img_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": img_b64
                }
            })

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": float(self._temperature)
            }
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        text_parts = [p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])]
        return "".join(text_parts).strip()

    def _chat_anthropic(self, message: str, img_b64: str = None, model_name: str = None, api_key: str = None) -> str:
        key = api_key if api_key is not None else self._api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01"
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
                }
            })
        content.append({"type": "text", "text": message})

        payload = {
            "model": model_name or self._llm or "claude-3-5-haiku-latest",
            "max_tokens": 600,
            "temperature": float(self._temperature),
            "messages": [{"role": "user", "content": content}]
        }
        if system_prompt:
            payload["system"] = system_prompt

        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    def _chat_buas(self, message: str) -> str:
        if not self._user_key or not self._access_token:
            raise RuntimeError("BUas credentials (ACCESS_TOKEN / USER_KEY) are not configured")
        if not self.__chat_key:
            init_data = {"user_key": self._user_key, "assistant_key": self.assistant_key}
            resp = requests.post(self.__init_endpoint_url, json=init_data, headers=self.__headers, timeout=15)
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
        resp = requests.post(self.__ask_endpoint_url, json=chat_data, headers=self.__headers, timeout=60)
        text = resp.text.strip()
        if text.startswith("{") and '"status": "Failed"' in text:
            self.__chat_key = None
            raise RuntimeError(f"BUas server error: {text}")
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
        image_generation: bool = False,
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
                    "image_generation": bool(image_generation),
                    "visibility": 0,
                }
                requests.post(self.__assistant_update_endpoint_url, json=update_data, headers=self.__headers, timeout=15)
            except Exception as e:
                print(f"[chat] Warning: BUas remote update failed ({e}), saved locally.")

        return {"status": "Success", "config": self.get_config_dict()}

    def get_config_dict(self) -> dict:
        self._load_local_config()
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
        }
