FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Railway injecte $PORT ; on retombe sur 8080 en local.
ENV PORT=8080
EXPOSE 8080

# Un seul worker : l'état de détection vit dans le process, la persistance est
# dans Redis. Deux workers doubleraient les boucles de check et les alertes.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
