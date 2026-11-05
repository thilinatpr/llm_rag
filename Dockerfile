FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-serve.txt .
RUN pip install -r requirements-serve.txt
COPY billrag ./billrag
COPY scripts/fetch_index.py scripts/upload_r2.py ./scripts/
COPY docker/entrypoint.sh ./entrypoint.sh
EXPOSE 8000
CMD ["./entrypoint.sh"]
