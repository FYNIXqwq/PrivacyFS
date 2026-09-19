package browser

import (
	"errors"
	"fmt"
	"sort"
)

type RangeView struct {
	Session     string  `json:"session"`
	Total       int     `json:"total"`
	Offset      int     `json:"offset"`
	Revision    uint64  `json:"revision"`
	Items       []Entry `json:"items"`
	Breadcrumbs []Crumb `json:"breadcrumbs"`
}
type treeRef struct {
	id, depth uint32
	expanded  bool
}
type TreeEntry struct {
	Entry
	Depth    uint32 `json:"depth"`
	Expanded bool   `json:"expanded"`
}
type TreeView struct {
	Session  string      `json:"session"`
	Total    int         `json:"total"`
	Offset   int         `json:"offset"`
	Items    []TreeEntry `json:"items"`
	MaxDepth uint32      `json:"maxDepth"`
}

func rangeBounds(offset, count, total int) (int, int, error) {
	if offset < 0 || count < 1 || count > 1024 {
		return 0, 0, errors.New("显示范围无效")
	}
	if offset > total {
		offset = total
	}
	end := offset + count
	if end > total {
		end = total
	}
	return offset, end, nil
}
func (x *index) entry(id uint32) Entry {
	n := x.nodes[id]
	return Entry{ID: id, Name: x.name(n), Directory: n.Flags&directory != 0, Link: n.Flags&link != 0}
}
func (x *index) directoryView(dir uint32, offset, count int) (RangeView, error) {
	x.mu.Lock()
	defer x.mu.Unlock()
	out := RangeView{Items: []Entry{}}
	crumbs, err := x.chain(dir)
	if err != nil {
		return out, err
	}
	if x.viewDir != dir || x.viewRevision != x.revision {
		x.viewIDs = x.viewIDs[:0]
		for id := x.heads[x.nodes[dir].ID]; id != 0; id = x.nodes[id].Next {
			n := x.nodes[id]
			if id == 1 || n.Flags&directory != 0 && x.private[n.ID] {
				continue
			}
			if x.memory()+4 > x.budget {
				x.viewIDs = nil
				x.viewDir = 0
				return out, ErrMemory
			}
			x.viewIDs = append(x.viewIDs, id)
		}
		x.viewDir = dir
		x.viewRevision = x.revision
	}
	start, end, err := rangeBounds(offset, count, len(x.viewIDs))
	if err != nil {
		return out, err
	}
	out.Total = len(x.viewIDs)
	out.Offset = start
	out.Revision = x.revision
	out.Breadcrumbs = crumbs
	for _, id := range x.viewIDs[start:end] {
		out.Items = append(out.Items, x.entry(id))
	}
	return out, nil
}
func (x *index) treeView(expanded, collapsed []uint32, all bool, offset, count int) (TreeView, error) {
	x.mu.Lock()
	defer x.mu.Unlock()
	out := TreeView{Items: []TreeEntry{}}
	if _, err := x.chain(1); err != nil {
		return out, err
	}
	expanded = append([]uint32{}, expanded...)
	collapsed = append([]uint32{}, collapsed...)
	sort.Slice(expanded, func(i, j int) bool { return expanded[i] < expanded[j] })
	sort.Slice(collapsed, func(i, j int) bool { return collapsed[i] < collapsed[j] })
	key := fmt.Sprint(all, expanded, collapsed)
	if x.treeKey != key || x.treeRevision != x.revision {
		open := make(map[uint32]bool, len(expanded))
		closed := make(map[uint32]bool, len(collapsed))
		for _, id := range expanded {
			open[id] = true
		}
		for _, id := range collapsed {
			closed[id] = true
		}
		x.treeRows = x.treeRows[:0]
		x.treeMaxDepth = 0
		// Each stack frame stores only a sibling cursor. No names or full paths are
		// copied, even when every branch is expanded.
		type frame struct{ id, depth uint32 }
		stack := []frame{{1, 0}}
		for len(stack) > 0 {
			last := len(stack) - 1
			f := stack[last]
			stack = stack[:last]
			if f.id == 0 {
				continue
			}
			n := x.nodes[f.id]
			if f.id != 1 {
				stack = append(stack, frame{x.dirs[n.ID].Next, f.depth})
			}
			if x.private[n.ID] {
				continue
			}
			isOpen := n.Flags&link == 0 && (all && !closed[f.id] || !all && open[f.id])
			if x.memory()+12 > x.budget {
				x.treeRows = nil
				x.treeKey = ""
				return out, ErrMemory
			}
			x.treeRows = append(x.treeRows, treeRef{f.id, f.depth, isOpen})
			if f.depth > x.treeMaxDepth {
				x.treeMaxDepth = f.depth
			}
			if isOpen {
				stack = append(stack, frame{x.folderHeads[n.ID], f.depth + 1})
			}
		}
		x.treeKey = key
		x.treeRevision = x.revision
	}
	start, end, err := rangeBounds(offset, count, len(x.treeRows))
	if err != nil {
		return out, err
	}
	out.Total = len(x.treeRows)
	out.MaxDepth = x.treeMaxDepth
	out.Offset = start
	for _, r := range x.treeRows[start:end] {
		out.Items = append(out.Items, TreeEntry{Entry: x.entry(r.id), Depth: r.depth, Expanded: r.expanded})
	}
	return out, nil
}
