package main

import (
	"encoding/json"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestEmbeddedAssetsAndBindingName(t *testing.T) {
	svc := &BrowserService{}
	h := assetHandler(svc)
	for _, path := range []string{"/", "/main.js", "/virtual.js", "/style.css", "/wails/runtime.js"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 || w.Body.Len() == 0 {
			t.Fatalf("missing asset %s: %d", path, w.Code)
		}
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/app-config.json", nil))
	var config map[string]string
	if json.Unmarshal(w.Body.Bytes(), &config) != nil || config["service"] != reflect.TypeOf(*svc).PkgPath()+".BrowserService" {
		t.Fatal(w.Body.String())
	}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/main.js", nil))
	if !strings.Contains(w.Body.String(), "Call.ByName") {
		t.Fatal("missing runtime bridge")
	}
}
