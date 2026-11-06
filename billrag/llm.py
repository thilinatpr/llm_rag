"""Thin provider adapters (Anthropic, OpenAI-compatible) with a shared tool-calling interface.

Internal message format (provider-neutral):
  {"role":"user","content":str}
  {"role":"assistant","content":str,"tool_calls":[{"id","name","args"}]}
  {"role":"tool","tool_call_id":str,"name":str,"content":str}
Tool spec: {"name","description","parameters": <JSON schema>}
"""
import json
import time
from dataclasses import dataclass, field

from . import calltrace


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop: str = ""  # provider stop reason; "length" / "max_tokens" = the reply was cut off at max_tokens

    @property
    def truncated(self):
        return self.stop in ("length", "max_tokens")


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

    def chat(self, system, messages, tools=(), max_tokens=2048, effort=None, on_token=None) -> Reply:  # effort: ignored
        if on_token is not None and not tools:
            t, first = time.time(), None
            with self.client.messages.stream(model=self.model, max_tokens=max_tokens, system=system,
                                             messages=to_anthropic_messages(messages)) as st:
                for piece in st.text_stream:
                    first = first if first is not None else time.time() - t
                    on_token(piece)
                r = st.get_final_message()
            calltrace.record("chat", time.time() - t, model=self.model, stop=getattr(r, "stop_reason", None),
                             max_tokens=max_tokens, stream=True, first=round(first, 2) if first is not None else None,
                             **calltrace.usage_fields(getattr(r, "usage", None)))
            return Reply("".join(b.text for b in r.content if b.type == "text"), [], getattr(r, "stop_reason", "") or "")
        kw = {}
        if tools:
            kw["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                           for t in tools]
        t = time.time()
        try:
            r = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                            messages=to_anthropic_messages(messages), **kw)
        except Exception as e:
            calltrace.record("chat", time.time() - t, model=self.model, error=type(e).__name__, max_tokens=max_tokens)
            raise
        calltrace.record("chat", time.time() - t, model=self.model, stop=getattr(r, "stop_reason", None),
                         max_tokens=max_tokens, **calltrace.usage_fields(getattr(r, "usage", None)))
        return Reply("".join(b.text for b in r.content if b.type == "text"),
                     [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"],
                     getattr(r, "stop_reason", "") or "")


class OpenAILLM:
    def __init__(self, model, base_url=None, api_key=None):
        import openai
        kw = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v}
        self.client, self.model = openai.OpenAI(max_retries=6, **kw), model

    def chat(self, system, messages, tools=(), max_tokens=2048, effort=None, on_token=None) -> Reply:
        """on_token(text): stream the reply, calling it with each piece of visible text (not with reasoning)."""
        if on_token is not None and not tools:
            return self._stream(system, messages, max_tokens, effort, on_token)
        kw = {}
        if effort:  # reasoning models (gpt-oss): low/medium/high; sent as an extra body field
            kw["extra_body"] = {"reasoning_effort": effort}
        if tools:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["parameters"]}} for t in tools]
        t, comp = time.time(), self.client.chat.completions
        kw.update(model=self.model, max_tokens=max_tokens, messages=to_openai_messages(system, messages))
        try:
            raw = getattr(comp, "with_raw_response", None)  # raw response: also says how many retries the SDK made
            raw = raw.create(**kw) if raw is not None else None
            r = raw.parse() if raw is not None else comp.create(**kw)
        except Exception as e:
            calltrace.record("chat", time.time() - t, model=self.model, error=type(e).__name__, max_tokens=max_tokens,
                             effort=effort)
            raise
        calltrace.record("chat", time.time() - t, model=self.model, stop=getattr(r.choices[0], "finish_reason", None),
                         retries=getattr(raw, "retries_taken", None), max_tokens=max_tokens, effort=effort,
                         **calltrace.usage_fields(getattr(r, "usage", None)))
        msg = r.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return Reply(msg.content or "", calls, getattr(r.choices[0], "finish_reason", "") or "")

    def _stream(self, system, messages, max_tokens, effort, on_token):
        kw = {"extra_body": {"reasoning_effort": effort}} if effort else {}
        kw.update(model=self.model, max_tokens=max_tokens, messages=to_openai_messages(system, messages), stream=True,
                  stream_options={"include_usage": True})
        t, first, parts, stop, usage, think = time.time(), None, [], "", None, 0
        try:
            for chunk in self.client.chat.completions.create(**kw):
                usage = getattr(chunk, "usage", None) or usage
                for ch in getattr(chunk, "choices", None) or []:
                    # hidden reasoning arrives in its own field (Workers AI: reasoning_content / reasoning): count, never show
                    think += len(getattr(ch.delta, "reasoning_content", None) or getattr(ch.delta, "reasoning", None) or "")
                    piece = getattr(ch.delta, "content", None)
                    if piece:
                        first = first if first is not None else time.time() - t
                        parts.append(piece)
                        on_token(piece)
                    stop = getattr(ch, "finish_reason", None) or stop
        except Exception as e:
            calltrace.record("chat", time.time() - t, model=self.model, error=type(e).__name__, max_tokens=max_tokens,
                             effort=effort, stream=True, first=round(first, 2) if first is not None else None)
            raise
        calltrace.record("chat", time.time() - t, model=self.model, stop=stop or None, max_tokens=max_tokens, effort=effort,
                         stream=True, first=round(first, 2) if first is not None else None,
                         think_chars=think or None, text_chars=sum(map(len, parts)), **calltrace.usage_fields(usage))
        return Reply("".join(parts), [], stop)


def make_llm(settings):
    if not settings.chat_model:
        raise SystemExit("Set BILLRAG_CHAT_MODEL (and BILLRAG_CHAT_PROVIDER / BILLRAG_CHAT_BASE_URL if needed).")
    cls = {"anthropic": AnthropicLLM, "openai": OpenAILLM}.get(settings.chat_provider)
    if cls is None:
        raise SystemExit(f"Unknown BILLRAG_CHAT_PROVIDER={settings.chat_provider!r} (use anthropic or openai)")
    return cls(settings.chat_model, settings.chat_base_url, settings.chat_api_key)


def make_answer_llm(settings):
    """Optional separate model for the answer step (BILLRAG_ANSWER_MODEL); None means use the main chat model."""
    if not settings.answer_model or settings.answer_model == settings.chat_model:
        return None
    import dataclasses
    return make_llm(dataclasses.replace(settings, chat_model=settings.answer_model))


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


class _Cloudflare:
    """Minimal Workers AI REST client (https://api.cloudflare.com/.../ai/run/<model>)."""

    def __init__(self, account, token):
        if not account or not token:
            raise SystemExit("Cloudflare provider needs CLOUDFLARE_API_TOKEN and an account id "
                             "(BILLRAG_CF_ACCOUNT_ID / CLOUDFLARE_ACCOUNT_ID / R2_ACCOUNT_ID).")
        self.account, self.token = account, token

    def _post(self, model, body, retries=4):
        import urllib.error
        import urllib.request
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account}/ai/run/{model}"
        n = len(body.get("text") or body.get("contexts") or []) or None
        t0 = time.time()
        for attempt in range(retries):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = json.load(r)["result"]
                calltrace.record("ai_run", time.time() - t0, model=model, n=n, attempts=attempt + 1)
                return out
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                    calltrace.record("ai_run", time.time() - t0, model=model, n=n, attempts=attempt + 1, error=f"HTTP {e.code}")
                    raise RuntimeError(f"Workers AI {model}: HTTP {e.code} {e.read().decode()[:200]}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    calltrace.record("ai_run", time.time() - t0, model=model, n=n, attempts=attempt + 1, error=type(e).__name__)
                    raise
            time.sleep(2 ** (attempt + 1))


class CloudflareEmbedder(_Cloudflare):
    def __init__(self, model, account, token, batch=50, workers=4, max_chars=2000):
        super().__init__(account, token)
        self.model, self.batch, self.workers, self.max_chars = model, batch, workers, max_chars

    def embed(self, texts):
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor
        parts = [[(t or " ")[: self.max_chars] for t in texts[i:i + self.batch]]
                 for i in range(0, len(texts), self.batch)]
        with ThreadPoolExecutor(self.workers) as ex:
            res = list(ex.map(lambda p: self._post(self.model, {"text": p})["data"], parts))
        return np.asarray([v for r in res for v in r], dtype="float32")


class CloudflareReranker(_Cloudflare):
    """Cross-encoder: scores each (query, passage) pair; returns scores aligned with the input order."""

    def __init__(self, model, account, token, max_chars=1500):
        super().__init__(account, token)
        self.model, self.max_chars = model, max_chars

    def scores(self, query, texts):
        r = self._post(self.model, {"query": query, "contexts": [{"text": (t or " ")[: self.max_chars]} for t in texts]})
        out = [0.0] * len(texts)
        for item in r["response"]:
            out[item["id"]] = float(item["score"])
        return out


def make_reranker(settings):
    if settings.rerank.startswith("cloudflare"):
        return CloudflareReranker(settings.rerank_model, settings.cf_account_id, settings.cf_api_token)
    return None


def make_embedder(settings):
    if settings.embed_provider == "none":
        return None
    if settings.embed_provider == "cloudflare":
        return CloudflareEmbedder(settings.embed_model or "@cf/baai/bge-base-en-v1.5",
                                  settings.cf_account_id, settings.cf_api_token)
    if not settings.embed_model:
        raise SystemExit("Set BILLRAG_EMBED_MODEL for the chosen BILLRAG_EMBED_PROVIDER.")
    if settings.embed_provider == "openai":
        return OpenAIEmbedder(settings.embed_model, settings.embed_base_url, settings.embed_api_key)
    if settings.embed_provider in ("sentence-transformers", "st"):
        return STEmbedder(settings.embed_model)
    raise SystemExit(f"Unknown BILLRAG_EMBED_PROVIDER={settings.embed_provider!r}")
