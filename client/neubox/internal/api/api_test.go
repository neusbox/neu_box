package api

import (
	"net/http"
	"testing"
)

func TestHTTPClientDoesNotUseProxyEnvironment(t *testing.T) {
	client := defaultHTTPClient()
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("unexpected transport: %T", client.Transport)
	}
	if transport.Proxy != nil {
		t.Fatal("proxy callback must be nil")
	}
}
