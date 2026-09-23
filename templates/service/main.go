package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

type application struct {
	requests atomic.Uint64
	errors   atomic.Uint64
}

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
func (a *application) handler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		w.WriteHeader(http.StatusOK)
		return
	}
	if r.URL.Path == "/metrics" {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		labels := fmt.Sprintf("service_name=%q,service_namespace=%q,instance=%q", env("OTEL_SERVICE_NAME", "__SERVICE_NAME__"), env("POD_NAMESPACE", "__NAMESPACE__"), env("POD_UID", "local"))
		fmt.Fprintf(w, "# TYPE service_requests_total counter\nservice_requests_total{%s,status_class=\"2xx\"} %d\nservice_requests_total{%s,status_class=\"5xx\"} %d\n", labels, a.requests.Load(), labels, a.errors.Load())
		return
	}
	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	_, span := otel.Tracer("http-service").Start(ctx, "HTTP request", trace.WithSpanKind(trace.SpanKindServer), trace.WithAttributes(attribute.String("http.request.method", r.Method)))
	defer span.End()
	status := http.StatusOK
	// Unknown routes have one bounded route label; never record raw URLs or headers.
	route := "/"
	if r.URL.Path != "/" {
		status = http.StatusNotFound
		route = "unmatched"
	}
	span.SetAttributes(attribute.String("http.route", route), attribute.Int("http.response.status_code", status))
	if status >= 500 {
		a.errors.Add(1)
		span.SetStatus(codes.Error, "server error")
	} else if status < 400 {
		a.requests.Add(1)
	}
	slog.Info("request complete", "route", route, "status", status, "trace_id", span.SpanContext().TraceID().String())
	w.WriteHeader(status)
	fmt.Fprintln(w, "__SERVICE_NAME__")
}
func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	exp, err := otlptracehttp.New(ctx)
	if err != nil {
		slog.Error("trace exporter initialization failed")
		os.Exit(1)
	}
	res, err := resource.New(ctx, resource.WithFromEnv(), resource.WithAttributes(attribute.String("service.name", env("OTEL_SERVICE_NAME", "__SERVICE_NAME__"))))
	if err != nil {
		slog.Error("resource initialization failed")
		os.Exit(1)
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp, sdktrace.WithMaxQueueSize(2048)), sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.AlwaysSample()))
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	app := &application{}
	server := &http.Server{Addr: ":8080", Handler: http.HandlerFunc(app.handler), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("HTTP server failed")
			stop()
		}
	}()
	<-ctx.Done()
	drain, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_ = server.Shutdown(drain)
	_ = provider.Shutdown(drain)
}
