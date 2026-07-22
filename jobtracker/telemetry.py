"""OpenTelemetry wiring. The *only* file that touches the OTel SDK.

If you read one thing about OpenTelemetry, read this:

    THE API AND THE SDK ARE TWO DIFFERENT PACKAGES, ON PURPOSE.

  * `opentelemetry-api` is what instrumented code imports. `fetch.py` uses only this.
    On its own it is a **no-op**: `tracer.start_as_current_span(...)` costs a couple of
    attribute lookups and records nothing. There is no exporter, no buffer, no network.

  * `opentelemetry-sdk` is what an *application* installs, once, at startup — right here.
    Installing it swaps the no-op implementation for a real one, and suddenly every span
    the API already created starts being recorded.

Why that matters: library code instruments unconditionally and never asks "is tracing
on?", because when tracing is off the calls genuinely do nothing. The application — not
the library — decides whether anything is collected and where it goes. That is why
`fetch.py` has no `if telemetry_enabled:` anywhere in it, and why telemetry defaults to
off here without `fetch.py` caring.

Three moving parts get assembled below:

    Resource         who is emitting (service.name — how you'll find this in a backend)
    SpanExporter     where spans go (your terminal, or an OTLP collector)
    SpanProcessor    when they go (immediately, or batched in a background thread)
                     TracerProvider ties the three together and hands out tracers.

Usage:

    jobtracker --telemetry console check     # spans printed as JSON to stderr
    jobtracker --telemetry otlp check        # spans shipped to a collector (tier 3)

`otlp` honors the standard env vars, so there is nothing app-specific to configure:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
"""

from __future__ import annotations

import atexit
import logging
import os
import sys

log = logging.getLogger(__name__)

MODES = ("off", "console", "otlp")
DEFAULT_MODE = os.environ.get("JOBTRACKER_TELEMETRY", "off")

_provider = None
_meter_provider = None


def _delta_temporality() -> dict:
    """Report counters as per-run deltas rather than process-lifetime totals.

    This is the single most important tier-3 decision for this project, and it exists
    entirely because we are a *batch job*, not a server.

    A cumulative counter means "total since this process started". That works when the
    process runs for months. Ours lives 30 seconds, so every run starts back at zero, and
    a backend seeing 2 retries yesterday and 3 today concludes one new retry occurred —
    it cannot distinguish a restart from a decrease. The numbers are quietly wrong.

    Delta means "this much happened in this interval", which is exactly what a run is.
    Process restarts stop mattering. The collector's `deltatocumulative` processor then
    rebuilds a proper monotonic series spanning every run, so `increase()` works.
    """
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    delta = AggregationTemporality.DELTA
    cumulative = AggregationTemporality.CUMULATIVE
    return {
        Counter: delta,
        Histogram: delta,
        ObservableCounter: delta,
        # UpDownCounters stay cumulative: they represent a current level, and a delta of
        # a level is meaningless.
        UpDownCounter: cumulative,
        ObservableUpDownCounter: cumulative,
    }


def configure(mode: str = DEFAULT_MODE, service_name: str = "jobtracker") -> bool:
    """Install the SDK. Returns True if tracing is actually recording.

    Called once, from the CLI entry point. Calling it with mode="off" is the same as not
    calling it at all — the API's no-op implementation stays in place.
    """
    global _provider, _meter_provider

    if mode not in MODES:
        raise ValueError(f"unknown telemetry mode {mode!r} — pick one of {MODES}")
    if mode == "off":
        return False
    if _provider is not None:  # idempotent; a second provider would be ignored anyway
        return True

    # Imported lazily so `mode="off"` never pays for loading the SDK.
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

    # 1. Resource — immutable facts about *this process*, attached to every span it emits.
    #    service.name is the one attribute every backend groups by; don't skip it.
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": _version(),
            # Distinguishes a laptop run from a cron run once tier 3 lands.
            "deployment.environment.name": os.environ.get("JOBTRACKER_ENV", "local"),
            # MUST be stable across runs. The SDK defaults this to a fresh random UUID
            # per process, which is right for a long-lived server and wrong for us: a
            # daily job would mint a brand-new Prometheus time series every night and
            # nothing would ever graph as a continuous line. Pin it to the host instead.
            "service.instance.id": os.environ.get(
                "JOBTRACKER_INSTANCE_ID", os.uname().nodename
            ),
        }
    )
    provider = TracerProvider(resource=resource)

    if mode == "console":
        # SimpleSpanProcessor exports each span the instant it ends, on the calling
        # thread. Terrible for production throughput, ideal for learning: the output
        # appears in the order spans *finish*, interleaved with your log lines.
        #
        # out=sys.stderr is NOT cosmetic. ConsoleSpanExporter defaults to stdout, which
        # here is where the report goes — `check > out.md` would interleave span JSON
        # into the markdown. Diagnostics never share a stream with output.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(out=sys.stderr),
            export_interval_millis=15_000,
        )
    else:
        # BatchSpanProcessor queues spans and flushes them from a background thread.
        # This is what you want in production: exporting stops blocking your request path,
        # and a slow or dead collector can no longer slow down the job itself.
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(preferred_temporality=_delta_temporality()),
            # The default 60s tick would never fire in a ~30s run; shutdown() forces a
            # final collect either way, but a short interval means a long run still
            # reports progress instead of going dark until it exits.
            export_interval_millis=15_000,
        )

    # 2. Make it global. Every `trace.get_tracer(...)` from now on — including ones that
    #    already ran at import time in fetch.py — resolves through this provider.
    trace.set_tracer_provider(provider)
    _provider = provider

    #    Metrics are the same three-part assembly with one asymmetry: readers are passed
    #    to the constructor, since a MeterProvider has no add_* method after the fact.
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    # 3. Free spans for every outbound HTTP call, without touching requests-using code.
    #    This is the "auto-instrumentation" people mean; there are equivalents for most
    #    popular libraries. Wrapped in try/except so the package stays optional.
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
    except ImportError:
        log.debug("opentelemetry-instrumentation-requests not installed; skipping")

    # 4. A batching processor holds spans in memory. Without an explicit flush, whatever
    #    is still queued when the process exits is silently lost — a classic first-day
    #    "why are my spans missing?".
    atexit.register(shutdown)

    log.info("telemetry enabled (mode=%s, service.name=%s)", mode, service_name)
    return True


def shutdown() -> None:
    """Flush pending telemetry and stop the exporters. Safe to call more than once."""
    global _provider, _meter_provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
    # Metrics are collected on a timer, so the window between the last periodic export
    # and process exit is lost unless something forces a final collect. This is it.
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None


def _version() -> str:
    from . import __version__

    return __version__
