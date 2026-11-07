FROM python:3.12-slim
# One BLAS thread: the host shows many cores while the instance gets a fraction of one, and extra threads only use up that share waiting (measured: 8 ms vs 200-800 ms per scan).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements-serve.txt .
RUN pip install -r requirements-serve.txt
COPY billrag ./billrag
COPY scripts/fetch_index.py scripts/upload_r2.py ./scripts/
COPY docker/entrypoint.sh ./entrypoint.sh
EXPOSE 8000
CMD ["./entrypoint.sh"]
