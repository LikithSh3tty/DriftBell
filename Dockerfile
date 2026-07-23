FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed_db.py .

# Checkpoints and the synthetic MLOps history live on a volume so a container
# rebuild never loses a pending approval.
VOLUME /data
ENV DRIFTBELL_DB=/data/driftbell.db \
    CHECKPOINT_DB=/data/checkpoints.db

EXPOSE 8000

CMD ["sh", "-c", "python seed_db.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
