import base64
import json
import logging
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlparse
import urllib.request
import requests

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import (
    DEFAULT_FALLBACK_MODELS,
    OPENAI_COMPAT_VISION_HINTS,
    THINKING_MODEL_HINTS,
    VISION_MODEL_HINTS,
    check_and_increment_daily_quota,
    fast_local_ollama_url,
    get_configured_ollama_host,
    is_thinking_model_name,
    load_env_vars,
    load_settings,
    update_settings_section,
)

logger = logging.getLogger("image_analysis_pipeline")

_OLLAMA_CAPS_LOCK = threading.Lock()
_OLLAMA_VISION_CAPS = {}
_OLLAMA_THINKING_CAPS = {}
_VISION_NAME_HINTS = VISION_MODEL_HINTS
_OPENAI_COMPAT_VISION_HINTS = OPENAI_COMPAT_VISION_HINTS
_THINKING_NAME_HINTS = THINKING_MODEL_HINTS
_THINK_TAG_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)
_GEMMA4_PX_PER_SOFT_TOKEN = 48 * 48


def _is_thinking_model_name(model_name: str) -> bool:
    return is_thinking_model_name(model_name)


def _strip_think_tags(text: str) -> str:
    if not text:
        return ""
    return _THINK_TAG_RE.sub("", str(text)).strip()

ASSISTANT_KEY_TO_ROLE = {
    "8a79f5ec-c5dd-448c-9611-99610792a04b": "analysis",
    "959058ad-4417-49e3-9e71-252ee2fb033d": "poem",
    "analysis": "analysis",
    "poem": "poem",
}

MAX_CHAT_BUDGET_SEC = 65.0
OLLAMA_WARM_READ_TIMEOUT_SEC = 75.0
OLLAMA_COLD_READ_TIMEOUT_SEC = 120.0
OLLAMA_CHAT_BUDGET_SEC = 125.0
OLLAMA_AUTO_THINKING_MAX_TOKENS = {"analysis": 768, "poem": 512}
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


def _should_skip_primary_retry(exc: Exception, provider: str = "") -> bool:
    """Skip redundant retries on non-transient failures or local Ollama read timeouts (which abort active loads)."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout)):
        return True
    if (provider or "").lower() == "ollama" and isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code in (400, 401, 403, 404):
            return True
    msg = str(exc)
    return "not configured" in msg or "Unknown AI provider" in msg or "BUas init failed" in msg


def _is_ollama_model_loaded(base_url: str, model_name: str) -> bool:
    """Fast non-blocking check (<0.5s) whether a local Ollama model is already warm in /api/ps."""
    target = (model_name or "").strip().lower()
    if not target:
        return False
    clean = (base_url or "").strip().rstrip("/")
    if clean.endswith("/v1"):
        clean = clean[:-3].rstrip("/")
    if not clean.startswith(("http://", "https://")):
        return False
    try:
        req = urllib.request.Request(f"{clean}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=0.45) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
        for m in (data.get("models") or []):
            name = str(m.get("name") or m.get("model") or "").strip().lower()
            if not name:
                continue
            if name == target or name == f"{target}:latest" or (":" not in target and name.startswith(f"{target}:")):
                return True
    except Exception:
        return False
    return False


DEFAULT_ANALYSIS_MAX_WORDS = 110
DEFAULT_WORD_LIMIT_TOLERANCE = 0.10

_WORD_CHAR_RE = re.compile(r"\w", re.UNICODE)
_ABBREV_END_RE = re.compile(
    r"(?:^|\s)(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|cf|al|[A-Z])\.$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r'([.!?…]+["”’\'\)\]]*)(\s+)')
_EDITOR_PREAMBLE_RE = re.compile(
    r"^(?:(?:sure|certainly|of course)[,!.\s-]*)?"
    r"(?:here(?:'s| is)\s+(?:the|a|your)\s+(?:condensed|tightened|shortened|revised|edited|updated)\b.*"
    r"|(?:condensed|tightened|shortened|edited)\s+(?:version|interpretation|analysis)\s*:?)\s*$",
    re.IGNORECASE,
)
_TRAILING_WORD_COUNT_LINE_RE = re.compile(
    r"^[\(\[\*_\s]*(?:word\s*count\s*[:=]?\s*\d+|\d+\s*words?)[\)\]\*_\.\s]*$",
    re.IGNORECASE,
)
_TRAILING_WORD_COUNT_INLINE_RE = re.compile(
    r"\s+[\(\[]\s*(?:word\s*count\s*[:=]?\s*\d+|\d+\s*words?)\s*[\)\]]\.?\s*$",
    re.IGNORECASE,
)


def count_words(text: str) -> int:
    """Count words in text (Unicode-aware, ignores standalone punctuation tokens)."""
    if not text:
        return 0
    return sum(1 for tok in str(text).split() if _WORD_CHAR_RE.search(tok))


def compute_word_limit_bounds(
    max_words: int = DEFAULT_ANALYSIS_MAX_WORDS,
    tolerance_pct: float = DEFAULT_WORD_LIMIT_TOLERANCE,
) -> tuple:
    """Return (min_target_words, hard_max_words) for max_words ± tolerance_pct."""
    try:
        mw = int(max_words)
    except (TypeError, ValueError):
        mw = DEFAULT_ANALYSIS_MAX_WORDS
    if mw <= 0:
        return (0, 0)
    tol = max(0.0, float(tolerance_pct))
    min_target = max(1, int(round(mw * (1.0 - tol))))
    hard_max = max(mw, int(round(mw * (1.0 + tol))))
    return (min_target, hard_max)


def _strip_editor_meta(text: str) -> str:
    """Remove accidental LLM condensation preambles and trailing '(108 words)' notes."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    lines = s.split("\n")
    # Strip leading chatter line like "Here is the condensed version:"
    while len(lines) >= 2 and _EDITOR_PREAMBLE_RE.match(lines[0].strip()):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    # Strip standalone trailing word-count line like "(106 words)"
    while len(lines) >= 2 and _TRAILING_WORD_COUNT_LINE_RE.match(lines[-1].strip()):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    s = "\n".join(lines).strip()
    s = _TRAILING_WORD_COUNT_INLINE_RE.sub("", s).strip()
    # Unwrap single outer quote pair if the model wrapped the entire answer in quotes
    if len(s) >= 2:
        for q_open, q_close in (('"', '"'), ("“", "”")):
            if s.startswith(q_open) and s.endswith(q_close) and s.count(q_open) == 1 and s.count(q_close) == 1:
                s = s[1:-1].strip()
                break
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


def _split_sentences(paragraph: str) -> list:
    """Split a paragraph into complete sentences while respecting abbreviations and closing quotes."""
    p = (paragraph or "").strip()
    if not p:
        return []
    sentences = []
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(p):
        cand_end = m.end(1)
        chunk = p[start:cand_end].strip()
        if not chunk:
            continue
        if _ABBREV_END_RE.search(chunk):
            continue
        sentences.append(chunk)
        start = m.end(2)
    tail = p[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def trim_text_to_word_limit(
    text: str,
    max_words: int = DEFAULT_ANALYSIS_MAX_WORDS,
    tolerance_pct: float = DEFAULT_WORD_LIMIT_TOLERANCE,
) -> str:
    """
    Deterministically cap text to at most hard_max = round(max_words * (1 + tolerance_pct)) words
    on complete sentence boundaries while preserving paragraph breaks. Never cuts mid-sentence.
    """
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not cleaned or int(max_words or 0) <= 0:
        return cleaned

    _, hard_max = compute_word_limit_bounds(max_words, tolerance_pct=tolerance_pct)
    if count_words(cleaned) <= hard_max:
        return cleaned

    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", cleaned) if p.strip()]
    if len(raw_paragraphs) == 1 and "\n" in raw_paragraphs[0]:
        raw_paragraphs = [p.strip() for p in raw_paragraphs[0].splitlines() if p.strip()]

    kept_paragraphs = []
    running_words = 0

    for para in raw_paragraphs:
        sentences = _split_sentences(para)
        kept_sents = []
        stop_all = False
        for sent in sentences:
            sw = count_words(sent)
            if sw == 0:
                continue
            if running_words + sw <= hard_max:
                kept_sents.append(sent)
                running_words += sw
            else:
                stop_all = True
                break
        if kept_sents:
            kept_paragraphs.append(" ".join(kept_sents))
        if stop_all:
            break

    if kept_paragraphs:
        result = "\n\n".join(kept_paragraphs).strip()
        if result and result[-1] not in '.!?…"”’\')':
            result += "."
        return result

    # Extreme fallback: even the very first sentence alone exceeded hard_max (run-on without periods).
    tokens = raw_paragraphs[0].split()
    picked = []
    wc = 0
    for tok in tokens:
        inc = 1 if _WORD_CHAR_RE.search(tok) else 0
        if wc + inc > hard_max:
            break
        picked.append(tok)
        wc += inc
    if not picked:
        return cleaned

    min_clause_idx = max(1, int(len(picked) * 0.65))
    clause_idx = -1
    for idx in range(len(picked) - 1, min_clause_idx - 1, -1):
        if picked[idx].endswith((",", ";", ":", "—", "–")):
            clause_idx = idx
            break
    if clause_idx != -1:
        picked = picked[: clause_idx + 1]
    picked[-1] = picked[-1].rstrip(",;:—– ") + "."
    return " ".join(picked).strip()


def enforce_analysis_word_limit(
    text: str,
    max_words: int = DEFAULT_ANALYSIS_MAX_WORDS,
    tolerance_pct: float = DEFAULT_WORD_LIMIT_TOLERANCE,
    llm_model=None,
    cancel_event=None,
    language_name: str = "English",
    deadline: float = None,
) -> tuple:
    """
    Guarantee an analysis obeys a hard max_words ± tolerance_pct ceiling (e.g. 110 ± 10% -> max 121 words)
    without mid-sentence truncation while allowing multiple paragraphs:
      1. If already <= hard_max words, return unchanged (zero extra latency).
      2. If over hard_max and llm_model is provided, run one low-temperature condensation pass to preserve
         the full narrative arc, closing sentence, and multi-paragraph flow within [min_target, max_words].
      3. Apply deterministic sentence-boundary trimming as an absolute safety net.
    Returns (final_text, info_dict).
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    orig_words = count_words(raw)
    try:
        mw = int(max_words or 0)
    except (TypeError, ValueError):
        mw = DEFAULT_ANALYSIS_MAX_WORDS

    if mw <= 0 or not raw:
        return raw, {
            "original_words": orig_words,
            "final_words": orig_words,
            "max_words": mw,
            "hard_max_words": 0,
            "action": "disabled",
        }

    min_target, hard_max = compute_word_limit_bounds(mw, tolerance_pct=tolerance_pct)
    if orig_words <= hard_max:
        return raw, {
            "original_words": orig_words,
            "final_words": orig_words,
            "max_words": mw,
            "hard_max_words": hard_max,
            "action": "unchanged",
        }

    candidate = raw
    used_condensation = False

    if (
        llm_model is not None
        and not (cancel_event is not None and cancel_event.is_set())
        and (deadline is None or (deadline - time.monotonic()) > 1.0)
    ):
        editor_system = (
            f"You are a precise literary editor for an interactive art installation. "
            f"Condense the provided psychoanalytical interpretation in {language_name} so its total length "
            f"is between {min_target} and {mw} words (strict hard maximum: {hard_max} words). "
            f"Preserve the poetic second-person voice, vivid imagery, psychological insight, and emotional arc. "
            f"Multiple short paragraphs are allowed as long as the total word count stays within {hard_max} words. "
            f"Every sentence must be complete. "
            f"Return ONLY the condensed interpretation—no title, no preamble, no quotes, and no word count."
        )
        editor_user = (
            f"Condense this interpretation into {min_target} to {mw} words "
            f"(strictly never exceed {hard_max} words total; keep complete sentences and full meaning):\n\n{raw}"
        )
        try:
            chat_kwargs = {
                "message": editor_user,
                "image_path": None,
                "allow_failover": True,
                "temperature_override": 0.4,
                "system_prompt_override": editor_system,
                "thinking_override": "off",
            }
            if cancel_event is not None:
                chat_kwargs["cancel_event"] = cancel_event
            if deadline is not None:
                chat_kwargs["deadline"] = deadline
            try:
                condensed_raw = llm_model.chat(**chat_kwargs)
            except TypeError:
                # Backwards-compatible fallback for simple test mocks without override kwargs
                fallback_kwargs = {"message": editor_user, "allow_failover": True}
                if cancel_event is not None:
                    fallback_kwargs["cancel_event"] = cancel_event
                condensed_raw = llm_model.chat(**fallback_kwargs)

            condensed = _strip_editor_meta(condensed_raw)
            cw = count_words(condensed)
            min_viable = min(25, max(10, int(round(mw * 0.35))))
            if cw >= min_viable and (cw <= hard_max or cw < orig_words):
                candidate = condensed
                used_condensation = True
        except Exception as e:
            logger.warning(f"[chat] Analysis condensation pass failed; falling back to sentence-boundary trim: {e}")

    cand_words = count_words(candidate)
    if cand_words <= hard_max:
        final_text = candidate
        action = "condensed" if used_condensation else "unchanged"
    else:
        final_text = trim_text_to_word_limit(candidate, max_words=mw, tolerance_pct=tolerance_pct)
        action = "condensed_and_trimmed" if used_condensation else "trimmed"

    return final_text, {
        "original_words": orig_words,
        "final_words": count_words(final_text),
        "max_words": mw,
        "hard_max_words": hard_max,
        "action": action,
    }


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
        self._max_words = DEFAULT_ANALYSIS_MAX_WORDS if self.role == "analysis" else 0
        self._thinking_mode = "auto"
        self._max_tokens = 0
        self._vision_token_budget = 280
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
        vision_cfg = ai_cfg.get("vision", {}) if isinstance(ai_cfg.get("vision"), dict) else {}
        try:
            self._vision_token_budget = int(vision_cfg.get("vision_token_budget", 280) or 280)
        except (TypeError, ValueError):
            self._vision_token_budget = 280
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
        self._fallback_provider = (role_cfg.get("fallback_provider") or "none").lower()
        self._fallback_model = role_cfg.get("fallback_model", "") or ""
        try:
            self._daily_cloud_call_limit = max(0, int(ai_cfg.get("daily_cloud_call_limit", 0) or 0))
        except (TypeError, ValueError):
            self._daily_cloud_call_limit = 0
        raw_fb_models = ai_cfg.get("fallback_models")
        self._fallback_models = dict(DEFAULT_FALLBACK_MODELS)
        if isinstance(raw_fb_models, dict):
            for k, v in raw_fb_models.items():
                if v and str(v).strip():
                    self._fallback_models[str(k).strip().lower()] = str(v).strip()
        self._name = role_cfg.get("name", f"Latent Space {self.role.capitalize()}")
        self._description = role_cfg.get("description", "")
        self._purpose = role_cfg.get("purpose", "")
        self._expertise = role_cfg.get("expertise", "")
        self._capabilities = role_cfg.get("capabilities", "")
        self._restrictions = role_cfg.get("restrictions", "")
        self._additional = role_cfg.get("additional", "")
        self._temperature = float(role_cfg.get("temperature", 0.7))
        self._creativity = self._temperature
        default_mw = DEFAULT_ANALYSIS_MAX_WORDS if self.role == "analysis" else 0
        raw_mw = role_cfg.get("max_words", default_mw)
        try:
            self._max_words = max(0, int(raw_mw)) if raw_mw is not None else default_mw
        except (TypeError, ValueError):
            self._max_words = default_mw
        self._thinking_mode = str(role_cfg.get("thinking_mode") or "auto").strip().lower()
        try:
            self._max_tokens = max(0, int(role_cfg.get("max_tokens", 0) or 0))
        except (TypeError, ValueError):
            self._max_tokens = 0
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
        if self.role == "analysis" and self._max_words > 0:
            _, hard_max = compute_word_limit_bounds(self._max_words)
            parts.append(
                f"Length Requirement: Keep your entire response within {self._max_words} words "
                f"(strict hard maximum: {hard_max} words total). Multiple short paragraphs are allowed "
                f"as long as the total stays within the limit. End with a complete sentence."
            )
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
    def _encode_image_base64(image_path: str, token_budget: int = 280):
        if not image_path or not Path(image_path).exists():
            return None
        try:
            raw_bytes = Path(image_path).read_bytes()
            budget = int(token_budget or 0)
            if budget > 0:
                max_pixels = budget * _GEMMA4_PX_PER_SOFT_TOKEN
                try:
                    import cv2
                    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
                    if img is not None and img.size > 0:
                        h, w = img.shape[:2]
                        if h * w > max_pixels:
                            scale = (max_pixels / float(h * w)) ** 0.5
                            new_w = max(32, int(round(w * scale)))
                            new_h = max(32, int(round(h * scale)))
                            resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                            ok, buf = cv2.imencode(".png", resized)
                            if ok:
                                return base64.b64encode(buf.tobytes()).decode("utf-8")
                except Exception:
                    pass
            return base64.b64encode(raw_bytes).decode("utf-8")
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
        fb_defaults = dict(DEFAULT_FALLBACK_MODELS)
        if isinstance(ai_cfg.get("fallback_models"), dict):
            for k, v in ai_cfg["fallback_models"].items():
                if v and str(v).strip():
                    fb_defaults[str(k).strip().lower()] = str(v).strip()
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
                fb_model = fb_model or (other_cfg.get("model") if other_prov == "ollama" else "") or fb_defaults["ollama"]
                fb_url = (other_cfg.get("base_url") if other_prov == "ollama" else "") or default_ollama_v1
                add_candidate("ollama", fb_model, fb_url, "")
            elif fb_prov == "gemini":
                key = self._resolve_key_for_provider("gemini", settings, env)
                if key:
                    add_candidate("gemini", fb_model or fb_defaults["gemini"], "", key)
            elif fb_prov == "anthropic":
                key = self._resolve_key_for_provider("anthropic", settings, env)
                if key:
                    add_candidate("anthropic", fb_model or fb_defaults["anthropic"], "", key)
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
                        add_candidate("openai_compatible", fb_model or other_cfg.get("model") or fb_defaults["groq"], url, key)
                elif env.get("GROQ_API_KEY"):
                    add_candidate("openai_compatible", fb_model or fb_defaults["groq"], "https://api.groq.com/openai/v1", env["GROQ_API_KEY"])
                elif env.get("OPENROUTER_API_KEY"):
                    add_candidate("openai_compatible", fb_model or fb_defaults["openrouter"], "https://openrouter.ai/api/v1", env["OPENROUTER_API_KEY"])
                elif env.get("OPENAI_API_KEY"):
                    add_candidate("openai_compatible", fb_model or fb_defaults["openai"], "https://api.openai.com/v1", env["OPENAI_API_KEY"])
            elif fb_prov == "buas" and env.get("ACCESS_TOKEN") and env.get("USER_KEY"):
                add_candidate("buas", fb_model or fb_defaults["buas"], self.__url_api, "")

        # 2. Other role's configured provider/endpoint if distinct from current failed primary
        if other_prov:
            other_url = other_cfg.get("base_url", "")
            if (other_prov, other_url.rstrip("/")) != (self._provider, (self._base_url or "").rstrip("/")):
                if other_prov != "openai_compatible" or bool(other_url):
                    other_key = other_cfg.get("api_key") or self._resolve_key_for_provider(other_prov, settings, env, base_url=other_url)
                    is_local_openai = other_prov == "openai_compatible" and bool(other_url) and not _is_cloud_openai_compatible_host(other_url)
                    is_buas_ready = other_prov == "buas" and bool(env.get("ACCESS_TOKEN") and env.get("USER_KEY"))
                    if other_prov == "ollama" or other_key or is_local_openai or is_buas_ready:
                        default_other_model = fb_defaults.get(other_prov, "")
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
            add_candidate("gemini", fb_defaults["gemini"], "", gemini_key)

        if env.get("GROQ_API_KEY"):
            add_candidate("openai_compatible", fb_defaults["groq"], "https://api.groq.com/openai/v1", env["GROQ_API_KEY"])
        if env.get("OPENROUTER_API_KEY"):
            add_candidate("openai_compatible", fb_defaults["openrouter"], "https://openrouter.ai/api/v1", env["OPENROUTER_API_KEY"])
        if env.get("OPENAI_API_KEY"):
            add_candidate("openai_compatible", fb_defaults["openai"], "https://api.openai.com/v1", env["OPENAI_API_KEY"])

        anth_key = self._resolve_key_for_provider("anthropic", settings, env)
        if anth_key:
            add_candidate("anthropic", fb_defaults["anthropic"], "", anth_key)

        if env.get("ACCESS_TOKEN") and env.get("USER_KEY"):
            add_candidate("buas", fb_defaults["buas"], self.__url_api, "")

        # 4. Self-hosted Ollama failover when a Cloud provider failed
        if self._provider != "ollama":
            ollama_model = (other_cfg.get("model") if other_prov == "ollama" else "") or fb_defaults["ollama"]
            ollama_url = (other_cfg.get("base_url") if other_prov == "ollama" else "") or default_ollama_v1
            add_candidate("ollama", ollama_model, ollama_url, "")

        return candidates

    def _call_provider(self, provider: str, model: str, base_url: str, api_key: str,
                       message: str, img_b64: str = None, timeout=None,
                       temperature_override: float = None, system_prompt_override: str = None,
                       thinking_override: str = None, deadline: float = None,
                       cancel_event=None) -> tuple:
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        is_cloud = provider in ("gemini", "anthropic", "buas") or (
            provider == "openai_compatible" and bool(_is_cloud_openai_compatible_host(base_url))
        )
        daily_limit = int(getattr(self, "_daily_cloud_call_limit", 0) or 0)
        if is_cloud and daily_limit > 0:
            if not check_and_increment_daily_quota("cloud_llm", daily_limit):
                raise RuntimeError(f"Daily cloud AI call limit ({daily_limit}) reached")

        fb_defaults = getattr(self, "_fallback_models", DEFAULT_FALLBACK_MODELS)
        used_model = model
        if provider == "gemini":
            same_prov_model = self._llm if self._provider == "gemini" else ""
            used_model = model or same_prov_model or fb_defaults.get("gemini", "gemini-2.0-flash")
            reply = self._chat_gemini(
                message, img_b64, model_name=used_model, api_key=api_key, timeout=req_timeout,
                temperature_override=temperature_override, system_prompt_override=system_prompt_override,
                thinking_override=thinking_override, deadline=deadline, cancel_event=cancel_event,
            )
        elif provider == "anthropic":
            same_prov_model = self._llm if self._provider == "anthropic" else ""
            used_model = model or same_prov_model or fb_defaults.get("anthropic", "claude-3-5-haiku-latest")
            reply = self._chat_anthropic(
                message, img_b64, model_name=used_model, api_key=api_key, timeout=req_timeout,
                temperature_override=temperature_override, system_prompt_override=system_prompt_override,
                deadline=deadline, cancel_event=cancel_event,
            )
        elif provider == "buas":
            reply = self._chat_buas(message, timeout=req_timeout, deadline=deadline, cancel_event=cancel_event)
        elif provider in ("ollama", "openai_compatible"):
            reply, used_model = self._chat_openai_or_ollama(
                message, img_b64, provider=provider, model_name=model,
                base_url=base_url, api_key=api_key, timeout=req_timeout,
                temperature_override=temperature_override, system_prompt_override=system_prompt_override,
                thinking_override=thinking_override, deadline=deadline, cancel_event=cancel_event,
            )
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

        if not reply or not reply.strip():
            raise RuntimeError(f"Empty response from {provider} ({used_model})")
        return reply.strip(), used_model

    def chat(self, message: str, image_path: str = None, allow_failover: bool = True,
             cancel_event=None, temperature_override: float = None,
             system_prompt_override: str = None, thinking_override: str = None,
             deadline: float = None) -> str:
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
        img_b64 = self._encode_image_base64(image_path, token_budget=self._vision_token_budget) if use_vision else None

        if self._provider == "ollama":
            is_warm = _is_ollama_model_loaded(self._base_url, self._llm)
            read_cap = OLLAMA_WARM_READ_TIMEOUT_SEC if is_warm else OLLAMA_COLD_READ_TIMEOUT_SEC
            budget_sec = max(MAX_CHAT_BUDGET_SEC, 85.0) if is_warm else OLLAMA_CHAT_BUDGET_SEC
            if not is_warm:
                logger.info(
                    f"[chat] Local Ollama model '{self._llm}' is not warm in /api/ps; "
                    f"using {read_cap:.0f}s uninterrupted cold-load read timeout."
                )
        else:
            read_cap = READ_TIMEOUT_SEC
            budget_sec = MAX_CHAT_BUDGET_SEC

        own_deadline = time.monotonic() + budget_sec
        eff_deadline = min(float(deadline), own_deadline) if deadline is not None else own_deadline

        # Step A: Primary provider (1 quick retry only on transient failures; never retry Ollama ReadTimeout)
        primary_err = None
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                raise AIGenerationError(
                    code=f"ERR_{self.role.upper()}_CANCELLED",
                    message=f"{self.role} generation cancelled after timeout",
                    provider=self._provider,
                    model=self._llm,
                )
            rem = eff_deadline - time.monotonic()
            if rem <= 0.1:
                primary_err = primary_err or "Run deadline exceeded"
                break
            req_timeout = (min(CONNECT_TIMEOUT_SEC, rem), min(read_cap, rem))
            call_deadline = min(eff_deadline, time.monotonic() + min(read_cap, rem))
            try:
                reply, used_model = self._call_provider(
                    self._provider, self._llm, self._base_url, self._api_key,
                    message, img_b64, timeout=req_timeout,
                    temperature_override=temperature_override,
                    system_prompt_override=system_prompt_override,
                    thinking_override=thinking_override,
                    deadline=call_deadline,
                    cancel_event=cancel_event,
                )
                self._last_used_provider = self._provider
                self._last_used_model = used_model
                return reply
            except Exception as e:
                primary_err = _redact_secret_text(str(e), self._api_key, self._access_token, self._user_key)
                logger.warning(f"[chat] {self.role} primary attempt {attempt + 1} ({self._provider}/{self._llm}) failed: {primary_err}")
                if _should_skip_primary_retry(e, provider=self._provider) or (eff_deadline - time.monotonic()) <= 0.6:
                    break
                if attempt == 0:
                    time.sleep(min(0.5, max(0.0, eff_deadline - time.monotonic() - 0.1)))

        # Step B: Cross-provider failover within remaining time budget
        failover_errs = []
        if allow_failover:
            for fb_prov, fb_model, fb_url, fb_key in self._build_failover_candidates():
                if cancel_event is not None and cancel_event.is_set():
                    failover_errs.append("cancelled")
                    break
                rem = eff_deadline - time.monotonic()
                if rem <= 1.0:
                    failover_errs.append("budget exhausted")
                    break
                req_timeout = (min(CONNECT_TIMEOUT_SEC, rem), min(15.0, rem))
                call_deadline = min(eff_deadline, time.monotonic() + min(15.0, rem))
                try:
                    logger.warning(f"[chat] {self.role} failing over to {fb_prov} ({fb_model})...")
                    reply, used_model = self._call_provider(
                        fb_prov, fb_model, fb_url, fb_key, message, img_b64, timeout=req_timeout,
                        temperature_override=temperature_override,
                        system_prompt_override=system_prompt_override,
                        thinking_override=thinking_override,
                        deadline=call_deadline,
                        cancel_event=cancel_event,
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
        host_root = fast_local_ollama_url(host_root.rstrip("/"))
        cache_key = (host_root, model_name)
        with _OLLAMA_CAPS_LOCK:
            if cache_key in _OLLAMA_VISION_CAPS:
                return _OLLAMA_VISION_CAPS[cache_key]

        supported = None
        thinking_supported = None
        if host_root.startswith(("http://", "https://")):
            try:
                resp = requests.post(f"{host_root}/api/show", json={"model": model_name}, timeout=0.6)
                if resp.ok:
                    caps = resp.json().get("capabilities")
                    if isinstance(caps, list):
                        caps_low = [str(c).lower() for c in caps]
                        supported = "vision" in caps_low
                        thinking_supported = "thinking" in caps_low
            except Exception:
                supported = None
                thinking_supported = None

        if supported is not None:
            with _OLLAMA_CAPS_LOCK:
                _OLLAMA_VISION_CAPS[cache_key] = bool(supported)
                if thinking_supported is not None:
                    _OLLAMA_THINKING_CAPS[cache_key] = bool(thinking_supported)
            return bool(supported)

        return any(hint in low for hint in _VISION_NAME_HINTS)

    @staticmethod
    def _ollama_supports_thinking(clean_base: str, model_name: str) -> bool:
        """Check cached Ollama /api/show capability (or model name hint) for thinking support."""
        if not model_name:
            return False
        host_root = clean_base[:-3] if clean_base.endswith("/v1") else clean_base
        host_root = fast_local_ollama_url(host_root.rstrip("/"))
        cache_key = (host_root, model_name)
        with _OLLAMA_CAPS_LOCK:
            if cache_key in _OLLAMA_THINKING_CAPS:
                return _OLLAMA_THINKING_CAPS[cache_key]
        return _is_thinking_model_name(model_name)

    @staticmethod
    def _openai_compatible_supports_vision(model_name: str) -> bool:
        """Return True when an OpenAI-compatible model name is known to support multimodal vision."""
        low = (model_name or "").strip().lower()
        if not low or low.startswith("gemma3:1b"):
            return False
        return any(h in low for h in _OPENAI_COMPAT_VISION_HINTS) or low.startswith(("o1", "o3", "o4"))

    def _chat_openai_or_ollama(
        self,
        message: str,
        img_b64: str = None,
        provider: str = None,
        model_name: str = None,
        base_url: str = None,
        api_key: str = None,
        timeout=None,
        temperature_override: float = None,
        system_prompt_override: str = None,
        thinking_override: str = None,
        deadline: float = None,
        cancel_event=None,
    ) -> tuple:
        prov = (provider or self._provider).lower()
        same_prov_model = self._llm if prov == self._provider else ""
        mod = model_name or same_prov_model or ("llama3.2" if prov == "ollama" else "")
        b_url = (base_url if base_url is not None else self._base_url) or f"{get_configured_ollama_host()}/v1"
        key = api_key if api_key is not None else self._api_key
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        conn_cap = req_timeout[0] if isinstance(req_timeout, (tuple, list)) else CONNECT_TIMEOUT_SEC
        read_cap = req_timeout[1] if isinstance(req_timeout, (tuple, list)) else float(req_timeout)
        call_deadline = float(deadline) if deadline is not None else (time.monotonic() + read_cap)

        def _next_timeout() -> tuple:
            if cancel_event is not None and cancel_event.is_set():
                raise requests.exceptions.ReadTimeout("Cancelled via cancel_event")
            rem = call_deadline - time.monotonic()
            if rem <= 0.05:
                raise requests.exceptions.ReadTimeout("Call deadline exceeded during provider request")
            return (min(conn_cap, rem), min(read_cap, rem))

        system_prompt = system_prompt_override if system_prompt_override is not None else self._build_system_prompt()
        eff_temp = float(temperature_override) if temperature_override is not None else float(self._temperature)
        eff_thinking = (thinking_override if thinking_override is not None else self._thinking_mode) or "auto"
        eff_thinking = str(eff_thinking).strip().lower()
        clean_base = b_url.rstrip("/")
        if not clean_base.endswith("/v1") and "11434" in clean_base and "/api" not in clean_base:
            clean_base = clean_base + "/v1"
        endpoint = f"{clean_base}/chat/completions"

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        send_img_b64 = img_b64
        if send_img_b64 and self._vision_mode == "auto":
            if prov == "ollama" and not self._ollama_supports_vision(clean_base, mod):
                send_img_b64 = None
            elif prov == "openai_compatible" and not self._openai_compatible_supports_vision(mod):
                send_img_b64 = None

        def build_messages(include_img: bool) -> list:
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            if include_img and img_b64:
                img_obj = {"url": f"data:image/png;base64,{img_b64}"}
                if prov == "openai_compatible":
                    budget = int(getattr(self, "_vision_token_budget", 280) or 280)
                    if budget <= 140:
                        img_obj["detail"] = "low"
                    elif budget >= 560:
                        img_obj["detail"] = "high"
                user_content = [
                    {"type": "text", "text": message},
                    {"type": "image_url", "image_url": img_obj},
                ]
            else:
                user_content = message
            msgs.append({"role": "user", "content": user_content})
            return msgs

        def resolve_reasoning_effort(target_model: str):
            is_thinking = (
                self._ollama_supports_thinking(clean_base, target_model)
                if prov == "ollama"
                else _is_thinking_model_name(target_model)
            )
            if eff_thinking == "auto":
                # Allow brief "low" thinking on thinking-capable models to balance quality & ~25-30s exhibition wait time
                return "low" if is_thinking else None
            if eff_thinking == "off":
                return "none" if is_thinking else None
            if eff_thinking in ("low", "medium", "high"):
                return eff_thinking
            return None

        payload = {
            "model": mod,
            "messages": build_messages(bool(send_img_b64)),
            "temperature": eff_temp,
        }
        effort = resolve_reasoning_effort(mod)
        if effort is not None:
            payload["reasoning_effort"] = effort
        if self._max_tokens > 0:
            payload["max_tokens"] = int(self._max_tokens)
        elif prov == "ollama" and eff_thinking == "auto" and effort == "low":
            # Guard boolean-thinking models (e.g. gemma4:e4b) against runaway unbounded thinking loops
            payload["max_tokens"] = OLLAMA_AUTO_THINKING_MAX_TOKENS.get(self.role, 768)

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=_next_timeout())

        # If server rejects reasoning_effort (e.g. non-thinking model or older runtime), strip it first while keeping image
        if resp.status_code in (400, 422) and "reasoning_effort" in payload:
            payload.pop("reasoning_effort", None)
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=_next_timeout())

        # Only retry without image when server specifically rejects the multimodal request shape
        if resp.status_code in (400, 422) and send_img_b64 is not None:
            payload["messages"] = build_messages(False)
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=_next_timeout())

        # If Ollama returns 404 because the configured model tag is not pulled locally,
        # discover an installed model and preserve image payload if that model supports vision
        used_model = mod
        if resp.status_code == 404 and prov == "ollama" and (call_deadline - time.monotonic()) > 0.5:
            try:
                fallback_model = self._discover_ollama_text_model(clean_base, mod)
                if fallback_model:
                    logger.warning(f"[chat] Ollama model '{mod}' not found locally; retrying with '{fallback_model}'")
                    payload["model"] = fallback_model
                    fb_effort = resolve_reasoning_effort(fallback_model)
                    if fb_effort is not None:
                        payload["reasoning_effort"] = fb_effort
                    else:
                        payload.pop("reasoning_effort", None)
                    keep_img = bool(
                        img_b64 and (
                            self._vision_mode == "vision"
                            or (self._vision_mode == "auto" and self._ollama_supports_vision(clean_base, fallback_model))
                        )
                    )
                    payload["messages"] = build_messages(keep_img)
                    resp = requests.post(endpoint, json=payload, headers=headers, timeout=_next_timeout())
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
        content = _strip_think_tags(content) if isinstance(content, str) else ""

        # Safety net: if thinking consumed max_tokens leaving empty content, retry once directly without thinking
        if (
            not content
            and (msg.get("reasoning") or msg.get("thinking") or msg.get("reasoning_content"))
            and payload.get("reasoning_effort") != "none"
            and (call_deadline - time.monotonic()) > 0.5
        ):
            logger.warning(
                f"[chat] {prov} ({used_model}) returned empty content with reasoning output; "
                "retrying once with reasoning_effort='none'."
            )
            payload["reasoning_effort"] = "none"
            if payload.get("max_tokens", 0) and payload["max_tokens"] < 512:
                payload.pop("max_tokens", None)
            retry_resp = requests.post(endpoint, json=payload, headers=headers, timeout=_next_timeout())
            if retry_resp.ok:
                retry_msg = (retry_resp.json().get("choices") or [{}])[0].get("message") or {}
                retry_c = retry_msg.get("content")
                if isinstance(retry_c, list):
                    retry_c = "".join(part.get("text", "") for part in retry_c if isinstance(part, dict))
                content = _strip_think_tags(retry_c) if isinstance(retry_c, str) else ""

        if not content:
            raise RuntimeError(f"Empty or null message content from {prov} ({used_model})")
        return content.strip(), used_model

    def _chat_gemini(self, message: str, img_b64: str = None, model_name: str = None,
                     api_key: str = None, timeout=None,
                     temperature_override: float = None, system_prompt_override: str = None,
                     thinking_override: str = None, deadline: float = None,
                     cancel_event=None) -> str:
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
        system_prompt = system_prompt_override if system_prompt_override is not None else self._build_system_prompt()
        eff_temp = float(temperature_override) if temperature_override is not None else float(self._temperature)
        eff_thinking = str((thinking_override if thinking_override is not None else self._thinking_mode) or "auto").strip().lower()

        parts = [{"text": message}]
        if img_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": img_b64,
                }
            })

        gen_cfg = {"temperature": eff_temp}
        if self._max_tokens > 0:
            gen_cfg["maxOutputTokens"] = int(self._max_tokens)
        mod_low = mod.lower()
        if ("gemini-2.5" in mod_low or "gemini-3" in mod_low) and eff_thinking in ("off", "low", "medium", "high"):
            budget_map = {"off": 0, "low": 256, "medium": 1024, "high": 2048}
            gen_cfg["thinkingConfig"] = {"thinkingBudget": budget_map[eff_thinking]}

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_cfg,
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        req_to = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        conn_cap = req_to[0] if isinstance(req_to, (tuple, list)) else CONNECT_TIMEOUT_SEC
        read_cap = req_to[1] if isinstance(req_to, (tuple, list)) else float(req_to)
        call_deadline = float(deadline) if deadline is not None else (time.monotonic() + read_cap)

        def _next_to():
            if cancel_event is not None and cancel_event.is_set():
                raise requests.exceptions.ReadTimeout("Cancelled via cancel_event")
            rem = call_deadline - time.monotonic()
            if rem <= 0.05:
                raise requests.exceptions.ReadTimeout("Call deadline exceeded during Gemini request")
            return (min(conn_cap, rem), min(read_cap, rem))

        resp = requests.post(url, json=payload, headers=headers, timeout=_next_to())
        if resp.status_code == 400 and "thinkingConfig" in gen_cfg:
            gen_cfg.pop("thinkingConfig", None)
            resp = requests.post(url, json=payload, headers=headers, timeout=_next_to())
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        text_parts = [
            p.get("text", "")
            for p in candidates[0].get("content", {}).get("parts", [])
            if not p.get("thought")
        ]
        return "".join(text_parts).strip()

    def _chat_anthropic(self, message: str, img_b64: str = None, model_name: str = None,
                        api_key: str = None, timeout=None,
                        temperature_override: float = None, system_prompt_override: str = None,
                        deadline: float = None, cancel_event=None) -> str:
        key = api_key if api_key is not None else self._api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured. Add it in Admin -> AI Config or .env")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        system_prompt = system_prompt_override if system_prompt_override is not None else self._build_system_prompt()
        eff_temp = float(temperature_override) if temperature_override is not None else float(self._temperature)

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
            "max_tokens": max(int(self._max_tokens), 64) if self._max_tokens > 0 else 600,
            "temperature": eff_temp,
            "messages": [{"role": "user", "content": content}],
        }
        if system_prompt:
            payload["system"] = system_prompt

        req_to = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0.05 or (cancel_event is not None and cancel_event.is_set()):
                raise requests.exceptions.ReadTimeout("Call deadline exceeded before Anthropic request")
            conn_cap = req_to[0] if isinstance(req_to, (tuple, list)) else CONNECT_TIMEOUT_SEC
            read_cap = req_to[1] if isinstance(req_to, (tuple, list)) else float(req_to)
            req_to = (min(conn_cap, rem), min(read_cap, rem))
        resp = requests.post(url, json=payload, headers=headers, timeout=req_to)
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    def _chat_buas(self, message: str, timeout=None, deadline: float = None, cancel_event=None) -> str:
        if not self._user_key or not self._access_token:
            raise RuntimeError("BUas credentials (ACCESS_TOKEN / USER_KEY) are not configured")
        req_timeout = timeout or (CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
        conn_cap = req_timeout[0] if isinstance(req_timeout, (tuple, list)) else CONNECT_TIMEOUT_SEC
        read_cap = req_timeout[1] if isinstance(req_timeout, (tuple, list)) else float(req_timeout)
        call_deadline = float(deadline) if deadline is not None else (time.monotonic() + read_cap)

        def _next_to(cap=None):
            if cancel_event is not None and cancel_event.is_set():
                raise requests.exceptions.ReadTimeout("Cancelled via cancel_event")
            rem = call_deadline - time.monotonic()
            if rem <= 0.05:
                raise requests.exceptions.ReadTimeout("Call deadline exceeded during BUas request")
            r_cap = min(read_cap, float(cap)) if cap is not None else read_cap
            return (min(conn_cap, rem), min(r_cap, rem))

        if not self.__chat_key:
            init_data = {"user_key": self._user_key, "assistant_key": self.assistant_key}
            resp = requests.post(self.__init_endpoint_url, json=init_data, headers=self.__headers, timeout=_next_to(15.0))
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
        resp = requests.post(self.__ask_endpoint_url, json=chat_data, headers=self.__headers, timeout=_next_to())
        if not resp.ok:
            self.__chat_key = None
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
        max_words: int = None,
        thinking_mode: str = None,
        max_tokens: int = None,
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
            "thinking_mode": (thinking_mode if thinking_mode is not None else self._thinking_mode).lower(),
            "max_tokens": int(max_tokens) if max_tokens is not None else int(self._max_tokens),
            "provider": (provider or self._provider).lower(),
            "base_url": base_url if base_url is not None else self._base_url,
            "fallback_provider": (fallback_provider if fallback_provider is not None else self._fallback_provider).lower(),
            "fallback_model": fallback_model if fallback_model is not None else self._fallback_model,
        }
        if max_words is not None:
            role_updates["max_words"] = max_words
        elif self.role == "analysis":
            role_updates["max_words"] = int(self._max_words)
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
        saved_role_key = bool(settings.get("ai", {}).get(self.role, {}).get("api_key"))
        return {
            "role": self.role,
            "provider": self._provider,
            "model": self._llm,
            "llm": self._llm,
            "base_url": self._base_url,
            "has_api_key": saved_role_key,
            "has_effective_api_key": bool(self._api_key),
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
            "max_words": self._max_words,
            "thinking_mode": self._thinking_mode,
            "max_tokens": self._max_tokens,
            "vision_mode": self._vision_mode,
            "ollama_vision_model": settings.get("ai", {}).get("ollama_vision_model", "moondream"),
        }
