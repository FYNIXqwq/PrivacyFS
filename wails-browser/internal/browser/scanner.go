package browser

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Status struct {
	Session     string  `json:"session"`
	Root        string  `json:"root"`
	State       string  `json:"state"`
	Backend     string  `json:"backend"`
	Message     string  `json:"message"`
	Notice      string  `json:"notice"`
	Records     uint64  `json:"records"`
	Files       uint64  `json:"files"`
	Directories uint64  `json:"directories"`
	Skipped     uint64  `json:"skipped"`
	Errors      uint64  `json:"errors"`
	MemoryMB    uint64  `json:"memoryMB"`
	BudgetMB    uint64  `json:"budgetMB"`
	Seconds     float64 `json:"seconds"`
	CanBrowse   bool    `json:"canBrowse"`
	Running     bool    `json:"running"`
}
type Scanner struct {
	mu         sync.RWMutex
	generation uint64
	status     Status
	data       *index
	cancel     context.CancelFunc
	started    time.Time
	native     func(context.Context, string, func(uint64, string), func([]record) error) error
}

func NewScanner() *Scanner {
	return &Scanner{status: Status{State: "idle", Message: "选择一个目录开始"}, native: scanNTFS}
}
func isPrivate(path string) bool { _, err := os.Lstat(filepath.Join(path, marker)); return err == nil }
func validateRoot(root string) (string, error) {
	absolute, err := filepath.Abs(root)
	if err != nil || strings.TrimSpace(root) == "" {
		return "", errors.New("请选择有效目录")
	}
	info, err := os.Lstat(absolute)
	if err != nil || !info.IsDir() {
		return "", errors.New("目录不存在或无法访问")
	}
	if isReparse(absolute, info) {
		return "", errors.New("请选择实际目录，不展开重解析点或链接")
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil || !samePath(resolved, absolute) {
		return "", errors.New("路径经过链接，请选择实际目录")
	}
	for p := absolute; ; p = filepath.Dir(p) {
		if p != absolute {
			if parentInfo, e := os.Lstat(p); e != nil || isReparse(p, parentInfo) {
				return "", errors.New("路径包含链接或无法访问，请选择实际目录")
			}
		}
		if isPrivate(p) {
			return "", errors.New("此目录属于私有状态，不扫描")
		}
		if filepath.Dir(p) == p {
			break
		}
	}
	return absolute, nil
}
func (s *Scanner) Start(root, mode string) (string, error) {
	if mode != "auto" && mode != "ntfs" && mode != "walk" {
		return "", errors.New("扫描方式无效")
	}
	root, err := validateRoot(root)
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.status.Running {
		return "", errors.New("请先停止当前扫描，等待停止后再选择目录")
	}
	s.generation++
	session := strconv.FormatUint(s.generation, 10)
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.started = time.Now()
	budget := memoryBudget()
	if budget < 32<<20 {
		cancel()
		return "", errors.New("可用内存不足，暂时无法建立目录索引")
	}
	s.data = nil
	s.status = Status{Session: session, Root: root, State: "scanning", Message: "正在准备读取目录", Running: true, BudgetMB: budget >> 20}
	go s.run(ctx, session, root, mode, budget)
	return session, nil
}
func (s *Scanner) set(fn func(*Status)) { s.mu.Lock(); fn(&s.status); s.mu.Unlock() }
func (s *Scanner) run(ctx context.Context, session, root, mode string, budget uint64) {
	var data *index
	init := func(fid uint64, backend string) {
		data = newIndex(fid, filepath.Base(root), budget)
		s.mu.Lock()
		s.data = data
		s.status.Backend = backend
		s.status.CanBrowse = backend == "walk"
		s.mu.Unlock()
	}
	add := func(records []record) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		data.mu.Lock()
		var err error
		for _, r := range records {
			if err = data.add(r); err != nil {
				break
			}
		}
		files, dirs, mem := data.files, data.folders, data.memory()
		data.mu.Unlock()
		s.set(func(st *Status) {
			st.Records += uint64(len(records))
			st.Files = files
			st.Directories = dirs
			st.MemoryMB = mem >> 20
		})
		return err
	}
	var err error
	if mode != "walk" {
		s.set(func(st *Status) { st.Message = "读取 NTFS 名称索引"; st.Backend = "ntfs" })
		err = s.native(ctx, root, init, add)
		if err != nil && mode == "auto" && errors.Is(err, ErrUnavailable) && data == nil && ctx.Err() == nil {
			reason := err.Error()
			s.set(func(st *Status) { st.Message = "NTFS 不可用，使用普通遍历"; st.Notice = reason })
			err = s.walk(ctx, root, init, add)
		}
	} else {
		err = s.walk(ctx, root, init, add)
	}
	s.set(func(st *Status) {
		st.Running = false
		st.Seconds = time.Since(s.started).Seconds()
		st.CanBrowse = data != nil
		switch {
		case ctx.Err() != nil:
			st.State = "cancelled"
			st.Message = "已停止；当前为部分结构"
		case err != nil:
			st.State = "failed"
			st.Message = err.Error()
		case st.Errors > 0:
			st.State = "partial"
			st.Message = "扫描完成，部分目录未能访问"
		default:
			st.State = "done"
			st.Message = "结构已就绪，展开目录即可浏览"
		}
	})
}
func (s *Scanner) walk(ctx context.Context, root string, init func(uint64, string), add func([]record) error) error {
	init(1, "walk")
	s.set(func(st *Status) {
		if st.Message != "NTFS 不可用，使用普通遍历" {
			st.Message = "扫描目录，可边扫描边浏览"
		}
	})
	paths := map[string]uint64{root: 1}
	next := uint64(2)
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			s.set(func(st *Status) { st.Errors++ })
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if path == root {
			if !d.IsDir() || isPrivate(path) {
				return errors.New("扫描目录已经改变或被标记为私有")
			}
			return nil
		}
		info, e := d.Info()
		if e != nil {
			s.set(func(st *Status) { st.Errors++ })
			return nil
		}
		if d.IsDir() && isPrivate(path) {
			s.set(func(st *Status) { st.Skipped++ })
			return filepath.SkipDir
		}
		attrs := uint32(0)
		if info.IsDir() {
			attrs |= 0x10
		}
		reparse := isReparse(path, info)
		if reparse {
			attrs |= 0x400
		}
		id := next
		next++
		if info.IsDir() && !reparse {
			paths[path] = id
		}
		if err := add([]record{{ID: id, Parent: paths[filepath.Dir(path)], Name: d.Name(), Attributes: attrs}}); err != nil {
			return err
		}
		if reparse && d.IsDir() {
			return filepath.SkipDir
		}
		return nil
	})
}
func (s *Scanner) Snapshot() Status {
	s.mu.RLock()
	out := s.status
	data := s.data
	if out.Running {
		out.Seconds = time.Since(s.started).Seconds()
	}
	s.mu.RUnlock()
	if data != nil {
		data.mu.RLock()
		out.MemoryMB = data.memory() >> 20
		data.mu.RUnlock()
	}
	return out
}
func (s *Scanner) Cancel() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cancel != nil && s.status.Running {
		s.cancel()
		s.status.State = "cancelling"
		s.status.Message = "正在停止扫描"
	}
}
func (s *Scanner) Children(session string, dir, cursor uint32, limit int, foldersOnly bool) (Page, error) {
	s.mu.RLock()
	data := s.data
	valid := session == s.status.Session && s.status.CanBrowse
	s.mu.RUnlock()
	if !valid || data == nil {
		return Page{}, errors.New("目录结构尚未就绪或扫描会话已改变")
	}
	p, err := data.page(dir, cursor, limit, foldersOnly)
	p.Session = session
	return p, err
}
func (s *Scanner) current(session string) (*index, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if session != s.status.Session || s.data == nil || !s.status.CanBrowse {
		return nil, errors.New("目录结构尚未就绪或扫描会话已改变")
	}
	return s.data, nil
}
func (s *Scanner) DirectoryView(session string, dir uint32, offset, count int) (RangeView, error) {
	x, e := s.current(session)
	if e != nil {
		return RangeView{}, e
	}
	out, e := x.directoryView(dir, offset, count)
	out.Session = session
	return out, e
}
func (s *Scanner) TreeView(session string, expanded, collapsed []uint32, all bool, offset, count int) (TreeView, error) {
	x, e := s.current(session)
	if e != nil {
		return TreeView{}, e
	}
	out, e := x.treeView(expanded, collapsed, all, offset, count)
	out.Session = session
	return out, e
}
