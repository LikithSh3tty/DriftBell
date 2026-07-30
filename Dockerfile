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

# --if-empty, not a bare reseed: this runs on every container start, and an
# unconditional reseed drops the runs row and registry promotion a retrain
# produces. Reset explicitly with `python seed_db.py` when that is what you want.
CMD ["sh", "-c", "python seed_db.py --if-empty && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
