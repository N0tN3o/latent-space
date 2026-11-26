#!/usr/bin/env python3
"""
Pluggable Multilingual Translation Engine for Latent Space (EN -> HU, extensible).

Supports three modes selectable live from /admin:
  - "local_nmt":  Zero-VRAM CPU Neural Machine Translation using HuggingFace
                  Helsinki-NLP/opus-mt-tc-big-en-hu (MarianMT, CC-BY 4.0) with
                  capped CPU threads so MediaPipe eye-tracking & Ollama stay smooth.
  - "ollama":     Dedicated Ollama translation model (e.g. translategemma:4b,
                  gemma3:4b, qwen3:4b, jobautomation/OpenEuroLLM-Hungarian).
  - "native_llm": Native target-language authoring via the configured Analysis
                  and Poem LLMModel providers (ideal with multilingual LLMs).
  - "off":        Disabled (English only).
"""

import importlib.util
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import requests

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import (
    ALLOWED_LANGUAGES,
    ALLOWED_POEM_STYLES,
    DEFAULT_LOCAL_NMT_MODEL,
    DEFAULT_POEM_STYLE,
    POEM_STYLE_SPECS,
    fast_local_ollama_url,
    get_configured_ollama_host,
    load_settings,
    validate_translation_data,
)

logger = logging.getLogger("image_analysis_pipeline")

LANGUAGE_NAMES = {
    "en": "English",
    "hu": "Hungarian",
}

CURATED_ARCHETYPES = {
    "hu": {
        "rorschach mirror": "Rorschach-tükör",
        "saccadic constellation": "Szakkadikus csillagkép",
        "fractal labyrinth": "Fraktál labirintus",
        "bilateral echo": "Kétoldali visszhang",
        "focused vortex": "Fókuszált örvény",
        "electric drift": "Elektromos sodródás",
        "panoramic currents": "Panoráma-áramlatok",
        "intertwined orbits": "Összefonódó pályák",
        "woven loops": "Szőtt hurkok",
        "woven tapestry": "Szőtt kárpit",
        "continuous horizon trace": "Folytonos horizontnyom",
        "abstract gaze trace": "Absztrakt tekintetnyom",
        "spiraling vortex": "Spirális örvény",
        "jagged intersections": "Tört metszéspontok",
        "wandering thread": "Vándorló fonal",
        "tangled web": "Összegubancolt háló",
        "horizon sweep": "Horizontív",
        "vertical ascent": "Függőleges emelkedés",
        "fluid waves": "Áramló hullámok",
        "scattered constellation": "Szétszórt csillagkép",
        "watching eyes / mask": "Figyelő szemek / Maszk",
        "wings in flight": "Repülő szárnyak",
        "branching roots": "Elágazó gyökerek",
        "labyrinth": "Labirintus",
        "twin silhouettes": "Ikersziluettek",
        "cosmic star map": "Kozmikus csillagtérkép",
        "infinity loop": "Végtelen hurok",
        "water reflections": "Víztükröződések",
        "descending sweep": "Ereszkedő lendület",
        "ascending sweep": "Emelkedő lendület",
        "horizon scan": "Látóhatár-pásztázás",
        "vertical ascent": "Függőleges emelkedés",
        "gathered knot": "Összefutó csomó",
        "zigzag drift": "Cikcakkos sodródás",
        "sea urchin": "Tengeri sün",
    }
}

_HU_VOWELS = "aáeéiíoóöőuúüűAÁEÉIÍOÓÖŐUÚÜŰ"
# Hungarian definite article must be 'az' before a vowel-initial word ('a egyenes' -> 'az egyenes')
_HU_ARTICLE_RE = re.compile(rf"(?<!\w)([Aa])([ \t]+)(?=[{_HU_VOWELS}])")
_HU_DUP_WORD_RE = re.compile(r"(?<!\w)(a|az|és|hogy|is)\s+\1(?!\w)", re.IGNORECASE)
# Recurring small-LLM Hungarian glitches observed in exhibition logs (safe, unambiguous fixes only)
_HU_LEXICAL_FIXES = {
    "magadot": "magadat",
    "skanálod": "fürkészed",
    "skanálja": "fürkészi",
    "skanál": "fürkész",
    "válasokat": "válaszokat",
    "vonalok": "vonalak",
    "vonalokat": "vonalakat",
    "trajektóriák": "ívek",
    "trajektória": "ív",
    "kis rákhoz": "tengerisünhöz",
    "kis rákként": "tengerisünként",
    "kis rák": "tengerisün",
}
_HU_LEXICAL_RE = re.compile(r"(?<!\w)(" + "|".join(sorted(map(re.escape, _HU_LEXICAL_FIXES), key=len, reverse=True)) + r")(?!\w)",
                            re.IGNORECASE)
# Tokens that indicate a broken/hallucinated Hungarian output (invented words, Hunglish, mistranslated metaphors)
_HU_SUSPECT_RE = re.compile(
    r"(?<!\w)(?:skan\w*|trajet\w*|nézedés\w*|értemény\w*|háttérzet\w*|körforog\w*|brilliant|koldus\w*|kis rák\w*"
    r"|the|and|of|with|your|gaze|lines?|edge|void|restless|stillness|urchin)(?!\w)",
    re.IGNORECASE,
)
_HU_FOREIGN_CHARS_RE = re.compile(r"[ñçãâêôàèìòùäëïÿøåßõûî]", re.IGNORECASE)
_HU_BAD_PLURAL_RE = re.compile(r"(?<!\w)\w{3,}[ae]ok(?!\w)", re.IGNORECASE)


def polish_hungarian_text(text: str, is_poem: bool = False) -> str:
    """Deterministically repair common Hungarian surface errors from small LLMs / NMT output."""
    if not text:
        return ""

    def _case_like(src: str, repl: str) -> str:
        return repl[0].upper() + repl[1:] if src[:1].isupper() else repl

    out = str(text)
    out = _HU_LEXICAL_RE.sub(lambda m: _case_like(m.group(1), _HU_LEXICAL_FIXES[m.group(1).lower()]), out)
    out = _HU_ARTICLE_RE.sub(lambda m: ("Az" if m.group(1) == "A" else "az") + m.group(2), out)
    out = _HU_DUP_WORD_RE.sub(lambda m: m.group(1), out)
    out = re.sub(r"[ \t]+([,.;:!?…])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    if is_poem:
        out = "\n".join(ln.strip() for ln in out.splitlines())
    return out.strip()


def hungarian_quality_issues(text: str) -> list:
    """Return suspicious tokens suggesting invented words, leftover English, or mistranslated metaphors."""
    if not text:
        return []
    issues = [m.group(0) for m in _HU_SUSPECT_RE.finditer(text)]
    issues += [m.group(0) for m in _HU_FOREIGN_CHARS_RE.finditer(text)]
    issues += [m.group(0) for m in _HU_BAD_PLURAL_RE.finditer(text)]
    return issues

_NMT_LOCK = threading.Lock()
_NMT_CACHE = {
    "model_id": "",
    "threads": 0,
    "tokenizer": None,
    "model": None,
    "loaded_at": 0.0,
    "last_error": "",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÖŐÚÜŰ\"'(\[])")
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", re.MULTILINE)
_SYLLABLE_TAG_RE = re.compile(r"\s*\(\s*\d{1,2}\s*(?:syllables?|szótag)?\s*\)\s*$", re.IGNORECASE)


def get_translation_config(settings: dict = None) -> dict:
    """Return normalized translation configuration merged with safe defaults."""
    cfg = settings if isinstance(settings, dict) else load_settings()
    tcfg = cfg.get("translation") if isinstance(cfg.get("translation"), dict) else {}
    raw_targets = tcfg.get("target_languages")
    if isinstance(raw_targets, list):
        targets = [
            str(x).strip().lower()
            for x in raw_targets
            if str(x).strip().lower() in ALLOWED_LANGUAGES and str(x).strip().lower() != "en"
        ]
    else:
        targets = ["hu"]
    return {
        "enabled": bool(tcfg.get("enabled", True)),
        "mode": str(tcfg.get("mode") or "local_nmt").strip().lower(),
        "target_languages": targets,
        "local_nmt_model": str(tcfg.get("local_nmt_model") or DEFAULT_LOCAL_NMT_MODEL).strip() or DEFAULT_LOCAL_NMT_MODEL,
        "local_nmt_threads": max(1, min(16, int(tcfg.get("local_nmt_threads") or 6))),
        "local_nmt_preload": bool(tcfg.get("local_nmt_preload", False)),
        "ollama_model": str(tcfg.get("ollama_model") or "translategemma:4b").strip() or "translategemma:4b",
        "ollama_base_url": str(tcfg.get("ollama_base_url") or "").strip(),
        "ollama_temperature": float(tcfg.get("ollama_temperature") if tcfg.get("ollama_temperature") is not None else 0.2),
        "ollama_timeout": max(5, min(180, int(tcfg.get("ollama_timeout") or 45))),
        "default_print_language": str(tcfg.get("default_print_language") or "en").strip().lower(),
    }


def reset_local_nmt_cache():
    """Unload any cached CPU NMT model (e.g. when model ID or thread cap changes)."""
    with _NMT_LOCK:
        _NMT_CACHE["model_id"] = ""
        _NMT_CACHE["threads"] = 0
        _NMT_CACHE["tokenizer"] = None
        _NMT_CACHE["model"] = None
        _NMT_CACHE["loaded_at"] = 0.0
        _NMT_CACHE["last_error"] = ""


def _is_hf_model_cached_locally(model_id: str) -> bool:
    """Fast, zero-network check whether a HuggingFace repo is cached on disk."""
    if not model_id:
        return False
    try:
        validate_translation_data({"local_nmt_model": model_id})
    except ValueError:
        return False
    try:
        from huggingface_hub import snapshot_download
        local_dir = snapshot_download(repo_id=model_id, local_files_only=True)
        return bool(local_dir and Path(local_dir).exists())
    except Exception:
        return False


def _load_local_nmt(model_id: str, num_threads: int = 6, allow_download: bool = False):
    """
    Thread-safe lazy loader for the CPU-only Marian/Seq2Seq NMT model.
    Strictly runs on CPU (0 MB GPU VRAM) with capped CPU threads so webcam
    MediaPipe gaze tracking and Ollama GPU inference remain smooth.
    """
    validate_translation_data({"local_nmt_model": model_id, "local_nmt_threads": int(num_threads)})
    if Path(model_id).exists():
        raise ValueError("Local filesystem paths are not allowed for local_nmt_model")
    with _NMT_LOCK:
        if (
            _NMT_CACHE["model"] is not None
            and _NMT_CACHE["tokenizer"] is not None
            and _NMT_CACHE["model_id"] == model_id
            and _NMT_CACHE["threads"] == num_threads
        ):
            return _NMT_CACHE["tokenizer"], _NMT_CACHE["model"]

        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            msg = f"Missing Python packages for local NMT ({exc}). Install torch, transformers, sentencepiece."
            _NMT_CACHE["last_error"] = msg
            raise RuntimeError(msg) from exc

        cpu_count = os.cpu_count() or 4
        safe_threads = max(1, min(int(num_threads), max(2, cpu_count - 2)))
        try:
            torch.set_num_threads(safe_threads)
        except Exception:
            pass

        def _load_seq2seq(mid: str, local_files_only: bool):
            try:
                return AutoModelForSeq2SeqLM.from_pretrained(
                    mid, local_files_only=local_files_only, use_safetensors=True
                )
            except Exception:
                if mid == DEFAULT_LOCAL_NMT_MODEL:
                    return AutoModelForSeq2SeqLM.from_pretrained(mid, local_files_only=local_files_only)
                raise

        t0 = time.monotonic()
        local_only = not allow_download
        try:
            # Always try local cache first so offline/air-gapped operation never stalls on DNS
            tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
            model = _load_seq2seq(model_id, local_files_only=True)
        except Exception:
            if local_only:
                msg = (
                    f"Local NMT model '{model_id}' is not cached on disk. "
                    "Click 'Download / Preload Model' in Admin or run: python download_offline_assets.py --with-translation"
                )
                _NMT_CACHE["last_error"] = msg
                raise RuntimeError(msg)
            # Fast reachability guard so air-gapped exhibitions never stall 30s waiting on DNS/retries
            try:
                requests.head("https://huggingface.co", timeout=2.0)
            except Exception as net_err:
                msg = (
                    f"Local NMT model '{model_id}' is not cached on disk and HuggingFace is unreachable ({net_err}). "
                    "Pre-download while online via Admin or: python download_offline_assets.py --with-translation"
                )
                _NMT_CACHE["last_error"] = msg
                raise RuntimeError(msg) from net_err
            logger.info(f"[translator] Downloading local CPU NMT model '{model_id}' (one-time cache)...")
            try:
                tokenizer = AutoTokenizer.from_pretrained(model_id)
                model = _load_seq2seq(model_id, local_files_only=False)
            except Exception as dl_err:
                msg = f"Could not load or download NMT model '{model_id}': {dl_err}"
                _NMT_CACHE["last_error"] = msg
                raise RuntimeError(msg) from dl_err

        model.to("cpu")
        model.eval()
        if getattr(model, "generation_config", None) is not None:
            model.generation_config.max_length = None

        _NMT_CACHE["model_id"] = model_id
        _NMT_CACHE["threads"] = num_threads
        _NMT_CACHE["tokenizer"] = tokenizer
        _NMT_CACHE["model"] = model
        _NMT_CACHE["loaded_at"] = time.time()
        _NMT_CACHE["last_error"] = ""
        logger.info(
            f"[translator] Loaded CPU NMT '{model_id}' in {time.monotonic() - t0:.2f}s "
            f"(threads={safe_threads}, VRAM=0 MB)"
        )
        return tokenizer, model


def preload_local_nmt_model(model_id: str = None, num_threads: int = None) -> dict:
    """Download (if needed) and warm up the CPU NMT model into RAM."""
    tcfg = get_translation_config()
    mid = (model_id if model_id is not None else tcfg["local_nmt_model"]).strip() or DEFAULT_LOCAL_NMT_MODEL
    thr = num_threads if num_threads is not None else tcfg["local_nmt_threads"]
    validate_translation_data({"local_nmt_model": mid, "local_nmt_threads": thr})
    if Path(mid).exists():
        raise ValueError("Local filesystem paths are not allowed for local_nmt_model")
    t0 = time.monotonic()
    _load_local_nmt(mid, num_threads=int(thr), allow_download=True)
    return {
        "status": "success",
        "model": mid,
        "threads": int(thr),
        "cached_locally": True,
        "loaded_in_memory": True,
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


def _clean_translated_output(text: str, is_poem: bool = False, poem_style: str = None) -> str:
    """Normalize LLM/NMT output, removing stray code fences or wrapping quotes, and enforcing verse line breaks."""
    if not text:
        return ""
    cleaned = _CODE_FENCE_RE.sub("", str(text)).strip()
    if len(cleaned) >= 2 and (
        (cleaned[0] == '"' and cleaned[-1] == '"')
        or (cleaned[0] == "'" and cleaned[-1] == "'")
        or (cleaned[0] == "„" and cleaned[-1] == "”")
    ):
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    if not is_poem:
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    else:
        lines = [_SYLLABLE_TAG_RE.sub("", ln).strip() for ln in cleaned.splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        non_empty = [ln for ln in lines if ln]
        # If an 8B model emitted a multi-sentence poem on a single paragraph line, split it into verse lines
        if len(non_empty) == 1 and len(non_empty[0].split()) >= 14:
            one_line = non_empty[0]
            split_sents = [s.strip() for s in re.split(r"(?<=[.!?…;])\s+", one_line) if s.strip()]
            if len(split_sents) < 3:
                split_sents = [s.strip() for s in re.split(r"(?<=[,;—–])\s+", one_line) if s.strip()]
            if len(split_sents) >= 3:
                lines = split_sents
                non_empty = split_sents

        eff_style = (poem_style or "").strip().lower()
        if eff_style == "haiku" and len(non_empty) > 3:
            lines = non_empty[:3]
        elif eff_style == "quatrain" and len(non_empty) >= 4:
            lines = non_empty[:4]
        elif eff_style == "lyrical_4_6" and len(non_empty) > 6:
            lines = non_empty[:6]
        elif eff_style == "two_stanza":
            if "" not in lines and len(non_empty) >= 4:
                mid = min(4, max(2, len(non_empty) // 2))
                lines = non_empty[:mid] + [""] + non_empty[mid:8]
        cleaned = "\n".join(lines)
    return cleaned.strip()


def _nmt_translate_batch(texts: list, model_id: str, num_threads: int = 6, allow_download: bool = False) -> list:
    """Translate a list of non-empty English strings in a single CPU-batched pass."""
    if not texts:
        return []
    import torch

    tokenizer, model = _load_local_nmt(model_id, num_threads=num_threads, allow_download=allow_download)
    outputs = []
    batch_size = 8
    with _NMT_LOCK:
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            encoded = tokenizer(
                chunk,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=384,
            )
            with torch.inference_mode():
                generated = model.generate(
                    **encoded,
                    num_beams=4,
                    early_stopping=True,
                    max_new_tokens=256,
                    no_repeat_ngram_size=3,
                )
            decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
            outputs.extend([d.strip() for d in decoded])
    return outputs


def _translate_prose_local_nmt(text: str, model_id: str, num_threads: int) -> str:
    """Translate prose paragraph-by-paragraph and sentence-by-sentence in one CPU batch."""
    if not text or not text.strip():
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    para_sentence_counts = []
    all_sentences = []
    for para in paragraphs:
        flat = re.sub(r"\s+", " ", para).strip()
        sents = [s.strip() for s in _SENTENCE_SPLIT_RE.split(flat) if s.strip()]
        if not sents:
            sents = [flat]
        para_sentence_counts.append(len(sents))
        all_sentences.extend(sents)

    translated_sents = _nmt_translate_batch(all_sentences, model_id=model_id, num_threads=num_threads)
    rebuilt_paras = []
    cursor = 0
    for count in para_sentence_counts:
        chunk = translated_sents[cursor : cursor + count]
        cursor += count
        rebuilt_paras.append(" ".join(s for s in chunk if s).strip())
    return "\n\n".join(p for p in rebuilt_paras if p).strip()


def _translate_poem_local_nmt(poem_text: str, model_id: str, num_threads: int) -> str:
    """Translate poem lines in a single CPU batch while preserving line and stanza breaks."""
    if not poem_text or not poem_text.strip():
        return ""
    raw_lines = poem_text.splitlines()
    non_empty_indices = []
    non_empty_texts = []
    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if stripped:
            non_empty_indices.append(idx)
            non_empty_texts.append(stripped)

    if not non_empty_texts:
        return ""

    translated_lines = _nmt_translate_batch(non_empty_texts, model_id=model_id, num_threads=num_threads)
    out = [""] * len(raw_lines)
    for idx, tr in zip(non_empty_indices, translated_lines):
        out[idx] = _clean_translated_output(tr, is_poem=False)
    return _clean_translated_output("\n".join(out), is_poem=True)


def _translate_archetype_parts(archetype: str, target_lang: str, translate_fn) -> str:
    """Translate combined archetype badges like 'Urchin · Woven Loops' using curated terms + translator."""
    if not archetype or not archetype.strip():
        return ""
    curated = CURATED_ARCHETYPES.get(target_lang, {})
    parts = [p.strip() for p in re.split(r"\s*[·|]\s*", archetype.strip()) if p.strip()]
    if not parts:
        return archetype.strip()

    translated_parts = []
    for part in parts:
        hit = curated.get(part.lower())
        if hit:
            translated_parts.append(hit)
            continue
        try:
            tr = translate_fn(part)
            tr = _clean_translated_output(tr, is_poem=False).strip(" .,:;-")
            if tr:
                # Preserve clean title-like capitalization of first character
                tr = tr[0].upper() + tr[1:] if len(tr) > 1 else tr.upper()
                translated_parts.append(tr)
            else:
                translated_parts.append(part)
        except Exception:
            translated_parts.append(part)
    return " · ".join(translated_parts)


def _resolve_ollama_endpoint(tcfg: dict, settings: dict) -> str:
    raw = (tcfg.get("ollama_base_url") or "").strip()
    if raw.startswith(("http://", "https://")):
        base = raw.rstrip("/")
    else:
        base = get_configured_ollama_host(settings).rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    if "/api" in base:
        return f"{base.split('/api')[0].rstrip('/')}/v1/chat/completions"
    return f"{base}/v1/chat/completions"


def _call_ollama_translate(
    text: str,
    kind: str,
    target_lang: str,
    tcfg: dict,
    settings: dict,
    cancel_event=None,
    deadline: float = None,
) -> str:
    """Translate text via a dedicated Ollama model (TranslateGemma or any local chat model)."""
    if not text or not text.strip():
        return ""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Translation cancelled")

    target_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    model_name = tcfg["ollama_model"]
    endpoint = _resolve_ollama_endpoint(tcfg, settings)
    read_cap = float(tcfg["ollama_timeout"])
    call_deadline = min(float(deadline), time.monotonic() + read_cap) if deadline is not None else (time.monotonic() + read_cap)

    def _next_to():
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Translation cancelled")
        rem = call_deadline - time.monotonic()
        if rem <= 0.1:
            raise requests.exceptions.ReadTimeout("Translation deadline exceeded")
        return (min(5.0, rem), min(read_cap, rem))

    is_translategemma = "translategemma" in model_name.lower()

    if is_translategemma:
        style_extra = ""
        if kind == "poem":
            style_extra = " This is a lyrical poem (or haiku): preserve its concrete imagery, line count, and exact line and stanza breaks."
        elif kind == "archetype":
            style_extra = " Keep it as a concise 1-to-4 word art title."
        user_prompt = (
            f"You are a professional English (en) to {target_name} ({target_lang}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original English text "
            f"while adhering to {target_name} grammar, vocabulary, and cultural sensitivities.{style_extra} "
            f"Produce only the {target_name} translation, without any additional explanations or commentary. "
            f"Please translate the following English text into {target_name}:\n\n"
            f"{text.strip()}"
        )
        messages = [{"role": "user", "content": user_prompt}]
    else:
        if kind == "poem":
            sys_prompt = (
                f"You are a lyrical poetry and haiku translator specializing in English to {target_name}. "
                f"Translate the poem into evocative, natural unrhymed {target_name} imagery. "
                f"Preserve the exact line and stanza structure and brevity without adding story or filler. "
                f"Return ONLY the {target_name} poem with no title, no syllable counts, no commentary, and no quotation marks."
            )
        elif kind == "archetype":
            sys_prompt = (
                f"Translate this concise 1-to-4 word abstract art archetype title from English into natural {target_name}. "
                f"Return ONLY the translated title with no punctuation or explanation."
            )
        else:
            sys_prompt = (
                f"You are a skilled literary translator from English to {target_name}. "
                f"Translate the following second-person psychoanalytical art interpretation into fluent, natural, "
                f"poetic {target_name}. Preserve paragraph breaks. "
                f"Return ONLY the {target_name} translation with no extra commentary."
            )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text.strip()},
        ]

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": float(tcfg["ollama_temperature"]),
    }
    from chat import _is_thinking_model_name, _strip_think_tags
    if _is_thinking_model_name(model_name):
        payload["reasoning_effort"] = "none"

    resp = requests.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=_next_to(),
    )
    if resp.status_code in (400, 422) and "reasoning_effort" in payload:
        payload.pop("reasoning_effort", None)
        resp = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_next_to(),
        )
    resp.raise_for_status()
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = _strip_think_tags(content) if isinstance(content, str) else ""
    if not content and (msg.get("reasoning") or msg.get("thinking") or msg.get("reasoning_content")):
        payload["reasoning_effort"] = "none"
        retry_resp = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_next_to(),
        )
        if retry_resp.ok:
            retry_msg = (retry_resp.json().get("choices") or [{}])[0].get("message") or {}
            retry_c = retry_msg.get("content")
            if isinstance(retry_c, list):
                retry_c = "".join(part.get("text", "") for part in retry_c if isinstance(part, dict))
            content = _strip_think_tags(retry_c) if isinstance(retry_c, str) else ""
    if not content:
        raise RuntimeError(f"Empty translation response from Ollama ({model_name})")
    cleaned_out = _clean_translated_output(content, is_poem=(kind == "poem"))
    if (target_lang or "").strip().lower() == "hu":
        cleaned_out = polish_hungarian_text(cleaned_out, is_poem=(kind == "poem"))
    return cleaned_out


def _chat_with_overrides(
    model,
    message: str,
    allow_failover: bool = True,
    cancel_event=None,
    temperature_override: float = None,
    system_prompt_override: str = None,
    thinking_override: str = None,
    deadline: float = None,
) -> str:
    """Call LLMModel.chat with optional temperature/system/thinking overrides, with TypeError fallback for simple mocks."""
    kwargs = {"message": message, "allow_failover": allow_failover}
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    if temperature_override is not None:
        kwargs["temperature_override"] = temperature_override
    if system_prompt_override is not None:
        kwargs["system_prompt_override"] = system_prompt_override
    if thinking_override is not None:
        kwargs["thinking_override"] = thinking_override
    if deadline is not None:
        kwargs["deadline"] = deadline
    try:
        return model.chat(**kwargs)
    except TypeError:
        fallback_kwargs = {"message": message, "allow_failover": allow_failover}
        if cancel_event is not None:
            fallback_kwargs["cancel_event"] = cancel_event
        return model.chat(**fallback_kwargs)


def _translate_native_llm(
    archetype: str,
    psychoanalysis: str,
    poem: str,
    target_lang: str,
    prompt_context: str = "",
    analysis_model=None,
    poem_model=None,
    cancel_event=None,
    settings: dict = None,
    deadline: float = None,
) -> dict:
    """
    Use the configured Analysis & Poem LLMModel providers to translate/adapt
    psychoanalysis and poetry into target_lang at a controlled low temperature with
    dedicated translator/poet system prompts (preventing high-temp lexical hallucinations
    while keeping the model hot in VRAM with zero model swapping).
    """
    from chat import LLMModel, compute_word_limit_bounds, enforce_analysis_word_limit

    cfg = settings if isinstance(settings, dict) else load_settings()
    tcfg = get_translation_config(cfg)
    target_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    a_mod = analysis_model or LLMModel("analysis")
    p_mod = poem_model or LLMModel("poem")

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Translation cancelled")

    # Controlled low temperatures so English-heavy models (e.g. llama3.1:8b) don't inherit temp=1.0
    # and hallucinate non-existent words or puns in the target language.
    base_tr_temp = float(tcfg.get("ollama_temperature", 0.2))
    analysis_tr_temp = round(min(max(base_tr_temp, 0.2), 0.4), 2)
    poem_tr_temp = round(min(max(base_tr_temp + 0.15, 0.35), 0.55), 2)

    try:
        native_max_words = int(((cfg.get("ai") or {}).get("analysis") or {}).get("max_words", 110))
    except (TypeError, ValueError):
        native_max_words = 110

    length_note = ""
    if native_max_words > 0:
        _, hard_max = compute_word_limit_bounds(native_max_words)
        length_note = (
            f" Keep the total length within {native_max_words} words (hard maximum: {hard_max} words). "
            f"Preserve paragraph breaks and ensure every sentence is complete."
        )

    lang_rules = ""
    hu_poem_rules = ""
    is_hu = (target_lang or "").strip().lower() == "hu"
    if is_hu:
        lang_rules = (
            " Hungarian style rules: Use natural, literary, idiomatic Hungarian with consistent informal "
            "second-person singular (tegezés, e.g. 'tekinteted', 'vonásaid', 'szemed' — NEVER mix magázás/önözés and "
            "NEVER start with 'Kedvesem'). Keep 'Latent Space' as 'Látens Tér' or 'Latent Space' (never invent "
            "puns). Rewrite the meaning in natural Hungarian sentence order rather than word-for-word English calques. "
            "Use 'az' before vowel-initial words and 'a' before consonant-initial words. "
            "Translate art/spatial terms accurately: strokes -> vonások/vonalak, archetype -> archetípus, "
            "balance -> egyensúly, upper/lower/left/right -> pontosan (ne cseréld fel a bal és jobb oldalt). "
            "Use only real, grammatically correct Hungarian words and inflections (e.g. 'magadat' not 'magadot', "
            "'vonalak/vonalakat' not 'vonalok', 'fürkészed/pásztázod' not 'skanálod', 'ívek' not 'trajektóriák')."
        )
        hu_poem_rules = (
            " For Hungarian: write clear, natural, grammatical Hungarian lines (use 'az' before vowels, "
            "real Hungarian verbs and nouns, never Hunglish or broken fragments). Each line must "
            "stand as a clean, concrete image in the present tense."
        )

    # 1. Faithful literary adaptation of the English psychoanalysis into target_lang
    a_system = (
        f"You are a skilled literary translator and art critic adapting an English psychoanalytical "
        f"art interpretation into fluent, poetic, natural {target_name}. "
        f"Stay faithful to the exact meaning, imagery, emotional tone, and spatial directions of the English text; "
        f"do not invent unrelated metaphors or add commentary.{lang_rules}{length_note} "
        f"Return ONLY the {target_name} interpretation with no title, preamble, quotes, or notes."
    )
    a_prompt = (
        f"Translate and literary-adapt this psychoanalytical interpretation into fluent, natural {target_name}:\n\n"
        f"{psychoanalysis}"
    )
    tr_analysis = _clean_translated_output(
        _chat_with_overrides(
            a_mod,
            message=a_prompt,
            allow_failover=True,
            cancel_event=cancel_event,
            temperature_override=analysis_tr_temp,
            system_prompt_override=a_system,
            thinking_override="off",
            deadline=deadline,
        ),
        is_poem=False,
    )
    if is_hu and tr_analysis:
        tr_analysis = polish_hungarian_text(tr_analysis, is_poem=False)
        issues = hungarian_quality_issues(tr_analysis)
        if issues and _is_hf_model_cached_locally(tcfg["local_nmt_model"]):
            try:
                logger.info(f"[translator] Native LLM analysis had suspect tokens {issues[:4]}; falling back to local NMT")
                nmt_prose = _translate_prose_local_nmt(
                    psychoanalysis, model_id=tcfg["local_nmt_model"], num_threads=tcfg["local_nmt_threads"]
                )
                if nmt_prose:
                    tr_analysis = polish_hungarian_text(nmt_prose, is_poem=False)
            except Exception:
                pass
    if native_max_words > 0 and tr_analysis:
        tr_analysis, _ = enforce_analysis_word_limit(
            tr_analysis,
            max_words=native_max_words,
            tolerance_pct=0.10,
            llm_model=a_mod,
            cancel_event=cancel_event,
            language_name=target_name,
            deadline=deadline,
        )
        if is_hu and tr_analysis:
            tr_analysis = polish_hungarian_text(tr_analysis, is_poem=False)

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Translation cancelled")

    # 2. Target-language poem (matches the configured poem_style: lyrical_4_6, two_stanza, quatrain, or haiku)
    poem_cfg = ((cfg.get("ai") or {}).get("poem") or {})
    raw_ps = str(poem_cfg.get("poem_style") or DEFAULT_POEM_STYLE).strip().lower()
    poem_style = raw_ps if raw_ps in ALLOWED_POEM_STYLES else DEFAULT_POEM_STYLE
    ps_spec = POEM_STYLE_SPECS.get(poem_style) or POEM_STYLE_SPECS[DEFAULT_POEM_STYLE]
    style_rule = ps_spec["hu"] if is_hu else ps_spec["en"]
    p_system = (
        f"You are a lyrical poet, haiku master, and literary translator writing in natural, evocative {target_name}. "
        f"{style_rule} "
        f"Faithfully capture the concrete imagery, movement, and quiet mood of the English poem, preserving its line and stanza structure.{hu_poem_rules} "
        f"Do NOT tell a story, do NOT rhyme, and use only real, natural {target_name} words. "
        f"Return ONLY the {target_name} poem with no title, no syllable counts, no quotes, and no commentary."
    )
    if poem and poem.strip():
        p_prompt = (
            f"Adapt this English poem into natural {target_name} (preserving its line/stanza form):\n\n"
            f"{poem.strip()}\n\n"
            f"Context ({target_name} reading):\n{tr_analysis or psychoanalysis}"
        )
    else:
        p_prompt = (
            f"Compose a {target_name} poem inspired by this reading:\n\n"
            f"{tr_analysis or psychoanalysis}"
        )
    tr_poem = _clean_translated_output(
        _chat_with_overrides(
            p_mod,
            message=p_prompt,
            allow_failover=True,
            cancel_event=cancel_event,
            temperature_override=poem_tr_temp,
            system_prompt_override=p_system,
            thinking_override="off",
            deadline=deadline,
        ),
        is_poem=True,
        poem_style=poem_style,
    )
    if is_hu and tr_poem:
        tr_poem = polish_hungarian_text(tr_poem, is_poem=True)
        poem_issues = hungarian_quality_issues(tr_poem)
        if poem_issues and poem and poem.strip() and _is_hf_model_cached_locally(tcfg["local_nmt_model"]):
            try:
                logger.info(f"[translator] Native LLM poem had suspect tokens {poem_issues[:4]}; falling back to local NMT")
                nmt_poem = _translate_poem_local_nmt(
                    poem, model_id=tcfg["local_nmt_model"], num_threads=tcfg["local_nmt_threads"]
                )
                if nmt_poem:
                    tr_poem = polish_hungarian_text(nmt_poem, is_poem=True)
            except Exception:
                pass

    # 3. Archetype badge (curated table first, low-temp dedicated prompt only for dynamic pareidolia parts)
    arch_system = (
        f"You are a concise translator. Translate the given 1-to-3 word abstract art archetype title "
        f"into natural {target_name}. Return ONLY the translated title with no punctuation, quotes, or explanation."
    )

    def _llm_part_translate(part: str) -> str:
        raw_part = _chat_with_overrides(
            p_mod,
            message=f"Translate into {target_name}: {part}",
            allow_failover=True,
            cancel_event=cancel_event,
            temperature_override=0.1,
            system_prompt_override=arch_system,
            thinking_override="off",
            deadline=deadline,
        )
        return polish_hungarian_text(raw_part, is_poem=False) if is_hu else raw_part

    tr_archetype = _translate_archetype_parts(archetype, target_lang, _llm_part_translate)
    if is_hu and tr_archetype:
        tr_archetype = polish_hungarian_text(tr_archetype, is_poem=False)
    return {
        "archetype": tr_archetype,
        "psychoanalysis": tr_analysis,
        "poem": tr_poem,
        "provider": f"native_llm:{getattr(p_mod, '_last_used_model', '') or getattr(p_mod, '_llm', '')}",
    }


def translate_analysis_stage_local_nmt(
    archetype: str,
    psychoanalysis: str,
    target_lang: str = "hu",
    settings: dict = None,
) -> dict:
    """
    Fast CPU-only translation of archetype + psychoanalysis intended to run
    in a background CPU thread *while* Ollama generates the English poem on GPU.
    """
    tcfg = get_translation_config(settings)
    mid = tcfg["local_nmt_model"]
    thr = tcfg["local_nmt_threads"]
    tr_arch = _translate_archetype_parts(
        archetype,
        target_lang,
        lambda part: (_nmt_translate_batch([part], model_id=mid, num_threads=thr) or [part])[0],
    )
    tr_analysis = _translate_prose_local_nmt(psychoanalysis, model_id=mid, num_threads=thr)
    if (target_lang or "").strip().lower() == "hu":
        tr_arch = polish_hungarian_text(tr_arch, is_poem=False)
        tr_analysis = polish_hungarian_text(tr_analysis, is_poem=False)
    return {
        "archetype": tr_arch,
        "psychoanalysis": tr_analysis,
        "provider": f"local_nmt:{mid}",
    }


def translate_poem_stage_local_nmt(
    poem: str,
    target_lang: str = "hu",
    settings: dict = None,
) -> str:
    """Translate the finished English poem on CPU, preserving lines and stanzas."""
    tcfg = get_translation_config(settings)
    out = _translate_poem_local_nmt(
        poem,
        model_id=tcfg["local_nmt_model"],
        num_threads=tcfg["local_nmt_threads"],
    )
    if (target_lang or "").strip().lower() == "hu":
        out = polish_hungarian_text(out, is_poem=True)
    return out


def translate_reading(
    archetype: str,
    psychoanalysis: str,
    poem: str,
    target_lang: str = "hu",
    settings: dict = None,
    prompt_context: str = "",
    analysis_model=None,
    poem_model=None,
    cancel_event=None,
    deadline: float = None,
) -> dict:
    """
    Translate a complete reading (archetype, psychoanalysis, poem) into target_lang
    using the configured pluggable backend ('local_nmt', 'ollama', or 'native_llm').
    """
    cfg = settings if isinstance(settings, dict) else load_settings()
    tcfg = get_translation_config(cfg)
    mode = tcfg["mode"]
    t0 = time.monotonic()

    if not tcfg["enabled"] or mode == "off":
        return {
            "ok": False,
            "skipped": True,
            "mode": "off",
            "provider": "off",
            "elapsed_sec": 0.0,
            "error": "Translation is disabled",
        }

    if mode == "local_nmt":
        mid = tcfg["local_nmt_model"]
        thr = tcfg["local_nmt_threads"]
        tr_arch = _translate_archetype_parts(
            archetype,
            target_lang,
            lambda part: (_nmt_translate_batch([part], model_id=mid, num_threads=thr) or [part])[0],
        )
        tr_analysis = _translate_prose_local_nmt(psychoanalysis, model_id=mid, num_threads=thr)
        tr_poem = _translate_poem_local_nmt(poem, model_id=mid, num_threads=thr)
        if (target_lang or "").strip().lower() == "hu":
            tr_arch = polish_hungarian_text(tr_arch, is_poem=False)
            tr_analysis = polish_hungarian_text(tr_analysis, is_poem=False)
            tr_poem = polish_hungarian_text(tr_poem, is_poem=True)
        return {
            "ok": True,
            "mode": "local_nmt",
            "provider": f"local_nmt:{mid}",
            "archetype": tr_arch,
            "psychoanalysis": tr_analysis,
            "poem": tr_poem,
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error": "",
        }

    if mode == "ollama":
        tr_arch = _translate_archetype_parts(
            archetype,
            target_lang,
            lambda part: _call_ollama_translate(
                part, "archetype", target_lang, tcfg, cfg, cancel_event=cancel_event, deadline=deadline
            ),
        )
        tr_analysis = _call_ollama_translate(
            psychoanalysis, "psychoanalysis", target_lang, tcfg, cfg, cancel_event=cancel_event, deadline=deadline
        )
        tr_poem = _call_ollama_translate(
            poem, "poem", target_lang, tcfg, cfg, cancel_event=cancel_event, deadline=deadline
        )
        return {
            "ok": True,
            "mode": "ollama",
            "provider": f"ollama:{tcfg['ollama_model']}",
            "archetype": tr_arch,
            "psychoanalysis": tr_analysis,
            "poem": tr_poem,
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error": "",
        }

    if mode == "native_llm":
        res = _translate_native_llm(
            archetype=archetype,
            psychoanalysis=psychoanalysis,
            poem=poem,
            target_lang=target_lang,
            prompt_context=prompt_context,
            analysis_model=analysis_model,
            poem_model=poem_model,
            cancel_event=cancel_event,
            settings=cfg,
            deadline=deadline,
        )
        res.update({
            "ok": True,
            "mode": "native_llm",
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error": "",
        })
        return res

    raise ValueError(f"Unsupported translation mode: {mode}")


def get_translation_availability(settings: dict = None) -> dict:
    """Fast local diagnostics for /admin translation configuration panel."""
    cfg = settings if isinstance(settings, dict) else load_settings()
    tcfg = get_translation_config(cfg)

    has_torch = importlib.util.find_spec("torch") is not None
    has_transformers = importlib.util.find_spec("transformers") is not None
    has_sentencepiece = importlib.util.find_spec("sentencepiece") is not None
    has_ctranslate2 = importlib.util.find_spec("ctranslate2") is not None

    mid = tcfg["local_nmt_model"]
    pkgs_ok = has_torch and has_transformers and has_sentencepiece
    cached_locally = _is_hf_model_cached_locally(mid) if pkgs_ok else False
    with _NMT_LOCK:
        loaded_in_memory = bool(_NMT_CACHE["model"] is not None and _NMT_CACHE["model_id"] == mid)
        last_nmt_error = _NMT_CACHE["last_error"]

    # Check Ollama models quickly (<=0.6s)
    ollama_models = []
    ollama_running = False
    try:
        raw_base = tcfg["ollama_base_url"] or get_configured_ollama_host(cfg)
        root = raw_base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        root = fast_local_ollama_url(root)
        if root.startswith(("http://", "https://")):
            r = requests.get(f"{root}/api/tags", timeout=0.6)
            if r.ok:
                ollama_running = True
                ollama_models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        pass

    target_om = tcfg["ollama_model"].lower()
    ollama_model_installed = any(
        m.lower() == target_om or m.lower() == f"{target_om}:latest" or (":" not in target_om and m.lower().startswith(f"{target_om}:"))
        for m in ollama_models
    )

    ai_cfg = cfg.get("ai", {})
    poem_cfg = ai_cfg.get("poem", {})
    return {
        "enabled": tcfg["enabled"],
        "mode": tcfg["mode"],
        "target_languages": tcfg["target_languages"],
        "default_print_language": tcfg["default_print_language"],
        "packages": {
            "torch": has_torch,
            "transformers": has_transformers,
            "sentencepiece": has_sentencepiece,
            "ctranslate2": has_ctranslate2,
        },
        "local_nmt": {
            "model": mid,
            "threads": tcfg["local_nmt_threads"],
            "packages_ready": pkgs_ok,
            "cached_locally": cached_locally,
            "loaded_in_memory": loaded_in_memory,
            "vram_mb": 0,
            "last_error": last_nmt_error,
        },
        "ollama": {
            "running": ollama_running,
            "model": tcfg["ollama_model"],
            "model_installed": ollama_model_installed,
            "installed_models": ollama_models,
        },
        "native_llm": {
            "provider": poem_cfg.get("provider", "ollama"),
            "model": poem_cfg.get("model", ""),
        },
    }
