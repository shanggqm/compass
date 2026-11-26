"""OTLP transport, bounded SDK batching and process-local diagnostics."""

import base64
from threading import Lock
from time import monotonic

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext

from compass_agent.observability.privacy import (
    CONTEXT_LISTS,
    FIELDS,
    META,
    diagnostic,
    safe_attribute,
    safe_context_event,
)


def export_context(context):
    if context is None:
        return None
    return SpanContext(
        trace_id=context.trace_id,
        span_id=context.span_id,
        is_remote=context.is_remote,
        trace_flags=context.trace_flags,
    )


class GuardedExporter(SpanExporter):
    def __init__(self, inner, instance_id, content=None):
        self.inner = inner
        self.instance_id = instance_id
        self.content = content
        self.last_warning = -30.0
        self.lock = Lock()
        self.failures = self.rejected = 0
        self.last = "never"

    def export(self, spans):
        valid = []
        allowed = {META + key for key in FIELDS | CONTEXT_LISTS} | {
            "langfuse.observation.type",
            "langfuse.observation.level",
            "langfuse.environment",
            "langfuse.trace.name",
            "langfuse.trace.metadata.call_id",
            "langfuse.observation.status_message",
            "langfuse.observation.model.name",
            "langfuse.observation.usage_details",
            "langfuse.observation.cost_details",
            "langfuse.observation.completion_start_time",
        }
        for span in spans:
            attrs = span.attributes or {}
            body = {"langfuse.observation.input", "langfuse.observation.output"}
            content_valid = all(
                self.content is not None
                and self.content.accepts(attrs[key])
                and attrs.get("langfuse.observation.type") == "generation"
                for key in body & attrs.keys()
            )
            if (
                (
                    span.instrumentation_scope is None
                    or span.instrumentation_scope.name != "compass.observability"
                )
                or not set(attrs).issubset(allowed | body)
                or not content_valid
                or not all(
                    safe_attribute(span.name, key, value)
                    for key, value in attrs.items()
                    if key not in body
                )
                or (
                    bool(span.events)
                    and (
                        span.name
                        not in {
                            "context.assemble",
                            "context.decisions",
                            "compass.context.guardrail",
                            "compass.context.compile",
                        }
                        or not all(safe_context_event(event) for event in span.events)
                    )
                )
            ):
                with self.lock:
                    self.rejected += 1
            else:
                # Tracestate may contain vendor text. It is used for trusted internal
                # propagation, while the telemetry sink receives only IDs and flags.
                valid.append(
                    ReadableSpan(
                        name=span.name,
                        context=export_context(span.context),
                        parent=export_context(span.parent),
                        resource=span.resource,
                        attributes=span.attributes,
                        events=span.events,
                        links=span.links,
                        kind=span.kind,
                        status=span.status,
                        start_time=span.start_time,
                        end_time=span.end_time,
                        instrumentation_scope=span.instrumentation_scope,
                    )
                )
        if not valid:
            return SpanExportResult.SUCCESS
        try:
            result = self.inner.export(valid)
        except Exception:
            result = SpanExportResult.FAILURE
        with self.lock:
            self.last = "success" if result == SpanExportResult.SUCCESS else "failure"
            if result != SpanExportResult.SUCCESS:
                self.failures += 1
                if monotonic() - self.last_warning >= 30:
                    self.last_warning = monotonic()
                    diagnostic(self.instance_id, "export_failed")
        return result

    def shutdown(self):
        self.inner.shutdown()


class Pipeline:
    def __init__(self, settings, provider, *, exporter=None, instance_id=""):
        self.reader = InMemoryMetricReader()
        self.meters = MeterProvider(metric_readers=[self.reader])
        if exporter is None:
            auth = base64.b64encode(
                f"{settings.compass_langfuse_public_key}:{settings.compass_langfuse_secret_key.get_secret_value()}".encode()
            ).decode()
            exporter = OTLPSpanExporter(
                endpoint=settings.compass_langfuse_base_url.rstrip("/")
                + "/api/public/otel/v1/traces",
                headers={"Authorization": "Basic " + auth, "x-langfuse-ingestion-version": "4"},
                timeout=2,
            )
        from compass_agent.observability.content import ContentPolicy

        self.exporter = GuardedExporter(
            exporter,
            instance_id,
            ContentPolicy(
                settings.capture_observability_content,
                settings.compass_observability_content_max_bytes,
            ),
        )
        self.processor = BatchSpanProcessor(
            self.exporter,
            max_queue_size=512,
            max_export_batch_size=64,
            schedule_delay_millis=1000,
            meter_provider=self.meters,
        )
        provider.add_span_processor(self.processor)

    def status(self):
        size = dropped = 0
        metrics = self.reader.get_metrics_data()
        if metrics:
            for resource in metrics.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        if metric.name == "otel.sdk.processor.span.queue.size":
                            size += sum(
                                int(getattr(point, "value", 0)) for point in metric.data.data_points
                            )
                        if metric.name == "otel.sdk.processor.span.processed":
                            dropped += sum(
                                int(getattr(point, "value", 0))
                                for point in metric.data.data_points
                                if (point.attributes or {}).get("error.type")
                                in {"queue_full", "already_shutdown"}
                            )
        with self.exporter.lock:
            return {
                "queue_size": size,
                "queue_capacity": 512,
                "dropped": dropped + self.exporter.rejected,
                "export_failures": self.exporter.failures,
                "last_export": self.exporter.last,
            }
