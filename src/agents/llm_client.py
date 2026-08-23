"""
Thin shared helper for calling Claude with a system prompt + user prompt and
getting back parsed JSON. Centralizing this means every agent gets the same
hallucination-safety net: if the model doesn't return valid JSON, we raise a
clear, catchable error rather than silently proceeding on garbage.

Agents pass in a `client` (an Anthropic-compatible chat model) so tests can
inject a stub that returns canned responses without hitting the network.
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional, Protocol

from langchain_anthropic import ChatAnthropic

from src.config import MODEL


class ChatModelLike(Protocol):
    def invoke(self, messages: list) -> Any: ...


class LLMParseError(Exception):
    """Raised when the model's response could not be parsed as JSON."""


import os


def build_reasoning_client(model_name: Optional[str] = None) -> Any:
    provider = os.getenv("CHAOS_LLM_PROVIDER", "").lower()
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if provider == "gemini" or (not provider and gemini_key and not anthropic_key):
        from langchain_google_genai import ChatGoogleGenerativeAI
        m = model_name if (model_name and "claude" not in model_name) else "gemini-2.5-flash"
        return ChatGoogleGenerativeAI(model=m, max_output_tokens=MODEL.max_tokens)
    elif provider == "openai" or (not provider and openai_key and not anthropic_key):
        from langchain_openai import ChatOpenAI
        m = model_name if (model_name and "claude" not in model_name) else "gpt-4o"
        return ChatOpenAI(model=m, max_tokens=MODEL.max_tokens)
    else:
        m = model_name or MODEL.reasoning_model
        return ChatAnthropic(model=m, max_tokens=MODEL.max_tokens)


def build_monitor_client(model_name: Optional[str] = None) -> Any:
    provider = os.getenv("CHAOS_LLM_PROVIDER", "").lower()
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if provider == "gemini" or (not provider and gemini_key and not anthropic_key):
        from langchain_google_genai import ChatGoogleGenerativeAI
        m = model_name if (model_name and "claude" not in model_name) else "gemini-2.5-flash"
        return ChatGoogleGenerativeAI(model=m, max_output_tokens=MODEL.max_tokens)
    elif provider == "openai" or (not provider and openai_key and not anthropic_key):
        from langchain_openai import ChatOpenAI
        m = model_name if (model_name and "claude" not in model_name) else "gpt-4o-mini"
        return ChatOpenAI(model=m, max_tokens=MODEL.max_tokens)
    else:
        m = model_name or MODEL.monitor_model
        return ChatAnthropic(model=m, max_tokens=MODEL.max_tokens)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    # Strip markdown code fences if the model added them despite instructions.
    text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMParseError(f"Could not parse JSON from model output: {e}\nRaw: {text[:500]}")
    raise LLMParseError(f"No JSON object found in model output.\nRaw: {text[:500]}")


def invoke_structured(
    client: ChatModelLike,
    system_prompt: str,
    user_prompt: str,
) -> Dict[str, Any]:
    """
    Invoke the model with a system+user prompt pair, requiring a JSON-only
    response, and return the parsed dict. Raises LLMParseError on malformed
    output so callers can treat it as a safety event rather than a crash.
    """
    full_system = (
        system_prompt.strip()
        + "\n\nCRITICAL: Respond with ONLY a single valid JSON object. "
          "No prose, no markdown code fences, no preamble."
    )
    response = client.invoke([
        {"role": "system", "content": full_system},
        {"role": "user", "content": user_prompt},
    ])
    content = response.content if hasattr(response, "content") else str(response)
    if isinstance(content, list):
        # Anthropic content blocks -- concatenate only text blocks (ignore thinking blocks).
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    text_parts.append(block.get("text", ""))
            elif hasattr(block, "text"):
                text_parts.append(str(block.text))
            elif isinstance(block, str):
                text_parts.append(block)
        content = "".join(text_parts)
    return _extract_json(content)
