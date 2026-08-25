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

# `python -m app.main` et non `uvicorn` : le bot Discord tourne dans la même
# event loop que le serveur, et la CLI uvicorn ne lancerait que le serveur.
# Un seul process de toute façon — l'état de détection y vit, deux workers
# doubleraient les boucles de check et les alertes.
CMD ["python", "-m", "app.main"]
