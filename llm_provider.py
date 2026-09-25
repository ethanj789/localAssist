import os
import json
import httpx
import logging
from typing import Any
from types import SimpleNamespace

log = logging.getLogger(__name__)

# --- Provider resolution logic ---
# We keep this here to consolidate all provider logic in one place.

def _normalize_provider(value: Any) -> str | None:
    if isinstance(value, str):
        provider = value.strip().lower()
        if provider in {"groq", "openrouter", "ollama"}:
            return provider
    return None

def get_effective_provider(config_dict: dict, explicit_provider: str | None = None) -> str:
    requested = _normalize_provider(explicit_provider)

    # 1. explicit request wins
    if requested:
        provider = requested
    else:
        # 2. config default
        provider = _normalize_provider(config_dict.get("use_provider"))

    # 3. fallback
    if not provider:
        provider = "groq" if config_dict.get("use_external_provider", False) else "ollama"

    # 4. openrouter override
    if provider in {"groq", "openrouter"} and config_dict.get("open_router_override"):
        return "openrouter"

    return provider

def get_model_name_for_provider(settings: dict[str, Any] | None, provider: str | None = None, fallback: Any = None) -> Any:
    if not settings:
        return fallback
    # In some contexts we might just pass the provider string instead of full config resolution
    # so we trust the passed provider if it's already resolved.
    if provider == "openrouter":
        model = settings.get("openrouter_model") or settings.get("model", fallback)
        # Eagerly append :free suffix when no payment is configured, so the
        # model name is canonical in SSE events and logs before the HTTP call.
        if model and not os.getenv("OPENROUTER_HAS_PAYMENT") and ":free" not in str(model):
            model = f"{model}:free"
        return model
    return settings.get("model", fallback)

def select_model(message: str, config_dict: dict, get_model_setting_fn, coding_model: str, thinking_model: str, default_model: str, coding_keywords: set, coding_phrases: list) -> tuple[str, str]:
    import re
    lower = message.lower()
    provider = get_effective_provider(config_dict)

    # --- HARD OVERRIDES ---
    if "<coding>" in lower:
        tool_settings = get_model_setting_fn("tool", "default")
        coding_settings = get_model_setting_fn("coding", "answer")
        return (
            get_model_name_for_provider(tool_settings, provider, default_model),
            get_model_name_for_provider(coding_settings, provider, coding_model),
        )

    if "<thinking>" in lower:
        tool_settings = get_model_setting_fn("tool", "default")
        thinking_settings = get_model_setting_fn("thinking", "answer")
        return (
            get_model_name_for_provider(tool_settings, provider, default_model),
            get_model_name_for_provider(thinking_settings, provider, thinking_model),
        )

    # --- NORMAL HEURISTIC FLOW ---
    words = set(re.findall(r"\b\w+\b", lower))

    coding_keywords_lower = {k.lower() for k in coding_keywords}
    coding_phrases_lower = [p.lower() for p in coding_phrases]

    score = 0
    score += len(words & coding_keywords_lower) * 2
    score += sum(1 for p in coding_phrases_lower if p in lower) * 3

    if re.search(r"\b(def|class|import|#include|public\s+static\s+void|function\s*\()", lower):
        score += 4

    if re.search(r"\b(python|java|c\+\+|javascript|typescript|go|rust)\b", lower):
        score += 2

    is_coding = score >= 3

    tool_settings = get_model_setting_fn("tool", "default")
    answer_settings = get_model_setting_fn("coding" if is_coding else "answer", "default")
    tool_model = get_model_name_for_provider(tool_settings, provider, default_model)
    answer_model = get_model_name_for_provider(answer_settings, provider, coding_model if is_coding else default_model)

    return tool_model, answer_model


# --- LLM HTTP logic ---

def _normalize_remote_response(payload: dict) -> SimpleNamespace:
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("empty chat completion response")
    message = choices[0].get("message", {}) or {}
    tool_calls = []
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        tool_calls.append(SimpleNamespace(
            id=tc.get("id"),
            function=SimpleNamespace(
                name=function.get("name"),
                arguments=function.get("arguments", "{}"),
            ),
        ))
    
    # Handle the openrouter empty response bug by retaining the exact empty content
    # We will catch this upstream.
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=message.get("content"),
        tool_calls=tool_calls,
    ))])


async def _provider_chat_completion(provider: str, request_kwargs: dict, config_dict: dict) -> SimpleNamespace:
    if provider == "groq":
        from groq import AsyncGroq
        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
        return await client.chat.completions.create(**request_kwargs)

    if provider == "openrouter":
        payload = dict(request_kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if payload.get("tools") is None:
            payload.pop("tools", None)
        if not payload.get("max_tokens"):
            payload["max_tokens"] = 512
        # Safety net: ensure :free suffix is present when no payment is configured.
        # Normally this is already handled by get_model_name_for_provider, but
        # guard here in case a model name is passed through a different path.
        model = payload.get("model", "")
        if model and not os.getenv("OPENROUTER_HAS_PAYMENT") and ":free" not in model:
            model = f"{model}:free"
            payload["model"] = model
        log.debug("openrouter request model: %s", payload.get("model"))

        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(
                f"{os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code >= 400:
                log.error("OpenRouter error %s: %s", response.status_code, response.text)

            response.raise_for_status()
            return _normalize_remote_response(response.json())

    raise ValueError(f"unsupported provider: {provider}")

def _normalize_messages_for_ollama(messages: list) -> list:
    """Convert OpenAI-style message history to Ollama-compatible format.

    The key difference: Ollama expects tool_calls[].function.arguments to be a
    dict (object), but OpenAI/Groq return — and this codebase stores — arguments
    as a JSON *string*.  Sending a string causes Ollama to reject the request
    with "Value looks like object, but can't find closing '}' symbol".
    """
    normalized = []
    for msg in messages:
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            normalized.append(msg)
            continue
        fixed_tcs = []
        for tc in tool_calls:
            function = tc.get("function") or {}
            args = function.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args and args != "null" else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
            fixed_tcs.append({
                **tc,
                "function": {**function, "arguments": args},
            })
        normalized.append({**msg, "tool_calls": fixed_tcs})
    return normalized


def _build_ollama_payload(request_kwargs: dict, config_dict: dict, stream: bool = False) -> dict:
    """Build the Ollama /api/chat request payload from OpenAI-style request_kwargs."""
    payload: dict = {
        "model": request_kwargs.get("model", config_dict["model"]),
        "messages": _normalize_messages_for_ollama(request_kwargs.get("messages", [])),
        "options": {
            "temperature": request_kwargs.get("temperature", 0.7),
            "num_predict": request_kwargs.get("max_completion_tokens", 50000),
        },
        "stream": stream,
    }
    tools = request_kwargs.get("tools")
    if tools:
        payload["tools"] = tools

    # Thinking flag — set think:true at the top level for models that support it
    # (e.g. qwen3, deepseek-r1). Controlled per-request via request_kwargs or
    # globally via config_dict["ollama_thinking"].
    thinking = request_kwargs.get("thinking", config_dict.get("ollama_thinking", False))
    if thinking:
        payload["think"] = True

    log.debug(
        "_build_ollama_payload | model=%s | stream=%s | tools=%d | think=%s",
        payload["model"],
        stream,
        len(payload.get("tools", [])),
        payload.get("think", False),
    )
    return payload


def _parse_ollama_tool_calls_raw(tool_calls_raw: list[dict]) -> list[SimpleNamespace]:
    """Convert the tool_calls_raw list from a streaming assembled chunk into the
    same SimpleNamespace list that _parse_ollama_message produces.  This lets
    the agent handle tool calls from the streaming path identically to the
    non-streaming path."""
    result = []
    for tc in tool_calls_raw:
        function = tc.get("function") or {}
        result.append(SimpleNamespace(
            id=tc.get("id", f"call_{function.get('name', 'tool')}"),
            function=SimpleNamespace(
                name=function.get("name"),
                arguments=json.dumps(function.get("arguments", {})),
            ),
        ))
    return result


def _parse_ollama_message(msg: dict) -> SimpleNamespace:
    """Normalise a single Ollama message dict into a SimpleNamespace compatible
    with the OpenAI response shape used elsewhere in this codebase."""
    tool_calls = []
    for tc in msg.get("tool_calls", []):
        function = tc.get("function") or {}
        tool_calls.append(SimpleNamespace(
            id=tc.get("id", f"call_{function.get('name', 'tool')}"),
            function=SimpleNamespace(
                name=function.get("name"),
                arguments=json.dumps(function.get("arguments", {})),
            ),
        ))

    # When thinking is enabled Ollama returns a separate "thinking" field on the
    # message. Preserve it so callers can surface it in the UI later.
    ns = SimpleNamespace(
        content=msg.get("content"),
        tool_calls=tool_calls,
        thinking=msg.get("thinking"),  # None when model doesn't support it
    )
    return ns


async def _ollama_chat_completion(request_kwargs: dict, config_dict: dict) -> SimpleNamespace:
    """Non-streaming Ollama completion. Returns a response shaped like the
    OpenAI ChatCompletion object used throughout the codebase.
    Also attaches verbose_stats to the response for token-rate display."""
    payload = _build_ollama_payload(request_kwargs, config_dict, stream=False)
    log.debug("ollama request payload: %s", payload)
    async with httpx.AsyncClient(timeout=240) as client:
        resp = await client.post(f"{config_dict['ollama_base_url']}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        msg = _parse_ollama_message(data.get("message", {}))

        # Capture verbose stats (mirrors --verbose in CLI)
        eval_count = data.get("eval_count")
        eval_duration_ns = data.get("eval_duration")
        prompt_eval_count = data.get("prompt_eval_count")
        prompt_eval_duration_ns = data.get("prompt_eval_duration")
        load_duration_ns = data.get("load_duration")
        total_duration_ns = data.get("total_duration")

        tokens_per_sec = None
        if eval_count and eval_duration_ns and eval_duration_ns > 0:
            tokens_per_sec = round(eval_count / (eval_duration_ns / 1e9), 1)
        prompt_tokens_per_sec = None
        if prompt_eval_count and prompt_eval_duration_ns and prompt_eval_duration_ns > 0:
            prompt_tokens_per_sec = round(prompt_eval_count / (prompt_eval_duration_ns / 1e9), 1)

        verbose_stats = {
            "eval_count": eval_count,
            "tokens_per_sec": tokens_per_sec,
            "prompt_eval_count": prompt_eval_count,
            "prompt_tokens_per_sec": prompt_tokens_per_sec,
            "load_duration_ms": round(load_duration_ns / 1e6, 1) if load_duration_ns else None,
            "total_duration_ms": round(total_duration_ns / 1e6, 1) if total_duration_ns else None,
        }

        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            verbose_stats=verbose_stats,
        )


async def _ollama_stream_chat_completion(request_kwargs: dict, config_dict: dict):
    """Streaming Ollama completion. An async generator that yields
    SimpleNamespace chunks shaped like OpenAI streaming deltas:

        chunk.choices[0].delta.content  — partial text (may be empty string)
        chunk.choices[0].delta.thinking — partial thinking text (or None)
        chunk.choices[0].finish_reason  — None until the final chunk ("stop")

    Tool calls are NOT streamed — if the model returns tool_calls they will
    only be present on the final assembled chunk (finish_reason="tool_calls").
    Callers should buffer content until finish_reason is set, then use the
    assembled full response via _ollama_chat_completion if tool calls are needed.

    This generator is wired up and ready; the agent can switch to it once the
    UI-side streaming renderer is in place.
    """
    payload = _build_ollama_payload(request_kwargs, config_dict, stream=True)
    log.debug("ollama stream request payload: %s", payload)

    # Accumulators for the final assembled message
    full_content: list[str] = []
    full_thinking: list[str] = []
    tool_calls_raw: list[dict] = []

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{config_dict['ollama_base_url']}/api/chat",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk_data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = chunk_data.get("message", {})
                delta_content = msg.get("content") or ""
                delta_thinking = msg.get("thinking")  # present on thinking tokens
                done = chunk_data.get("done", False)

                if delta_content:
                    full_content.append(delta_content)
                if delta_thinking:
                    full_thinking.append(delta_thinking)

                finish_reason = None
                verbose_stats: dict | None = None
                if done:
                    tcs = msg.get("tool_calls", [])
                    if tcs:
                        tool_calls_raw = tcs
                        finish_reason = "tool_calls"
                    else:
                        finish_reason = "stop"

                    # Collect Ollama verbose stats from the final done=true chunk.
                    # These mirror what `ollama run --verbose` prints to the CLI.
                    eval_count = chunk_data.get("eval_count")
                    eval_duration_ns = chunk_data.get("eval_duration")  # nanoseconds
                    prompt_eval_count = chunk_data.get("prompt_eval_count")
                    prompt_eval_duration_ns = chunk_data.get("prompt_eval_duration")
                    load_duration_ns = chunk_data.get("load_duration")
                    total_duration_ns = chunk_data.get("total_duration")

                    tokens_per_sec = None
                    if eval_count and eval_duration_ns and eval_duration_ns > 0:
                        tokens_per_sec = round(eval_count / (eval_duration_ns / 1e9), 1)

                    prompt_tokens_per_sec = None
                    if prompt_eval_count and prompt_eval_duration_ns and prompt_eval_duration_ns > 0:
                        prompt_tokens_per_sec = round(prompt_eval_count / (prompt_eval_duration_ns / 1e9), 1)

                    verbose_stats = {
                        "eval_count": eval_count,
                        "tokens_per_sec": tokens_per_sec,
                        "prompt_eval_count": prompt_eval_count,
                        "prompt_tokens_per_sec": prompt_tokens_per_sec,
                        "load_duration_ms": round(load_duration_ns / 1e6, 1) if load_duration_ns else None,
                        "total_duration_ms": round(total_duration_ns / 1e6, 1) if total_duration_ns else None,
                    }

                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(
                            content=delta_content,
                            thinking=delta_thinking,
                        ),
                        finish_reason=finish_reason,
                    )],
                    # Attach assembled fields on the final chunk for convenience
                    assembled=SimpleNamespace(
                        content="".join(full_content),
                        thinking="".join(full_thinking) or None,
                        tool_calls_raw=tool_calls_raw,
                        verbose_stats=verbose_stats,
                    ) if finish_reason else None,
                )

                if done:
                    break

async def chat_completion_with_retry(provider: str, request_kwargs: dict, config_dict: dict, max_retries: int = 1) -> SimpleNamespace:
    """Wrapper that retries OpenRouter calls if they return empty responses."""
    for attempt in range(max_retries + 1):
        try:
            if provider in {"groq", "openrouter"}:
                response = await _provider_chat_completion(provider, request_kwargs, config_dict)
            else:
                response = await _ollama_chat_completion(request_kwargs, config_dict)

            msg = response.choices[0].message
            content = msg.content
            tool_calls = msg.tool_calls or []

            # Check for empty openrouter response bug
            if provider == "openrouter":
                
                is_empty = not content and not tool_calls
                if is_empty:
                    if attempt < max_retries:
                        log.warning(f"OpenRouter returned empty response (attempt {attempt + 1}). Retrying...")
                        continue
                    else:
                        log.error("OpenRouter returned empty response after retries.")
                        # Inject warning message
                        response.choices[0].message.content = f"[Warning: no response was given from {provider}]"
                        return response
            
            return response
            
        except Exception as e:
            if attempt < max_retries:
                log.warning(f"Chat completion failed on attempt {attempt + 1}: {e}. Retrying...")
                continue
            raise e
