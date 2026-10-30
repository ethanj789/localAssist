import logging

from config import (
    CONFIG, CODING_MODEL, SUMMARY_MODEL, THINKING_MODEL,
    get_model_setting, CODING_KEYWORDS, CODING_PHRASES,
)
import llm_provider
import prompts

log = logging.getLogger(__name__)


async def generate_and_save_title(conversation_id: str, first_message: str):
    import db
    try:
        provider = llm_provider.get_effective_provider(CONFIG)
        if provider == "ollama":
            return

        title_prompt = prompts.TITLE_GENERATION_PROMPT_TEMPLATE.format(first_message=first_message)
        title = ""
        provider = llm_provider.get_effective_provider(CONFIG)
        title_settings = get_model_setting("title", "default")
        request_kwargs = {
            "model": CONFIG["model"] if provider == "ollama" else llm_provider.get_model_name_for_provider(title_settings, provider, SUMMARY_MODEL),
            "messages": [
                {"role": "user", "content": title_prompt}
            ],
            "max_completion_tokens": title_settings.get("max_tokens", 200),
            "temperature": title_settings.get("temperature", 0.5),
        }
        reasoning_effort = title_settings.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort.strip():
            request_kwargs["reasoning_effort"] = reasoning_effort.strip()

        response = await llm_provider.chat_completion_with_retry(provider, request_kwargs, CONFIG)
        title = response.choices[0].message.content.strip()

        title = title.strip().strip('"').strip("'").strip()
        if title:
            title = title[:50]  # truncate to prevent layout issues
            db.update_conversation_title(conversation_id, title)
            log.info(f"Generated title for {conversation_id}: {title}")
    except Exception as e:
        log.error(f"Failed to generate title for {conversation_id}: {e}")


def _resolve_turn_models(resolved_provider: str, use_remote_provider: bool, model_override: str, user_message: str) -> tuple[str, str]:
    """Resolve (tool_model, answer_model) for a turn — factored out so both the
    live agent turn and the resume-after-confirmation path pick models the same
    way. Does NOT emit the `model_upgrade` SSE; the caller compares the two and
    emits it when they differ (only the live path does)."""
    if use_remote_provider:
        tool_settings = get_model_setting("tool", "default")
        if model_override == "coding":
            return (
                llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                llm_provider.get_model_name_for_provider(get_model_setting("coding", "answer"), resolved_provider, CODING_MODEL),
            )
        elif model_override == "thinking":
            return (
                llm_provider.get_model_name_for_provider(tool_settings, resolved_provider, CONFIG.get("groq_model")),
                llm_provider.get_model_name_for_provider(get_model_setting("thinking", "answer"), resolved_provider, THINKING_MODEL),
            )
        else:
            return llm_provider.select_model(
                user_message, CONFIG, get_model_setting, CODING_MODEL, THINKING_MODEL, CONFIG.get("groq_model"), CODING_KEYWORDS, CODING_PHRASES
            )
    else:
        default_settings = get_model_setting("default", "default")
        model = default_settings.get("model", CONFIG["model"])
        return (model, model)
