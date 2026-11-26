"""Explicit span ownership and policy-controlled model I/O capture."""

import json
import os
from datetime import UTC, datetime
from threading import Lock
from time import monotonic_ns
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from compass_agent.models.errors import MESSAGES
from compass_agent.observability.content import ContentPolicy, input_snapshot, output_snapshot
from compass_agent.observability.privacy import fields, install_dependency_filters

PROPAGATOR = TraceContextTextMapPropagator()


def error_code(value):
    return value if value in MESSAGES else "internal_error" if value else None


def remote_context(carrier):
    """Call only after service authentication; ignore baggage and malformed carriers."""
    safe = {}
    for name in ("traceparent", "tracestate"):
        value = carrier.get(name)
        if isinstance(value, str) and len(value) <= 512:
            safe[name] = value
    return PROPAGATOR.extract(safe, context=Context())


class Observation:
    def __init__(
        self,
        owner,
        name,
        attributes=None,
        parent=None,
        context=None,
        *,
        capture_content=None,
    ):
        self.owner, self.name, self.parent = owner, name, parent
        self.metadata = {**(parent.metadata if parent else {}), **(attributes or {})}
        self.root = parent.root if parent else self
        self.content_enabled = (
            parent.content_enabled
            if parent
            else True
            if capture_content is None
            else capture_content
        )
        self.started_ns = monotonic_ns()
        self.ended = self.authoritative = False
        self.first_ns = None
        self.status = None
        self.lock = Lock()
        self.cost = None
        self._content_secrets = ()
        self.span = None
        if owner.tracer:
            try:
                self.span = owner.tracer.start_span(
                    name,
                    context=trace.set_span_in_context(parent.span)
                    if parent and parent.span
                    else context or Context(),
                    attributes={
                        "langfuse.observation.type": "span",
                        "langfuse.environment": owner.environment,
                        "langfuse.trace.name": "compass.request",
                        **fields({"instance_id": owner.instance_id, **self.metadata}),
                    },
                )
            except Exception:
                owner.instrumentation_errors += 1

    def child(self, name, **attributes):
        return Observation(self.owner, name, attributes, parent=self)

    def set(self, **attributes):
        self.metadata.update(attributes)
        if self.span and not self.ended:
            try:
                self.span.set_attributes(fields(attributes))
            except Exception:
                self.owner.instrumentation_errors += 1

    def event(self, name, **attributes):
        if self.span and not self.ended:
            try:
                self.span.add_event(name, fields(attributes))
            except Exception:
                self.owner.instrumentation_errors += 1

    def native(self, key, value):
        if self.span and not self.ended:
            try:
                self.span.set_attribute("langfuse.observation." + key, value)
            except Exception:
                self.owner.instrumentation_errors += 1

    def headers(self):
        if not self.span:
            return {}
        ctx = self.span.get_span_context()
        return {
            "X-Compass-Trace-Id": f"{ctx.trace_id:032x}",
            "X-Compass-Trace-Sampled": "1" if ctx.trace_flags.sampled else "0",
        }

    def carrier(self):
        result = {}
        if self.span:
            PROPAGATOR.inject(result, context=trace.set_span_in_context(self.span))
        return result

    def complete(self, status, code=None, *, authoritative=False):
        with self.lock:
            if self.ended or self.authoritative:
                return
            self.status = (status, error_code(code))
            self.authoritative = authoritative
        if code == "record_write_failed" and self.root is not self:
            self.root.complete("failed", code, authoritative=True)

    def first_visible(self):
        if self.first_ns is None:
            self.first_ns = monotonic_ns()
            self.set(first_visible_ms=(self.first_ns - self.started_ns) / 1e6)
            self.native("completion_start_time", datetime.now(UTC).isoformat())
            if self.root.first_ns is None:
                self.root.first_ns = self.first_ns
                self.root.set(first_visible_ms=(self.first_ns - self.root.started_ns) / 1e6)

    def accept_cost(self, value):
        from compass_agent.observability.privacy import scalar

        if type(value) not in {int, float} or scalar(value) is None:
            return
        with self.lock:
            if not self.ended:
                self.cost = float(value)

    def capture_input(self, request):
        if (
            not self.content_enabled
            or not self.owner.content.enabled
            or not self.span
            or not self.span.is_recording()
        ):
            return
        try:
            self._content_secrets = (request.access.api_key.get_secret_value(),)
            encoded, truncated = self.owner.content.encode(
                input_snapshot(request.input), self._content_secrets
            )
            self.native("input", encoded)
            self.set(content_input_truncated=truncated)
        except Exception:
            self.owner.instrumentation_errors += 1

    def capture_output(self, result):
        if (
            not self.content_enabled
            or not self.owner.content.enabled
            or not self.span
            or not self.span.is_recording()
        ):
            return
        try:
            value = output_snapshot(getattr(result, "output", None))
            if value is not None:
                encoded, truncated = self.owner.content.encode(value, self._content_secrets)
                self.native("output", encoded)
                self.set(
                    content_output_truncated=truncated,
                    content_output_partial=result.status != "completed",
                )
        except Exception:
            self.owner.instrumentation_errors += 1

    def finish_attempt(self, result):
        if not result.adapter_started:
            # A pre-adapter validation is an execution phase, with no generation or charge.
            if self.span:
                self.span.update_name("compass.validation")
            self.complete(result.status, result.error.code if result.error else None)
            self.end()
            return
        self.native("type", "generation")
        self.capture_output(result)
        self.set(
            sent=result.upstream_sent,
            outcome_unknown=result.upstream_outcome_unknown,
            cleanup_confirmed=result.cleanup_confirmed,
            reasoning_tokens=result.usage.reasoning_tokens,
        )
        usage = result.usage
        known = usage.input_tokens is not None and usage.output_tokens is not None
        self.set(usage_complete=known, cost_state="unknown")
        detail = {
            k: v
            for k, v in {"input": usage.input_tokens, "output": usage.output_tokens}.items()
            if v is not None
        }
        if detail:
            self.native("usage_details", json.dumps(detail))
        if known and not result.upstream_outcome_unknown and self.cost is not None:
            self.native("model.name", result.requested_model[:256])
            self.native("cost_details", json.dumps({"total": self.cost}))
            self.set(cost_state="complete_estimate", cost_source="litellm-1.106.6")
        self.complete(result.status, result.error.code if result.error else None)
        self.end()

    def end(self):
        with self.lock:
            if self.ended:
                return
            self._content_secrets = ()
            status, code = self.status or ("completed", None)
            self.set(status=status, error_code=code)
            if self.span:
                try:
                    level = (
                        "ERROR"
                        if status in {"failed", "incomplete"}
                        else "WARNING"
                        if status == "canceled"
                        else "DEFAULT"
                    )
                    self.native("level", level)
                    if code:
                        self.native("status_message", code)
                    if status in {"failed", "incomplete"}:
                        self.span.set_status(Status(StatusCode.ERROR))
                    self.span.end()
                except Exception:
                    self.owner.instrumentation_errors += 1
            self.ended = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind and self.status is None:
            import asyncio

            self.complete(
                "canceled" if issubclass(kind, asyncio.CancelledError) else "failed",
                getattr(value, "code", None),
            )
        self.end()


class Observer:
    def __init__(self, settings, *, exporter=None):
        self.instance_id = settings.compass_observability_instance_id or str(uuid4())
        self.environment = settings.compass_observability_environment
        self.ratio = settings.compass_observability_sample_ratio
        self.enabled = settings.compass_observability_enabled
        self.content = ContentPolicy(
            settings.capture_observability_content,
            settings.compass_observability_content_max_bytes,
        )
        self.configured = bool(
            settings.compass_langfuse_base_url
            and settings.compass_langfuse_public_key
            and settings.compass_langfuse_secret_key
        )
        self.provider = self.tracer = self.pipeline = None
        self.closed = False
        self.instrumentation_errors = 0
        install_dependency_filters()
        if self.enabled and (self.configured or exporter is not None):
            from compass_agent.observability.export import Pipeline

            os.environ["OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"] = "true"
            self.provider = TracerProvider(
                resource=Resource(
                    {
                        "service.name": "compass-agent",
                        "service.namespace": "compass",
                        "service.instance.id": self.instance_id,
                        "service.version": "0.1.0",
                        "deployment.environment.name": self.environment,
                    }
                ),
                sampler=ParentBased(TraceIdRatioBased(self.ratio)),
                span_limits=SpanLimits(
                    max_attributes=64,
                    max_events=16,
                    max_links=0,
                    max_attribute_length=self.content.max_bytes if self.content.enabled else 256,
                ),
                shutdown_on_exit=False,
            )
            self.pipeline = Pipeline(
                settings, self.provider, exporter=exporter, instance_id=self.instance_id
            )
            self.tracer = self.provider.get_tracer("compass.observability")

    def root(self, call_id, context, carrier=None, *, capture_content=True):
        attrs = {
            "call_id": str(call_id),
            "purpose": context.purpose,
            "actor_id": str(context.actor_user_id),
        }
        for key in ("tenant_id", "workspace_id"):
            if value := getattr(context.scope, key, None):
                attrs[key] = str(value)
        scope = Observation(
            self,
            "compass.request",
            attrs,
            context=remote_context(carrier or {}),
            capture_content=capture_content,
        )
        if scope.span:
            scope.span.set_attribute("langfuse.trace.metadata.call_id", str(call_id))
        return scope

    def status(self):
        data = (
            self.pipeline.status()
            if self.pipeline
            else {
                "queue_size": 0,
                "queue_capacity": 512,
                "dropped": 0,
                "export_failures": 0,
                "last_export": "never",
            }
        )
        return {
            "instance_id": self.instance_id,
            "environment": self.environment,
            "enabled": self.enabled,
            "configured": self.configured,
            "sample_ratio": self.ratio,
            "instrumentation_errors": self.instrumentation_errors,
            **data,
        }

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        if self.provider:
            self.provider.shutdown()
        if self.pipeline:
            from compass_agent.observability.privacy import diagnostic

            diagnostic(self.instance_id, "shutdown", self.status())
            self.pipeline.meters.shutdown()
