# Deterministic job-discovery pipeline. State lives on a mounted volume at /data,
# so the image is disposable and rebuilds never touch state.db.
FROM python:3.14-slim

WORKDIR /app

# Dependencies first for layer caching.
#
# sir-client is not in requirements.txt because it is not on PyPI — it lives in a sibling
# repo. Pass a build arg to install it, or leave it out: without it the model tasks are a
# no-op and `check`, `rank`'s scoring, `report` and `dashboard` all still work. That is
# the same "structurally optional" posture the pass has always had.
#
#   docker build --build-arg SIR_CLIENT="git+ssh://git@github.com/ida314/stupid--inference-router.git#subdirectory=clients/python" .
ARG SIR_CLIENT=""
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ -n "$SIR_CLIENT" ]; then pip install --no-cache-dir "$SIR_CLIENT"; fi

# Package + curated inputs. companies.yaml / criteria.yaml are baked in; edit and
# rebuild to change targets, or bind-mount over them for local iteration.
#
# answers.yaml is deliberately NOT baked in — it is personal data, and it is gitignored.
# Mount it and point $JOBTRACKER_ANSWERS at it if you want the prefill task. `apply-to`
# is interactive and has no place in a container at all.
COPY jobtracker/ ./jobtracker/
COPY companies.yaml criteria.yaml profile.yaml ./

# state.db is written here; mount a host directory to persist it.
ENV JOBTRACKER_DB=/data/state.db
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "jobtracker.cli"]
CMD ["check"]
