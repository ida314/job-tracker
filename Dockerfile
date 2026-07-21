# Deterministic job-discovery pipeline. State lives on a mounted volume at /data,
# so the image is disposable and rebuilds never touch state.db.
FROM python:3.14-slim

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Package + curated inputs. companies.yaml / criteria.yaml are baked in; edit and
# rebuild to change targets, or bind-mount over them for local iteration.
COPY jobtracker/ ./jobtracker/
COPY companies.yaml criteria.yaml ./

# state.db is written here; mount a host directory to persist it.
ENV JOBTRACKER_DB=/data/state.db
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "jobtracker.cli"]
CMD ["check"]
