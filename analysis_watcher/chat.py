import base64
import json
import os
import sys
from pathlib import Path
import requests

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import load_settings, update_settings_section, load_env_vars

ASSISTANT_KEY_TO_ROLE = {
    "8a79f5ec-c5dd-448c-9611-99610792a04b": "analysis",
    "959058ad-4417-49e3-9e71-252ee2fb033d": "poem",
    "analysis": "analysis",
    "poem": "poem",
}


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
        self._vision_mode = "auto"
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

    def chat(self, message: str, image_path: str = None) -> str:
        """
        Send a prompt (and optional drawing image for Vision LLMs) to the configured provider.
        """
        self._load_local_config()
        use_vision = (
            image_path is not None
            and self.role == "analysis"
            and self._vision_mode in ("auto", "vision")
        )
        img_b64 = self._encode_image_base64(image_path) if use_vision else None

        try:
            if self._provider == "gemini":
                return self._chat_gemini(message, img_b64)
            elif self._provider == "anthropic":
                return self._chat_anthropic(message, img_b64)
            elif self._provider == "buas":
                return self._chat_buas(message)
            elif self._provider in ("ollama", "openai_compatible"):
                return self._chat_openai_or_ollama(message, img_b64)
            else:
                raise ValueError(f"Unknown AI provider: {self._provider}")
        except Exception as e:
            print(f"[chat] Error calling {self._provider} ({self._llm}): {e}")
            return self._fallback_response(message, str(e))

    def _chat_openai_or_ollama(self, message: str, img_b64: str = None) -> str:
        system_prompt = self._build_system_prompt()
        base_url = (self._base_url or "http://localhost:11434/v1").rstrip("/")
        if not base_url.endswith("/v1") and "11434" in base_url and "/api" not in base_url:
            base_url = base_url + "/v1"
        endpoint = f"{base_url}/chat/completions"

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if img_b64:
            user_content = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]
        else:
            user_content = message

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self._llm,
            "messages": messages,
            "temperature": float(self._temperature),
        }

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)

        # If a text-only model on Ollama/OpenAI rejects image_url, retry with text only
        if resp.status_code >= 400 and img_b64 is not None:
            messages[-1]["content"] = message
            payload["messages"] = messages
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)

        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    def _chat_gemini(self, message: str, img_b64: str = None) -> str:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        model_name = self._llm or "gemini-2.0-flash"
        if not model_name.startswith("models/"):
            model_path = f"models/{model_name}"
        else:
            model_path = model_name

        url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={self._api_key}"
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

    def _chat_anthropic(self, message: str, img_b64: str = None) -> str:
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
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
            "model": self._llm or "claude-3-5-haiku-latest",
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
        if not self.__chat_key:
            self.initialize()
        chat_data = {
            "assistant_key": self.assistant_key,
            "chat_key": self.__chat_key,
            "message": message,
            "user_key": self._user_key,
        }
        resp = requests.post(self.__ask_endpoint_url, json=chat_data, headers=self.__headers, timeout=60)
        text = resp.text.strip()
        # Detect when BUas server returns a JSON error payload
        if text.startswith("{") and '"status": "Failed"' in text:
            raise RuntimeError(f"BUas server error: {text}")
        return text

    def _fallback_response(self, message: str, error_msg: str) -> str:
        """
        Graceful fallback so the gallery and print PDF never display broken JSON stack traces
        if an external AI server is offline or unconfigured.
        """
        if self.role == "analysis":
            return (
                "The trajectory of your gaze traces a quiet dialogue between impulse and restraint. "
                "Where the lines gather and overlap, attention lingers—searching for an anchor within "
                "open space—while the sudden leaps across the canvas reveal a restless curiosity "
                "drawn toward the unseen edges of perception."
            )
        else:
            return (
                "A silent thread of light and sight,\n"
                "Where wandering eyes Composed the night;\n"
                "In loops of thought and sudden grace,\n"
                "A fleeting mind leaves its own trace."
            )

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
