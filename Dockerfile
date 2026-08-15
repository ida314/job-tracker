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
#   docker build --build-arg SIR_CLIENT="git+https://github.com/ida314/stupid--inference-router.git#subdirectory=clients/python" .
#
# Two things here are not obvious and both used to break this silently:
#
#  1. `git` is NOT in python:*-slim, so a `git+…` spec fails with "git executable not
#     found" — the previously documented build arg could never have worked. It is
#     installed and purged inside one RUN so the final layer carries neither git nor its
#     apt lists; nothing at runtime needs it.
#  2. The credential for a private repo must NEVER be a build arg. Build args are
#     recorded in image metadata and readable with `docker history` on the published
#     image. It arrives as a BuildKit secret, which is mounted for this RUN only and is
#     absent from every layer. `git config` would persist it into /root/.gitconfig, so
#     that file is removed in the same layer. A public repo needs no secret at all — the
#     mount is optional and an empty one is simply skipped.
ARG SIR_CLIENT=""
COPY requirements.txt .
RUN --mount=type=secret,id=sir_token \
    pip install --no-cache-dir -r requirements.txt \
 && if [ -n "$SIR_CLIENT" ]; then \
      apt-get update \
   && apt-get install -y --no-install-recommends git \
   && if [ -s /run/secrets/sir_token ]; then \
        git config --global \
          url."https://x-access-token:$(cat /run/secrets/sir_token)@github.com/".insteadOf \
          "https://github.com/"; \
      fi \
   && pip install --no-cache-dir "$SIR_CLIENT"; \
      status=$?; \
      rm -f /root/.gitconfig; \
      apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
      [ $status -eq 0 ]; \
    fi

# Package + curated inputs. companies.yaml / criteria.yaml are baked in; edit and
# rebuild to change targets, or bind-mount over them for local iteration.
#
# answers.yaml is deliberately NOT baked in — it is personal data, and it is gitignored.
# Mount it and point $JOBTRACKER_ANSWERS at it if you want the prefill task. `apply-to`
# is interactive and has no place in a container at all.
COPY jobtracker/ ./jobtracker/
COPY companies.yaml criteria.yaml profile.yaml ./

# Which commit this image was built from. Declared last on purpose: it changes on every
# push, and anything below an ARG is rebuilt when that ARG changes — up here it would
# invalidate the dependency layer nightly for a value pip does not care about.
#
# The ENV is the load-bearing half. A label can only be read by inspecting the image,
# which requires already suspecting something is wrong; the env var reaches the process,
# so every run says which build it is in its first log line and on `service.version`.
# Without it a host that silently failed to pull is indistinguishable from one that did.
ARG GIT_SHA=""
ENV JOBTRACKER_REVISION=$GIT_SHA
LABEL org.opencontainers.image.title="jobtracker" \
      org.opencontainers.image.source="https://github.com/ida314/job-tracker" \
      org.opencontainers.image.revision="$GIT_SHA"

# state.db is written here; mount a host directory to persist it.
ENV JOBTRACKER_DB=/data/state.db
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "jobtracker.cli"]
CMD ["check"]
