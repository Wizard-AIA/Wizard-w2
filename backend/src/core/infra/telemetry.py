import contextlib
import os

from src.config import settings
from src.utils.logging import logger


@contextlib.contextmanager
def dummy_span(*args, **kwargs):
    class DummySpan:
        def set_attribute(self, key, value):
            pass

        def set_status(self, status, description=None):
            pass

        def record_exception(self, exception):
            pass

    yield DummySpan()


class DummyTracer:
    def start_as_current_span(self, name: str, **kwargs):
        return dummy_span()


_TRACER_PROVIDER = None
_IS_OTEL_AVAILABLE = False

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _IS_OTEL_AVAILABLE = True
except ImportError:
    _IS_OTEL_AVAILABLE = False


def setup_telemetry(app=None):
    global _TRACER_PROVIDER
    if not _IS_OTEL_AVAILABLE:
        logger.debug("opentelemetry_unavailable_using_local_tracing")
        return

    otlp_endpoint = getattr(settings, "OTLP_ENDPOINT", None) or os.environ.get("OTLP_ENDPOINT")

    provider = TracerProvider()
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    _TRACER_PROVIDER = provider

    if app is not None:
        FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    SQLite3Instrumentor().instrument()


def get_tracer(name: str):
    if _IS_OTEL_AVAILABLE and _TRACER_PROVIDER is not None:
        return trace.get_tracer(name)
    return DummyTracer()


def inject_trace_context(carrier: dict):
    if _IS_OTEL_AVAILABLE:
        TraceContextTextMapPropagator().inject(carrier)


def extract_trace_context(carrier: dict):
    if _IS_OTEL_AVAILABLE:
        return TraceContextTextMapPropagator().extract(carrier)
    return None
