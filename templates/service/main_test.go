package main

import (
	"context"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHealthAndMetrics(t *testing.T) {
	a := &application{}
	for _, path := range []string{"/healthz", "/", "/bad?token=secret"} {
		a.handler(httptest.NewRecorder(), httptest.NewRequest("GET", path, nil))
	}
	r := httptest.NewRecorder()
	a.handler(r, httptest.NewRequest("GET", "/metrics", nil))
	if !strings.Contains(r.Body.String(), "status_class=\"2xx\"} 1") || strings.Contains(r.Body.String(), "secret") {
		t.Fatal(r.Body.String())
	}
}

func TestTraceContextAndBoundedRoute(t *testing.T) {
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	original := otel.GetTracerProvider()
	propagationOriginal := otel.GetTextMapPropagator()
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		otel.SetTracerProvider(original)
		otel.SetTextMapPropagator(propagationOriginal)
		_ = provider.Shutdown(context.Background())
	}()
	a := &application{}
	request := httptest.NewRequest("GET", "/private/customer-123?token=do-not-collect", nil)
	request.Header.Set("traceparent", "00-01234567890123456789012345678901-0123456789012345-01")
	response := httptest.NewRecorder()
	a.handler(response, request)
	spans := exporter.GetSpans()
	if len(spans) != 1 || spans[0].SpanKind != trace.SpanKindServer || spans[0].SpanContext.TraceID().String() != "01234567890123456789012345678901" {
		t.Fatalf("incorrect propagated server span: %+v", spans)
	}
	for _, attr := range spans[0].Attributes {
		if strings.Contains(attr.Value.AsString(), "customer-123") || strings.Contains(attr.Value.AsString(), "token") {
			t.Fatal("raw URL escaped into span attributes")
		}
	}
	if a.requests.Load() != 0 || response.Code != 404 {
		t.Fatal("unmatched routes must not inflate the SLO denominator")
	}
}
