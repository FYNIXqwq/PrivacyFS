//go:build !windows

package browser

import (
	"context"
	"errors"
	"os"
	"path/filepath"
)

var ErrUnavailable = errors.New("此平台不支持 NTFS 索引")

func scanNTFS(context.Context, string, func(uint64, string), func([]record) error) error {
	return ErrUnavailable
}
func samePath(a, b string) bool              { return filepath.Clean(a) == filepath.Clean(b) }
func isReparse(_ string, i os.FileInfo) bool { return i.Mode()&os.ModeSymlink != 0 }
func memoryBudget() uint64                   { return 1 << 30 }
