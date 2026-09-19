package main

import (
	"embed"
	"encoding/json"
	"github.com/wailsapp/wails/v3/pkg/application"
	"io/fs"
	"net/http"
	"privacyfs/browser/internal/browser"
	"reflect"
)

//go:embed web/index.html web/style.css web/main.js web/virtual.js
var assets embed.FS

type BrowserService struct {
	scanner *browser.Scanner
	app     *application.App
}

func (s *BrowserService) SelectDirectory() (string, error) {
	return s.app.Dialog.OpenFile().SetTitle("选择要浏览的目录").CanChooseDirectories(true).CanChooseFiles(false).PromptForSingleSelection()
}
func (s *BrowserService) Scan(root, mode string) (string, error) { return s.scanner.Start(root, mode) }
func (s *BrowserService) Status() browser.Status                 { return s.scanner.Snapshot() }
func (s *BrowserService) Cancel()                                { s.scanner.Cancel() }
func (s *BrowserService) Children(session string, dir, cursor uint32, limit int, foldersOnly bool) (browser.Page, error) {
	return s.scanner.Children(session, dir, cursor, limit, foldersOnly)
}
func (s *BrowserService) DirectoryView(session string, dir uint32, offset, count int) (browser.RangeView, error) {
	return s.scanner.DirectoryView(session, dir, offset, count)
}
func (s *BrowserService) TreeView(session string, expanded, collapsed []uint32, all bool, offset, count int) (browser.TreeView, error) {
	return s.scanner.TreeView(session, expanded, collapsed, all, offset, count)
}

func assetHandler(service *BrowserService) http.Handler {
	web, _ := fs.Sub(assets, "web")
	static := application.BundledAssetFileServer(web)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/app-config.json" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]string{"service": reflect.TypeOf(*service).PkgPath() + ".BrowserService"})
			return
		}
		static.ServeHTTP(w, r)
	})
}
func main() {
	service := &BrowserService{scanner: browser.NewScanner()}
	app := application.New(application.Options{Name: "PrivacyFS Browser", Description: "本地目录结构浏览器", Services: []application.Service{application.NewService(service)}, Assets: application.AssetOptions{Handler: assetHandler(service)}, OnShutdown: service.scanner.Cancel})
	service.app = app
	app.Window.NewWithOptions(application.WebviewWindowOptions{Title: "PrivacyFS · 目录浏览器", Width: 1240, Height: 820, MinWidth: 900, MinHeight: 640, URL: "/", BackgroundColour: application.NewRGB(246, 248, 247)})
	_ = app.Run()
}
