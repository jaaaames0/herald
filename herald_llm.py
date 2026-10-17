"""Minimal NanoGPT (OpenAI-compatible) chat client. Stdlib only."""

import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("HERALD_LLM_URL", "https://nano-gpt.com/api/v1/chat/completions")


class LLMError(RuntimeError):
    pass


def chat(model, messages, max_tokens=4000, temperature=0.4, json_mode=False,
         reasoning_effort=None, timeout=300, retries=1):
    """Return (content, info) where info has tokens, cost_usd, finish_reason, model."""
    key = os.environ.get("NANOGPT_API_KEY")
    if not key:
        raise LLMError("NANOGPT_API_KEY is not set")
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code >= 500 and attempt < retries:
                time.sleep(10)
                continue
            raise LLMError(f"HTTP {e.code} from {model}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(10)
                continue
            raise LLMError(f"{model}: {e}") from e
    if data.get("error"):
        raise LLMError(f"{model}: {data['error']}")
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    content = (choice.get("message") or {}).get("content") or ""
    info = {
        "model": data.get("model") or model,
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": usage.get("reasoning_tokens") or 0,
        "cost_usd": float(usage.get("cost") or (data.get("x_nanogpt_pricing") or {}).get("cost") or 0),
        "finish_reason": choice.get("finish_reason"),
    }
    return content, info


def parse_json(content):
    """Parse a JSON object from model output, tolerating code fences and preamble."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
