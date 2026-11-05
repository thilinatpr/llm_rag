"""Thin provider adapters (Anthropic, OpenAI-compatible) with a shared tool-calling interface.

Internal message format (provider-neutral):
  {"role":"user","content":str}
  {"role":"assistant","content":str,"tool_calls":[{"id","name","args"}]}
  {"role":"tool","tool_call_id":str,"name":str,"content":str}
Tool spec: {"name","description","parameters": <JSON schema>}
"""
import json
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list = field(default_factory=list)


def to_anthropic_messages(messages):
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]}
                       for t in m.get("tool_calls", [])]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # merge parallel tool results
            else:
                out.append({"role": "user", "content": [block]})
    return out


def to_openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [  # "" not null: Workers AI rejects null
                {"id": t["id"], "type": "function",
                 "function": {"name": t["name"], "arguments": json.dumps(t["args"])}} for t in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class AnthropicLLM:
    def __init__(self, model, base_url=None, api_key=None):
        import anthropic
        kw = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v}
        self.client, self.model = anthropic.Anthropic(**kw), model

    def chat(self, system, messages, tools=(), max_tokens=2048) -> Reply:
        kw = {}
        if tools:
            kw["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                           for t in tools]
        r = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                        messages=to_anthropic_messages(messages), **kw)
        return Reply("".join(b.text for b in r.content if b.type == "text"),
                     [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"])


class OpenAILLM:
    def __init__(self, model, base_url=None, api_key=None):
        import openai
        kw = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v}
        self.client, self.model = openai.OpenAI(**kw), model

    def chat(self, system, messages, tools=(), max_tokens=2048) -> Reply:
        kw = {}
        if tools:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["parameters"]}} for t in tools]
        r = self.client.chat.completions.create(model=self.model, max_tokens=max_tokens,
                                                messages=to_openai_messages(system, messages), **kw)
        msg = r.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return Reply(msg.content or "", calls)


def make_llm(settings):
    if not settings.chat_model:
        raise SystemExit("Set BILLRAG_CHAT_MODEL (and BILLRAG_CHAT_PROVIDER / BILLRAG_CHAT_BASE_URL if needed).")
    cls = {"anthropic": AnthropicLLM, "openai": OpenAILLM}.get(settings.chat_provider)
    if cls is None:
        raise SystemExit(f"Unknown BILLRAG_CHAT_PROVIDER={settings.chat_provider!r} (use anthropic or openai)")
    return cls(settings.chat_model, settings.chat_base_url, settings.chat_api_key)


# ---------------------------------------------------------------- embeddings
class OpenAIEmbedder:
    def __init__(self, model, base_url=None, api_key=None, batch=64):
        import openai
        kw = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v}
        self.client, self.model, self.batch = openai.OpenAI(**kw), model, batch

    def embed(self, texts):
        import numpy as np
        out = []
        for i in range(0, len(texts), self.batch):
            r = self.client.embeddings.create(model=self.model, input=[t[:8000] for t in texts[i:i + self.batch]])
            out += [d.embedding for d in r.data]
        return np.asarray(out, dtype="float32")


class STEmbedder:
    def __init__(self, model):
        from sentence_transformers import SentenceTransformer
        self.m = SentenceTransformer(model)

    def embed(self, texts):
        return self.m.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)


def make_embedder(settings):
    if settings.embed_provider == "none":
        return None
    if not settings.embed_model:
        raise SystemExit("Set BILLRAG_EMBED_MODEL for the chosen BILLRAG_EMBED_PROVIDER.")
    if settings.embed_provider == "openai":
        return OpenAIEmbedder(settings.embed_model, settings.embed_base_url, settings.embed_api_key)
    if settings.embed_provider in ("sentence-transformers", "st"):
        return STEmbedder(settings.embed_model)
    raise SystemExit(f"Unknown BILLRAG_EMBED_PROVIDER={settings.embed_provider!r}")
