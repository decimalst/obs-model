package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

type fakeAdmission struct {
	allowed []bool
	err     error
}

func (f fakeAdmission) Allow(context.Context, []Record) ([]bool, error) { return f.allowed, f.err }
func TestHTTPAdmission(t *testing.T) {
	for _, tt := range []struct {
		name, body string
		admission  fakeAdmission
		status     int
		forwarded  string
	}{
		{"keep-error-and-drop-debug", `{"service_key":"apps/a","level":"ERROR"}` + "\n" + `{"service_key":"apps/a","level":"DEBUG"}`, fakeAdmission{[]bool{true, false}, nil}, 200, `"ERROR"`},
		{"all-dropped-is-acknowledged", `{"service_key":"apps/a","level":"ERROR"}`, fakeAdmission{[]bool{false}, nil}, 200, ""},
		{"redis-outage-fails-closed", `{"service_key":"apps/a","level":"ERROR"}`, fakeAdmission{nil, errors.New("down")}, 503, ""},
		{"invalid-identity", `{"service_key":"a"}`, fakeAdmission{}, 400, ""},
		{"malformed", `not json`, fakeAdmission{}, 400, ""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			var received string
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { b, _ := io.ReadAll(r.Body); received = string(b) }))
			defer upstream.Close()
			s := Server{Admission: tt.admission, Upstream: upstream.URL, Client: upstream.Client(), Inflight: make(chan struct{}, 2)}
			w := httptest.NewRecorder()
			s.ingest(w, httptest.NewRequest("POST", "/ingest", strings.NewReader(tt.body)))
			if w.Code != tt.status {
				t.Fatalf("status %d; want %d", w.Code, tt.status)
			}
			if tt.forwarded == "" && received != "" {
				t.Fatal("rejected data was forwarded")
			}
			if tt.forwarded != "" && (!strings.Contains(received, tt.forwarded) || strings.Contains(received, "DEBUG")) {
				t.Fatalf("unexpected forwarded data %s", received)
			}
		})
	}
}
func TestHTTPResourceBounds(t *testing.T) {
	s := Server{Inflight: make(chan struct{}, 1)}
	for _, tt := range []struct {
		method, body string
		status       int
	}{
		{"GET", "", 405}, {"POST", strings.Repeat("x", 300000), 413},
		{"POST", strings.Repeat("{\"service_key\":\"a/b\"}\n", 513), 413},
	} {
		w := httptest.NewRecorder()
		s.ingest(w, httptest.NewRequest(tt.method, "/ingest", strings.NewReader(tt.body)))
		if w.Code != tt.status {
			t.Fatalf("got %d want %d", w.Code, tt.status)
		}
	}
	s.Inflight <- struct{}{}
	w := httptest.NewRecorder()
	s.ingest(w, httptest.NewRequest("POST", "/ingest", strings.NewReader("")))
	if w.Code != 503 {
		t.Fatal("concurrency bound not enforced")
	}
}

func TestShutdownDrainsInflightRequest(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	entered, release := make(chan struct{}), make(chan struct{})
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-release
		w.Write([]byte("completed"))
	})}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- serveUntilShutdown(ctx, server, listener) }()
	response := make(chan string, 1)
	go func() {
		client := &http.Client{Timeout: 3 * time.Second}
		resp, err := client.Get("http://" + listener.Addr().String())
		if err != nil {
			response <- err.Error()
			return
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		response <- string(body)
	}()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("request did not reach handler")
	}
	cancel()
	select {
	case err := <-done:
		close(release)
		t.Fatalf("server returned before in-flight request drained: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	close(release)
	if body := <-response; body != "completed" {
		t.Fatalf("in-flight request was lost: %s", body)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("shutdown did not finish after request completed")
	}
}
func redisClient(t *testing.T) *redis.Client {
	t.Helper()
	addr := os.Getenv("TEST_REDIS_ADDR")
	if addr == "" {
		t.Skip("set TEST_REDIS_ADDR for actual shared-store tests")
	}
	r := redis.NewClient(&redis.Options{Addr: addr})
	t.Cleanup(func() { r.Close() })
	if err := r.Ping(context.Background()).Err(); err != nil {
		t.Fatal(err)
	}
	return r
}
func TestRedisSharedQuotaAndNoisyNeighbor(t *testing.T) {
	r := redisClient(t)
	l := Limits{1, 100, 1000000, 1000000, 1, 10, 100000, 100000}
	a, b := RedisAdmission{r, l}, RedisAdmission{r, l}
	service := fmt.Sprintf("test/flood-%d", time.Now().UnixNano())
	var accepted atomic.Int64
	var wg sync.WaitGroup
	start := time.Now()
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			gate := a
			if i%2 == 1 {
				gate = b
			}
			records := make([]Record, 10)
			for j := range records {
				records[j] = Record{Event{service, "ERROR"}, []byte("log")}
			}
			ok, err := gate.Allow(context.Background(), records)
			if err != nil {
				t.Error(err)
				return
			}
			for _, pass := range ok {
				if pass {
					accepted.Add(1)
				}
			}
		}(i)
	}
	wg.Wait()
	if start.Add(time.Second).Before(time.Now()) {
		t.Fatal("test exceeded one refill interval")
	}
	if accepted.Load() != 100 {
		t.Fatalf("replicas multiplied error budget: accepted %d instead of 100", accepted.Load())
	}
	ok, err := a.Allow(context.Background(), []Record{{Event{service + "-quiet", "INFO"}, []byte("quiet")}})
	if err != nil || !ok[0] {
		t.Fatal("noisy service starved an independent service")
	}
}
func TestRedisDebugSubBudgetAndBytes(t *testing.T) {
	r := redisClient(t)
	service := fmt.Sprintf("test/debug-%d", time.Now().UnixNano())
	a := RedisAdmission{r, Limits{1, 100, 1, 1000, 1, 2, 1, 10}}
	records := []Record{{Event{service, "DEBUG"}, []byte("123456")}, {Event{service, "DEBUG"}, []byte("123456")}, {Event{service, "ERROR"}, []byte("123456")}}
	ok, err := a.Allow(context.Background(), records)
	if err != nil || !ok[0] || ok[1] || !ok[2] {
		t.Fatalf("debug budget escaped or blocked errors: %v %v", ok, err)
	}
	a = RedisAdmission{r, Limits{1, 100, 1, 5, 1, 10, 1, 5}}
	ok, err = a.Allow(context.Background(), []Record{{Event{service + "-bytes", "ERROR"}, []byte("123456")}})
	if err != nil || ok[0] {
		t.Fatal("large error log bypassed byte quota")
	}
}

func TestRedisPolicyTransitionFailsClosedAndExpires(t *testing.T) {
	r := redisClient(t)
	ctx := context.Background()
	service := fmt.Sprintf("test/policy-%d", time.Now().UnixNano())
	records := []Record{{Event{service, "ERROR"}, []byte("x")}}
	a := RedisAdmission{r, Limits{1, 1, 1, 1, 1, 1, 1, 1}}
	if ok, err := a.Allow(ctx, records); err != nil || !ok[0] {
		t.Fatalf("initial admission: %v %v", ok, err)
	}
	b := RedisAdmission{r, Limits{10, 10, 10, 10, 1, 1, 1, 1}}
	if _, err := b.Allow(ctx, records); err == nil {
		t.Fatal("mixed quota policies must fail closed")
	}
	// Keep trying the new policy beyond the original 2s TTL. Rejections must
	// never keep the previous policy alive after its last active writer drains.
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		if ok, err := b.Allow(ctx, records); err == nil && ok[0] {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("new policy never became active after the old bucket expired")
}

func TestRedisClockRollbackDoesNotMintTokens(t *testing.T) {
	r := redisClient(t)
	ctx := context.Background()
	service := fmt.Sprintf("test/clock-%d", time.Now().UnixNano())
	hash := sha256.Sum256([]byte(service))
	key := "olly:quota:v1:" + hex.EncodeToString(hash[:])
	now, err := r.Time(ctx).Result()
	if err != nil {
		t.Fatal(err)
	}
	future := float64(now.Unix()) + 30
	if err := r.HSet(ctx, key, "at", future, "events", 0, "bytes", 0, "debug_events", 0, "debug_bytes", 0).Err(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Del(ctx, key) })
	a := RedisAdmission{r, Limits{100, 100, 100, 100, 10, 10, 10, 10}}
	for i := 0; i < 2; i++ {
		ok, err := a.Allow(ctx, []Record{{Event{service, "ERROR"}, []byte("x")}})
		if err != nil || ok[0] {
			t.Fatalf("rollback minted tokens: %v %v", ok, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if at, err := r.HGet(ctx, key, "at").Float64(); err != nil || at != future {
		t.Fatalf("bucket clock moved backwards: %v %v", at, err)
	}
	if ttl, err := r.TTL(ctx, key).Result(); err != nil || ttl < 29*time.Second {
		t.Fatalf("rollback shortened safe expiry: %v %v", ttl, err)
	}
}
