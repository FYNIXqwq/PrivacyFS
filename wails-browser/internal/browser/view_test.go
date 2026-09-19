package browser

import (
	"strconv"
	"testing"
)

func TestViewCanSeekDirectlyToMillionthEntry(t *testing.T) {
	x := newIndex(5, "root", 1<<30)
	for i := 0; i < 1000000; i++ {
		if e := x.add(record{ID: uint64(100 + i), Parent: 5, Name: "file-" + strconv.Itoa(i)}); e != nil {
			t.Fatal(e)
		}
	}
	end, e := x.directoryView(1, 999995, 40)
	if e != nil || end.Total != 1000000 || len(end.Items) != 5 || end.Items[4].Name != "file-0" {
		t.Fatal(end.Total, len(end.Items), e)
	}
	middle, e := x.directoryView(1, 450123, 40)
	if e != nil || len(middle.Items) != 40 || middle.Items[0].Name != "file-549876" {
		t.Fatal(middle, e)
	}
	if x.viewRevision != x.revision || len(x.viewIDs) != 1000000 {
		t.Fatal("incomplete view")
	}
	_ = x.add(record{ID: 2000000, Parent: 5, Name: "new"})
	first, e := x.directoryView(1, 0, 1)
	if e != nil || first.Total != 1000001 || first.Items[0].Name != "new" {
		t.Fatal(first, e)
	}
}
func TestTreeExpandAllAndCollapsePreserveFullHierarchy(t *testing.T) {
	x := newIndex(5, "root", 1<<28)
	for i := 0; i < 1000; i++ {
		_ = x.add(record{ID: uint64(1000 + i), Parent: 5, Name: "folder", Attributes: 0x10})
		_ = x.add(record{ID: uint64(10000 + i), Parent: uint64(1000 + i), Name: "child", Attributes: 0x10})
	}
	view, e := x.treeView([]uint32{1}, nil, false, 0, 30)
	if e != nil || view.Total != 1001 {
		t.Fatal(view, e)
	}
	full, e := x.treeView(nil, nil, true, 1995, 30)
	if e != nil || full.Total != 2001 || len(full.Items) != 6 || full.Items[5].Depth != 2 {
		t.Fatal(full, e)
	}
	collapsed, e := x.treeView(nil, []uint32{x.dirs[1000].Index}, true, 1995, 30)
	if e != nil || collapsed.Total != 2000 {
		t.Fatal(collapsed, e)
	}
	root, e := x.treeView(nil, []uint32{1}, true, 0, 30)
	if e != nil || root.Total != 1 {
		t.Fatal(root, e)
	}
}
func TestViewPrivateDirectoriesAndLinks(t *testing.T) {
	x := newIndex(5, "root", 1<<20)
	for _, r := range []record{{ID: 100, Parent: 5, Name: "hidden", Attributes: 0x10}, {ID: 200, Parent: 100, Name: marker}, {ID: 101, Parent: 5, Name: "link", Attributes: 0x410}, {ID: 102, Parent: 101, Name: "outside", Attributes: 0x10}} {
		_ = x.add(r)
	}
	tree, e := x.treeView(nil, nil, true, 0, 40)
	if e != nil || tree.Total != 2 || tree.Items[1].Expanded {
		t.Fatal(tree, e)
	}
	if _, e = x.directoryView(x.dirs[100].Index, 0, 40); e == nil {
		t.Fatal("private scope")
	}
	if _, e = x.directoryView(x.dirs[101].Index, 0, 40); e == nil {
		t.Fatal("link followed")
	}
}
