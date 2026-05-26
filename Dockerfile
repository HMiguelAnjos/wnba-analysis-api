FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
# scripts/ é necessário em runtime: o backfill_worker importa
# scripts.backfill_outcomes pra preencher actual_outcome no line_log.
# Sem isto, o import falha em produção e o backfill nunca roda.
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
