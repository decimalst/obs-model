package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed quota.lua
var quotaScript string

// Bounds are shared across all processes using the same Redis primary and prefix.
type Limits struct{ Events, EventBurst, Bytes, ByteBurst, DebugEvents, DebugEventBurst, DebugBytes, DebugByteBurst int64 }
type Event struct {
	Service string `json:"service_key"`
	Level   string `json:"level"`
}
type Record struct {
	Event
	Raw []byte
}
type Admission interface {
	Allow(context.Context, []Record) ([]bool, error)
}
type RedisAdmission struct {
	Client *redis.Client
	Limits Limits
}

func (a RedisAdmission) Allow(ctx context.Context, records []Record) ([]bool, error) {
	p := a.Client.Pipeline()
	cmds := make([]*redis.Cmd, len(records))
	l := a.Limits
	policy := fmt.Sprintf("%d/%d/%d/%d/%d/%d/%d/%d", l.Events, l.EventBurst, l.Bytes, l.ByteBurst, l.DebugEvents, l.DebugEventBurst, l.DebugBytes, l.DebugByteBurst)
	for i, r := range records {
		hash := sha256.Sum256([]byte(r.Service))
		debug := 0
		if strings.EqualFold(r.Level, "debug") || strings.EqualFold(r.Level, "trace") {
			debug = 1
		}
		cmds[i] = p.Eval(ctx, quotaScript, []string{"olly:quota:v1:" + hex.EncodeToString(hash[:])}, l.Events, l.EventBurst, l.Bytes, l.ByteBurst, l.DebugEvents, l.DebugEventBurst, l.DebugBytes, l.DebugByteBurst, len(r.Raw), debug, policy)
	}
	if _, err := p.Exec(ctx); err != nil {
		return nil, err
	}
	allowed := make([]bool, len(records))
	for i, cmd := range cmds {
		n, err := cmd.Int()
		if err != nil {
			return nil, err
		}
		allowed[i] = n == 1
	}
	return allowed, nil
}

type Server struct {
	Admission   Admission
	Upstream    string
	Client      *http.Client
	Inflight    chan struct{}
	Accepted    atomic.Uint64
	Dropped     atomic.Uint64
	Unavailable atomic.Uint64
}

var serviceKey = regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,63}/[a-zA-Z0-9_.-]{1,63}$`)

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", 405)
		return
	}
	select {
	case s.Inflight <- struct{}{}:
		defer func() { <-s.Inflight }()
	default:
		http.Error(w, "busy", 503)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	defer r.Body.Close()
	scanner := bufio.NewScanner(r.Body)
	scanner.Buffer(make([]byte, 4096), 256<<10)
	records := []Record{}
	for scanner.Scan() {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		var e Event
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil || !serviceKey.MatchString(e.Service) {
			http.Error(w, "invalid event identity", 400)
			return
		}
		records = append(records, Record{e, bytes.Clone(scanner.Bytes())})
		if len(records) > 512 {
			http.Error(w, "too many events", 413)
			return
		}
	}
	if scanner.Err() != nil {
		http.Error(w, "invalid or oversized body", 413)
		return
	}
	if len(records) == 0 {
		w.WriteHeader(200)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	allowed, err := s.Admission.Allow(ctx, records)
	if err != nil {
		s.Unavailable.Add(1)
		http.Error(w, "quota store unavailable", 503)
		return
	}
	var body bytes.Buffer
	count := uint64(0)
	for i, record := range records {
		if allowed[i] {
			body.Write(record.Raw)
			body.WriteByte('\n')
			count++
		} else {
			s.Dropped.Add(1)
		}
	}
	if count > 0 {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.Upstream, &body)
		if err != nil {
			http.Error(w, "invalid upstream", 503)
			return
		}
		req.Header.Set("Content-Type", "application/x-ndjson")
		resp, err := s.Client.Do(req)
		if err != nil {
			s.Unavailable.Add(1)
			http.Error(w, "upstream unavailable", 503)
			return
		}
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			s.Unavailable.Add(1)
			http.Error(w, "upstream rejected batch", 503)
			return
		}
		s.Accepted.Add(count)
	}
	// Quota drops are intentional and acknowledged: never retry the flood.
	w.WriteHeader(http.StatusOK)
}
func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE log_quota_accepted_total counter\nlog_quota_accepted_total %d\n# TYPE log_quota_dropped_total counter\nlog_quota_dropped_total %d\n# TYPE log_quota_unavailable_total counter\nlog_quota_unavailable_total %d\n", s.Accepted.Load(), s.Dropped.Load(), s.Unavailable.Load())
}
func envInt(key string, def int64) int64 {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n <= 0 {
		log.Fatalf("%s must be a positive integer", key)
	}
	return n
}

// Serve must not return to main until Shutdown has drained in-flight requests.
func serveUntilShutdown(ctx context.Context, server *http.Server, listener net.Listener) error {
	drained := make(chan error, 1)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		defer cancel()
		err := server.Shutdown(shutdown)
		if err != nil {
			server.Close()
		}
		drained <- err
	}()
	err := server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return <-drained
	}
	return err
}

func main() {
	addr, upstream := os.Getenv("REDIS_ADDR"), os.Getenv("VECTOR_URL")
	if addr == "" || upstream == "" {
		log.Fatal("REDIS_ADDR and VECTOR_URL are required")
	}
	opts := &redis.Options{Addr: addr, Password: os.Getenv("REDIS_PASSWORD"), DialTimeout: 3 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, PoolSize: 32}
	if os.Getenv("REDIS_TLS") != "false" {
		opts.TLSConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	rc := redis.NewClient(opts)
	defer rc.Close()
	l := Limits{envInt("EVENTS_PER_SECOND", 1000), envInt("EVENT_BURST", 2000), envInt("BYTES_PER_SECOND", 1048576), envInt("BYTE_BURST", 2097152), envInt("DEBUG_EVENTS_PER_SECOND", 100), envInt("DEBUG_EVENT_BURST", 200), envInt("DEBUG_BYTES_PER_SECOND", 102400), envInt("DEBUG_BYTE_BURST", 204800)}
	s := &Server{Admission: RedisAdmission{rc, l}, Upstream: upstream, Client: &http.Client{Timeout: 8 * time.Second}, Inflight: make(chan struct{}, 64)}
	mux := http.NewServeMux()
	mux.HandleFunc("/ingest", s.ingest)
	mux.HandleFunc("/metrics", s.metrics)
	mux.HandleFunc("/live", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if rc.Ping(ctx).Err() != nil {
			http.Error(w, "quota store unavailable", 503)
			return
		}
		w.WriteHeader(200)
	})
	server := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	listener, err := net.Listen("tcp", server.Addr)
	if err != nil {
		log.Fatal(err)
	}
	log.Print("log admission listening on :8080")
	if err := serveUntilShutdown(ctx, server, listener); err != nil {
		log.Fatal(err)
	}
}
