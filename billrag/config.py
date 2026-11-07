"""Environment-driven settings. Everything model-related is swappable via env vars.

Chat model
  BILLRAG_CHAT_PROVIDER   anthropic (default) | openai   ("openai" = any OpenAI-compatible endpoint)
  BILLRAG_CHAT_MODEL      model id (default for anthropic: claude-sonnet-5-5; required for openai)
  BILLRAG_CHAT_BASE_URL   optional endpoint override (Ollama, vLLM, LM Studio, gateways ...)
  BILLRAG_CHAT_API_KEY    optional; otherwise the SDK's own env var (ANTHROPIC_API_KEY / OPENAI_API_KEY)
  BILLRAG_FALLBACK_MODEL  optional: an OpenAI-compatible model to use when the chat model's provider fails (llm.FallbackLLM), with
  BILLRAG_FALLBACK_BASE_URL / BILLRAG_FALLBACK_API_KEY

Embeddings (optional; without them retrieval is BM25 + structure-aware, which already works well)
  BILLRAG_EMBED_PROVIDER  default: "cloudflare" when CLOUDFLARE_API_TOKEN + an account id are set, else "none"
                          none | cloudflare | openai | sentence-transformers
  BILLRAG_EMBED_MODEL     e.g. text-embedding-3-large, or BAAI/bge-small-en-v1.5
  BILLRAG_EMBED_BASE_URL / BILLRAG_EMBED_API_KEY   for OpenAI-compatible embedding servers

Pipeline (retrieve -> grade -> summarise)
  BILLRAG_ANSWER_MODEL    optional: a different chat model (same provider/endpoint) for the final answer step only;
                          grading/condense/judging keep BILLRAG_CHAT_MODEL
  BILLRAG_GRADE_EFFORT    reasoning effort for the grading/condense calls (default low; empty = omit the field)
  BILLRAG_ANSWER_EFFORT   reasoning effort for the answer call (default: model default)

Translation (questions in other languages; the index is English only - see translate.py)
  BILLRAG_TRANSLATE       off | script (default: Sinhala/Tamil by Unicode script, no extra call for English) |
                          ai (one model call names ANY language and translates; +1 call per non-Sinhala/Tamil question)
  BILLRAG_TRANSLATE_MODEL optional model for the translate calls only (default: the chat model). A chat model on the same
                          provider/endpoint (e.g. @cf/google/gemma-3-12b-it) or a Workers AI MT model (@cf/meta/m2m100-1.2b)

Retrieval
  BILLRAG_RERANK          default: "cloudflare" when Cloudflare credentials are set, else "none"
                          none | llm | cloudflare | cloudflare-fused
                          llm: chat model scores candidates; cloudflare: Workers AI cross-encoder reorders them;
                          cloudflare-fused: RRF of the cross-encoder order and the first-stage order
  BILLRAG_RERANK_MODEL    default @cf/baai/bge-reranker-base

Cloudflare Workers AI (embedding provider "cloudflare", rerank "cloudflare*")
  CLOUDFLARE_API_TOKEN    token with Workers AI permission
  BILLRAG_CF_ACCOUNT_ID   (falls back to CLOUDFLARE_ACCOUNT_ID, then R2_ACCOUNT_ID)
  BILLRAG_EMBED_MODEL     e.g. @cf/baai/bge-base-en-v1.5
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
    rerank_model: str
    answer_model: str | None
    grade_effort: str | None
    answer_effort: str | None
    translate: str
    translate_model: str | None
    cf_account_id: str | None
    cf_api_token: str | None
    fallback_model: str | None = None
    fallback_base_url: str | None = None
    fallback_api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    @property
    def vectors_path(self) -> Path:
        return self.data_dir / "vectors.npz"


def load_settings() -> Settings:
    e = os.environ.get
    provider = e("BILLRAG_CHAT_PROVIDER", "anthropic").lower()
    cf_account = e("BILLRAG_CF_ACCOUNT_ID") or e("CLOUDFLARE_ACCOUNT_ID") or e("R2_ACCOUNT_ID") or None
    cf_token = e("CLOUDFLARE_API_TOKEN") or None
    cf_default = "cloudflare" if (cf_account and cf_token) else "none"  # best measured config, if usable
    default_model = "claude-sonnet-5-5" if provider == "anthropic" else ""
    return Settings(
        data_dir=Path(e("BILLRAG_DATA", "data/bills")),
        chat_provider=provider,
        chat_model=e("BILLRAG_CHAT_MODEL", default_model),
        chat_base_url=e("BILLRAG_CHAT_BASE_URL") or None,
        chat_api_key=e("BILLRAG_CHAT_API_KEY") or None,
        embed_provider=e("BILLRAG_EMBED_PROVIDER", cf_default).lower(),
        embed_model=e("BILLRAG_EMBED_MODEL", ""),
        embed_base_url=e("BILLRAG_EMBED_BASE_URL") or None,
        embed_api_key=e("BILLRAG_EMBED_API_KEY") or None,
        rerank=e("BILLRAG_RERANK", cf_default).lower(),
        rerank_model=e("BILLRAG_RERANK_MODEL", "@cf/baai/bge-reranker-base"),
        answer_model=e("BILLRAG_ANSWER_MODEL") or None,
        grade_effort=(e("BILLRAG_GRADE_EFFORT", "low") or None),
        answer_effort=(e("BILLRAG_ANSWER_EFFORT") or None),
        translate=e("BILLRAG_TRANSLATE", "script").lower(),
        translate_model=e("BILLRAG_TRANSLATE_MODEL") or None,
        cf_account_id=cf_account,
        cf_api_token=cf_token,
        fallback_model=e("BILLRAG_FALLBACK_MODEL") or None,
        fallback_base_url=e("BILLRAG_FALLBACK_BASE_URL") or None,
        fallback_api_key=e("BILLRAG_FALLBACK_API_KEY") or None,
    )
