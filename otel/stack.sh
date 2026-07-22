#!/usr/bin/env bash
# Bring the tier-3 stack up/down with plain podman.
#
# compose.yaml is the canonical description, but `podman compose` needs a compose
# provider installed (podman-compose or docker-compose) and this machine has neither.
# These are the same four containers, wired the same way, with no extra tooling.
#
#   otel/stack.sh up     # start collector + jaeger + prometheus + grafana
#   otel/stack.sh down   # stop and remove them
#   otel/stack.sh run    # run `check` through the stack
#
# Containers use --rm, so `down` leaves nothing behind except the named network.

set -euo pipefail
cd "$(dirname "$0")/.."

NET=jt-otel
COLLECTOR=docker.io/otel/opentelemetry-collector-contrib:0.116.1
JAEGER=docker.io/jaegertracing/all-in-one:1.65.0
PROMETHEUS=docker.io/prom/prometheus:v3.1.0
GRAFANA=docker.io/grafana/grafana:11.4.0

case "${1:-}" in
  up)
    podman network exists "$NET" || podman network create "$NET"

    podman run -d --rm --name jaeger --network "$NET" \
      -e COLLECTOR_OTLP_ENABLED=true -p 16686:16686 "$JAEGER" >/dev/null

    # --web.enable-remote-write-receiver is required: the collector pushes to Prometheus
    # rather than being scraped, because a 30s batch job is never up when a scrape lands.
    podman run -d --rm --name prometheus --network "$NET" -p 9090:9090 \
      -v "$PWD/otel/prometheus.yml:/etc/prometheus/prometheus.yml:z,ro" \
      "$PROMETHEUS" \
      --config.file=/etc/prometheus/prometheus.yml \
      --web.enable-remote-write-receiver >/dev/null

    podman run -d --rm --name grafana --network "$NET" -p 3000:3000 \
      -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Admin \
      -e GF_AUTH_DISABLE_LOGIN_FORM=true \
      -v "$PWD/otel/grafana-datasources.yml:/etc/grafana/provisioning/datasources/ds.yml:z,ro" \
      "$GRAFANA" >/dev/null

    sleep 3
    podman run -d --rm --name otel-collector --network "$NET" -p 4318:4318 -p 4317:4317 \
      -v "$PWD/otel/collector.yaml:/etc/otel/collector.yaml:z,ro" \
      "$COLLECTOR" --config=/etc/otel/collector.yaml >/dev/null

    echo "Jaeger   http://localhost:16686"
    echo "Grafana  http://localhost:3000"
    echo "Prom     http://localhost:9090"
    ;;

  down)
    podman rm -f otel-collector jaeger prometheus grafana 2>/dev/null || true
    podman network rm "$NET" 2>/dev/null || true
    echo "stack down"
    ;;

  run)
    shift
    # host.containers.internal reaches the host's published 4318 from inside the job
    # container; --network jt-otel would also work and skip the host hop.
    podman run --rm -v "$PWD/data:/data:Z" \
      -e JOBTRACKER_TELEMETRY=otlp \
      -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
      --network "$NET" jobtracker:latest check "$@"
    ;;

  *)
    echo "usage: otel/stack.sh {up|down|run}" >&2
    exit 64
    ;;
esac
