package browser

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
	"unsafe"
)

func TestCompactIndexAndLateParents(t *testing.T) {
	if unsafe.Sizeof(node{}) != 32 {
		t.Fatal("node is not compact")
	}
	x := newIndex(5, "root", 1<<30)
	for _, r := range []record{{ID: 201, Parent: 100, Name: "file.txt"}, {ID: 100, Parent: 5, Name: "folder", Attributes: 0x10}, {ID: 90, Parent: 91, Name: "outside", Attributes: 0x10}} {
		if e := x.add(r); e != nil {
			t.Fatal(e)
		}
	}
	p, e := x.page(1, 0, 100, false)
	if e != nil || len(p.Items) != 1 || p.Items[0].Name != "folder" {
		t.Fatal(p, e)
	}
	child, e := x.page(p.Items[0].ID, 0, 100, false)
	if e != nil || len(child.Items) != 1 || child.Items[0].Name != "file.txt" {
		t.Fatal(child, e)
	}
	if _, e = x.page(x.dirs[90].Index, 0, 100, false); e == nil {
		t.Fatal("scope escape")
	}
	if _, e = x.page(1, child.Items[0].ID, 100, false); e == nil {
		t.Fatal("cursor scope escape")
	}
}
func TestFolderQueryDoesNotWalkFiles(t *testing.T) {
	x := newIndex(5, "root", 1<<30)
	_ = x.add(record{ID: 100, Parent: 5, Name: "folder", Attributes: 0x10})
	for i := 0; i < 100000; i++ {
		if e := x.add(record{ID: uint64(1000 + i), Parent: 5, Name: "file"}); e != nil {
			t.Fatal(e)
		}
	}
	p, e := x.page(1, 0, 100, true)
	if e != nil || len(p.Items) != 1 || p.Examined != 1 {
		t.Fatal(p, e)
	}
	count, cursor := 0, uint32(0)
	for {
		p, e = x.page(1, cursor, 200, false)
		if e != nil {
			t.Fatal(e)
		}
		count += len(p.Items)
		if !p.More {
			break
		}
		cursor = p.Next
	}
	if count != 100001 {
		t.Fatal(count)
	}
}
func TestPrivateAndLinksNotTraversed(t *testing.T) {
	x := newIndex(5, "root", 1<<20)
	for _, r := range []record{{ID: 100, Parent: 5, Name: "private", Attributes: 0x10}, {ID: 200, Parent: 100, Name: marker}, {ID: 101, Parent: 5, Name: "link", Attributes: 0x410}, {ID: 201, Parent: 101, Name: "outside"}} {
		_ = x.add(r)
	}
	p, e := x.page(1, 0, 100, false)
	if e != nil || len(p.Items) != 1 || !p.Items[0].Link {
		t.Fatal(p, e)
	}
	for _, id := range []uint32{x.dirs[100].Index, x.dirs[101].Index} {
		if _, e = x.page(id, 0, 100, false); e == nil {
			t.Fatal("expanded protected node")
		}
	}
}
func TestMemoryBudgetIsFailureNotSuccessfulTruncation(t *testing.T) {
	x := newIndex(5, "root", 4096)
	var err error
	for i := 0; i < 10000; i++ {
		err = x.add(record{ID: uint64(100 + i), Parent: 5, Name: "name"})
		if err != nil {
			break
		}
	}
	if !errors.Is(err, ErrMemory) {
		t.Fatal(err)
	}
	if len(x.nodes) <= 2 {
		t.Fatal("expected retained partial index")
	}
}
func finish(t *testing.T, s *Scanner) Status {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		st := s.Snapshot()
		if !st.Running {
			return st
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("scan did not stop")
	return Status{}
}
func TestWalkAndRescanSessions(t *testing.T) {
	root := t.TempDir()
	_ = os.Mkdir(filepath.Join(root, "folder"), 0700)
	p := filepath.Join(root, "folder", "keep.txt")
	_ = os.WriteFile(p, []byte("original contents"), 0600)
	_ = os.Mkdir(filepath.Join(root, "private"), 0700)
	_ = os.WriteFile(filepath.Join(root, "private", marker), []byte("private"), 0600)
	s := NewScanner()
	id, e := s.Start(root, "walk")
	if e != nil {
		t.Fatal(e)
	}
	st := finish(t, s)
	if st.State != "done" || st.Files != 1 || st.Directories != 1 || st.Skipped != 1 {
		t.Fatal(st)
	}
	page, e := s.Children(id, 1, 0, 100, false)
	if e != nil || len(page.Items) != 1 {
		t.Fatal(page, e)
	}
	before, _ := os.ReadFile(p)
	if string(before) != "original contents" {
		t.Fatal("source modified")
	}
	second, e := s.Start(root, "walk")
	if e != nil || second == id {
		t.Fatal(e)
	}
	finish(t, s)
	if _, e = s.Children(id, 1, 0, 100, false); e == nil {
		t.Fatal("stale session accepted")
	}
}
func TestCancelWhileRunning(t *testing.T) {
	started := make(chan struct{})
	s := NewScanner()
	s.native = func(ctx context.Context, _ string, init func(uint64, string), add func([]record) error) error {
		init(5, "ntfs")
		close(started)
		<-ctx.Done()
		return ctx.Err()
	}
	if _, e := s.Start(t.TempDir(), "ntfs"); e != nil {
		t.Fatal(e)
	}
	<-started
	s.Cancel()
	st := finish(t, s)
	if st.State != "cancelled" || !st.CanBrowse {
		t.Fatal(st)
	}
}
func TestAutoFallbackOnlyBeforeAnyIndexIsRead(t *testing.T) {
	for _, partial := range []bool{false, true} {
		s := NewScanner()
		s.native = func(_ context.Context, _ string, init func(uint64, string), add func([]record) error) error {
			if partial {
				init(5, "ntfs")
				_ = add([]record{{ID: 100, Parent: 5, Name: "partial"}})
			}
			return ErrUnavailable
		}
		if _, e := s.Start(t.TempDir(), "auto"); e != nil {
			t.Fatal(e)
		}
		st := finish(t, s)
		if partial && st.State != "failed" {
			t.Fatal("mixed partial native data with walk", st)
		}
		if !partial && (st.Backend != "walk" || st.Notice == "") {
			t.Fatal(st)
		}
	}
}
func BenchmarkIndexMillion(b *testing.B) {
	for range b.N {
		x := newIndex(5, "root", 4<<30)
		for i := 0; i < 1000; i++ {
			_ = x.add(record{ID: uint64(100 + i), Parent: 5, Name: "folder-" + strconv.Itoa(i), Attributes: 0x10})
		}
		for i := 0; i < 1000000; i++ {
			if e := x.add(record{ID: uint64(10000 + i), Parent: uint64(100 + i%1000), Name: "file-" + strconv.Itoa(i) + ".txt"}); e != nil {
				b.Fatal(e)
			}
		}
		p, e := x.page(1, 0, 100, true)
		if e != nil || len(p.Items) != 100 {
			b.Fatal(e)
		}
		b.ReportMetric(float64(x.memory())/1024/1024, "index-MiB")
	}
}
