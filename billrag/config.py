"""Environment-driven settings. Everything model-related is swappable via env vars.

Chat model
  BILLRAG_CHAT_PROVIDER   anthropic (default) | openai   ("openai" = any OpenAI-compatible endpoint)
  BILLRAG_CHAT_MODEL      model id (default for anthropic: claude-sonnet-5-5; required for openai)
  BILLRAG_CHAT_BASE_URL   optional endpoint override (Ollama, vLLM, LM Studio, gateways ...)
  BILLRAG_CHAT_API_KEY    optional; otherwise the SDK's own env var (ANTHROPIC_API_KEY / OPENAI_API_KEY)

Embeddings (optional; without them retrieval is BM25 + structure-aware, which already works well)
  BILLRAG_EMBED_PROVIDER  none (default) | openai | sentence-transformers
  BILLRAG_EMBED_MODEL     e.g. text-embedding-3-large, or BAAI/bge-small-en-v1.5
  BILLRAG_EMBED_BASE_URL / BILLRAG_EMBED_API_KEY   for OpenAI-compatible embedding servers

Retrieval
  BILLRAG_RERANK          none (default) | llm   (LLM scores the fused candidates)
  BILLRAG_DATA            data directory (default data/bills)
"""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path
    chat_provider: str
    chat_model: str
    chat_base_url: str | None
    chat_api_key: str | None
    embed_provider: str
    embed_model: str
    embed_base_url: str | None
    embed_api_key: str | None
    rerank: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    @property
    def vectors_path(self) -> Path:
        return self.data_dir / "vectors.npz"


def load_settings() -> Settings:
    e = os.environ.get
    provider = e("BILLRAG_CHAT_PROVIDER", "anthropic").lower()
    default_model = "claude-sonnet-5-5" if provider == "anthropic" else ""
    return Settings(
        data_dir=Path(e("BILLRAG_DATA", "data/bills")),
        chat_provider=provider,
        chat_model=e("BILLRAG_CHAT_MODEL", default_model),
        chat_base_url=e("BILLRAG_CHAT_BASE_URL") or None,
        chat_api_key=e("BILLRAG_CHAT_API_KEY") or None,
        embed_provider=e("BILLRAG_EMBED_PROVIDER", "none").lower(),
        embed_model=e("BILLRAG_EMBED_MODEL", ""),
        embed_base_url=e("BILLRAG_EMBED_BASE_URL") or None,
        embed_api_key=e("BILLRAG_EMBED_API_KEY") or None,
        rerank=e("BILLRAG_RERANK", "none").lower(),
    )
