# Build from the repo root (image_build_context in wrangler.toml). The index is baked in: only public documents, 12 MB,
# so the container needs no R2 credentials. Rebuild it with `python -m procurement build` before deploying.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install openai
COPY billrag ./billrag
COPY procurement/__init__.py procurement/ask.py procurement/index.py procurement/law.py procurement/parse.py procurement/serve.py procurement/sources.py procurement/tables.py ./procurement/
COPY data/procurement/index.sqlite ./data/procurement/index.sqlite
EXPOSE 8000
CMD ["python", "-m", "procurement.serve"]
