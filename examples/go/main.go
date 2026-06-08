// Go integration — stdlib net/http/pprof → OTel Collector → Dynatrace
//
// Zero dependencies beyond the Go standard library.
// The profiler push loop runs in a background goroutine.
//
// Run:
//   DT_COLLECTOR_URL=http://localhost:4040 \
//   OTEL_SERVICE_NAME=my-go-service \
//   go run main.go

package main

import (
	"bytes"
	"fmt"
	"io"
	"log"
	"net/http"
	_ "net/http/pprof" // registers /debug/pprof/* handlers
	"os"
	"time"
)

func main() {
	// Start the pprof debug server (separate port — not exposed publicly)
	go func() {
		log.Fatal(http.ListenAndServe("localhost:6060", nil))
	}()

	// Start the profile push loop
	go pushProfiles()

	// Your application server
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintln(w, "hello")
	})
	log.Fatal(http.ListenAndServe(":8080", nil))
}

// pushProfiles captures a 30-second CPU profile every 60 seconds and
// POSTs it to the OTel Collector's pprof receiver.
func pushProfiles() {
	collectorURL := os.Getenv("DT_COLLECTOR_URL")
	if collectorURL == "" {
		collectorURL = "http://localhost:4040"
	}
	serviceName := os.Getenv("OTEL_SERVICE_NAME")
	if serviceName == "" {
		serviceName = "unknown-go-service"
	}

	for {
		time.Sleep(60 * time.Second)

		log.Println("capturing 30s CPU profile...")
		profileURL := "http://localhost:6060/debug/pprof/profile?seconds=30"
		resp, err := http.Get(profileURL)
		if err != nil {
			log.Printf("pprof capture failed: %v", err)
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		// Push to the OTel Collector pyroscope receiver
		ingestURL := fmt.Sprintf("%s/ingest?name=%s&from=%d&until=%d",
			collectorURL,
			serviceName,
			time.Now().Add(-30*time.Second).Unix(),
			time.Now().Unix(),
		)
		pushResp, err := http.Post(ingestURL, "application/octet-stream", bytes.NewReader(body))
		if err != nil {
			log.Printf("profile push failed: %v", err)
			continue
		}
		pushResp.Body.Close()
		log.Printf("profile pushed → HTTP %d (%d bytes)", pushResp.StatusCode, len(body))
	}
}
