package browser

import (
	"errors"
	"math"
	"strings"
	"sync"
)

const (
	directory uint16 = 1
	link      uint16 = 2
)
const marker = ".privacyfs-state.json"

var ErrScope = errors.New("目录不在当前范围、尚未读到或不可展开")
var ErrMemory = errors.New("索引达到内存预算，扫描已停止；已读取的部分仍可浏览")

// A node occupies 32 bytes on amd64. Names share a UTF-8 arena; no full paths
// or per-file Go maps are stored. Zero is the end-of-list sentinel.
type node struct {
	ID, Parent, Name uint64
	Next             uint32
	Length, Flags    uint16
}
type record struct {
	ID, Parent uint64
	Name       string
	Attributes uint32
}
type dirRef struct{ Index, Next uint32 }
type Entry struct {
	ID        uint32 `json:"id"`
	Name      string `json:"name"`
	Directory bool   `json:"directory"`
	Link      bool   `json:"link"`
}
type Crumb struct {
	ID   uint32 `json:"id"`
	Name string `json:"name"`
}
type Page struct {
	Session     string  `json:"session"`
	Directory   uint32  `json:"directory"`
	Items       []Entry `json:"items"`
	Breadcrumbs []Crumb `json:"breadcrumbs"`
	Next        uint32  `json:"next"`
	More        bool    `json:"more"`
	Examined    uint32  `json:"examined"`
}
type index struct {
	mu                      sync.RWMutex
	nodes                   []node
	names                   []byte
	heads                   map[uint64]uint32
	dirs                    map[uint64]dirRef
	folderHeads             map[uint64]uint32
	private                 map[uint64]bool
	files, folders, skipped uint64
	budget                  uint64
	revision                uint64
	viewDir                 uint32
	viewRevision            uint64
	viewIDs                 []uint32
	treeRevision            uint64
	treeKey                 string
	treeRows                []treeRef
	treeMaxDepth            uint32
}

func newIndex(root uint64, label string, budget uint64) *index {
	x := &index{nodes: make([]node, 1), heads: make(map[uint64]uint32), dirs: make(map[uint64]dirRef), folderHeads: make(map[uint64]uint32), private: make(map[uint64]bool), budget: budget}
	x.add(record{ID: root, Name: label, Attributes: 0x10})
	x.folders = 0
	return x
}
func (x *index) memory() uint64 {
	return uint64(cap(x.nodes))*32 + uint64(cap(x.names)) + uint64(len(x.heads)+len(x.dirs)+len(x.folderHeads)+len(x.private))*40 + uint64(cap(x.viewIDs))*4 + uint64(cap(x.treeRows))*12
}
func (x *index) name(n node) string { return string(x.names[n.Name : n.Name+uint64(n.Length)]) }
func (x *index) add(r record) error {
	if len(x.nodes) > 1 && r.ID == x.nodes[1].ID {
		return nil
	}
	if len(r.Name) > math.MaxUint16 || uint64(len(x.nodes)) >= math.MaxUint32 {
		return ErrMemory
	}
	if x.memory() > x.budget {
		return ErrMemory
	}
	flags := uint16(0)
	if r.Attributes&0x10 != 0 {
		flags |= directory
	}
	if r.Attributes&0x400 != 0 {
		flags |= link
	}
	if flags&directory != 0 {
		if _, exists := x.dirs[r.ID]; exists {
			return errors.New("目录标识在扫描期间重复，请重新扫描")
		}
	}
	n := node{ID: r.ID, Parent: r.Parent, Name: uint64(len(x.names)), Length: uint16(len(r.Name)), Flags: flags, Next: x.heads[r.Parent]}
	x.names = append(x.names, r.Name...)
	id := uint32(len(x.nodes))
	x.nodes = append(x.nodes, n)
	x.heads[r.Parent] = id
	if flags&directory != 0 {
		x.dirs[r.ID] = dirRef{Index: id, Next: x.folderHeads[r.Parent]}
		x.folderHeads[r.Parent] = id
		x.folders++
	} else {
		x.files++
	}
	if strings.EqualFold(r.Name, marker) {
		x.private[r.Parent] = true
	}
	x.revision++
	return nil
}
func (x *index) chain(id uint32) ([]Crumb, error) {
	result := []Crumb{}
	seen := make(map[uint32]bool)
	for id != 0 && len(result) < 4096 {
		if int(id) >= len(x.nodes) || seen[id] {
			return nil, ErrScope
		}
		seen[id] = true
		n := x.nodes[id]
		if n.Flags&directory == 0 || n.Flags&link != 0 || x.private[n.ID] {
			return nil, ErrScope
		}
		result = append(result, Crumb{ID: id, Name: x.name(n)})
		if id == 1 {
			for l, r := 0, len(result)-1; l < r; l, r = l+1, r-1 {
				result[l], result[r] = result[r], result[l]
			}
			return result, nil
		}
		id = x.dirs[n.Parent].Index
	}
	return nil, ErrScope
}
func (x *index) page(dir, cursor uint32, limit int, foldersOnly bool) (Page, error) {
	x.mu.RLock()
	defer x.mu.RUnlock()
	p := Page{Directory: dir, Items: []Entry{}}
	if limit < 1 || limit > 200 {
		return p, errors.New("每页数量须为 1–200")
	}
	crumbs, err := x.chain(dir)
	if err != nil {
		return p, err
	}
	p.Breadcrumbs = crumbs
	parent := x.nodes[dir].ID
	if cursor != 0 && (int(cursor) >= len(x.nodes) || x.nodes[cursor].Parent != parent) {
		return p, errors.New("分页位置失效，请刷新目录")
	}
	if foldersOnly && cursor != 0 && x.nodes[cursor].Flags&directory == 0 {
		return p, ErrScope
	}
	if cursor == 0 {
		if foldersOnly {
			cursor = x.folderHeads[parent]
		} else {
			cursor = x.heads[parent]
		}
	}
	for cursor != 0 && len(p.Items) < limit && p.Examined < 8192 {
		n := x.nodes[cursor]
		id := cursor
		if foldersOnly {
			cursor = x.dirs[n.ID].Next
		} else {
			cursor = n.Next
		}
		p.Examined++
		if id == 1 || x.private[n.ID] && n.Flags&directory != 0 {
			continue
		}
		if foldersOnly && n.Flags&directory == 0 {
			continue
		}
		p.Items = append(p.Items, Entry{ID: id, Name: x.name(n), Directory: n.Flags&directory != 0, Link: n.Flags&link != 0})
	}
	p.Next = cursor
	p.More = cursor != 0
	return p, nil
}
