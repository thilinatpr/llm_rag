# Build from the repo root (image_build_context in wrangler.toml). The index is baked in: public documents only (the 2027 Appropriation Bill and
# Budget Speech), about 1 MB, so the container needs no R2 credentials. Build it with `python -m budget build` and publish it with
# `python scripts/budget_index.py upload`; the deploy job fetches it (deploy.yml, target budget).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install openai
COPY billrag ./billrag
COPY budget/__init__.py budget/ask.py budget/index.py budget/parse.py budget/speech.py budget/serve.py ./budget/
COPY data/budget/index.sqlite ./data/budget/index.sqlite
EXPOSE 8000
CMD ["python", "-m", "budget.serve"]
