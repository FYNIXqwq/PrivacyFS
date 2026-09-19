//go:build windows

package browser

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"golang.org/x/sys/windows"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf16"
	"unsafe"
)

var ErrUnavailable = errors.New("NTFS 不可用")

func localPath(p string) string {
	if strings.HasPrefix(p, `\\?\UNC\`) {
		return `\\` + p[8:]
	}
	return strings.TrimPrefix(p, `\\?\`)
}
func samePath(a, b string) bool {
	return strings.EqualFold(filepath.Clean(localPath(a)), filepath.Clean(localPath(b)))
}
func longPath(p string) string {
	if strings.HasPrefix(p, `\\?\`) {
		return p
	}
	if strings.HasPrefix(p, `\\`) {
		return `\\?\UNC\` + p[2:]
	}
	return `\\?\` + p
}
func isReparse(path string, info os.FileInfo) bool {
	if info.Mode()&os.ModeSymlink != 0 {
		return true
	}
	p, e := windows.UTF16PtrFromString(longPath(path))
	if e != nil {
		return true
	}
	a, e := windows.GetFileAttributes(p)
	return e != nil || a&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0
}
func memoryBudget() uint64 {
	type memoryStatus struct {
		Length, Load                                                                         uint32
		Total, Available, TotalPage, AvailablePage, TotalVirtual, AvailableVirtual, Extended uint64
	}
	m := memoryStatus{}
	m.Length = uint32(unsafe.Sizeof(m))
	proc := windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")
	ok, _, _ := proc.Call(uintptr(unsafe.Pointer(&m)))
	if ok == 0 {
		return 1 << 30
	}
	budget := m.Available / 3
	if budget > 4<<30 {
		budget = 4 << 30
	}
	return budget
}
func openMeta(path string) (windows.Handle, error) {
	p, e := windows.UTF16PtrFromString(longPath(path))
	if e != nil {
		return 0, e
	}
	return windows.CreateFile(p, windows.FILE_READ_ATTRIBUTES, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
}
func fileInfo(h windows.Handle) (windows.ByHandleFileInformation, uint64, error) {
	var i windows.ByHandleFileInformation
	e := windows.GetFileInformationByHandle(h, &i)
	return i, uint64(i.FileIndexHigh)<<32 | uint64(i.FileIndexLow), e
}
func scanNTFS(ctx context.Context, root string, init func(uint64, string), add func([]record) error) error {
	root = localPath(root)
	if strings.HasPrefix(root, `\\`) {
		return fmt.Errorf("%w：网络目录请用普通遍历", ErrUnavailable)
	}
	p, e := windows.UTF16PtrFromString(root)
	if e != nil {
		return ErrUnavailable
	}
	mount := make([]uint16, 32768)
	if windows.GetVolumePathName(p, &mount[0], uint32(len(mount))) != nil {
		return ErrUnavailable
	}
	fsname := make([]uint16, 64)
	if windows.GetVolumeInformation(&mount[0], nil, 0, nil, nil, nil, &fsname[0], uint32(len(fsname))) != nil || windows.UTF16ToString(fsname) != "NTFS" {
		return fmt.Errorf("%w：不是本地 NTFS 卷", ErrUnavailable)
	}
	guid := make([]uint16, 64)
	if windows.GetVolumeNameForVolumeMountPoint(&mount[0], &guid[0], uint32(len(guid))) != nil {
		return ErrUnavailable
	}
	selected, e := openMeta(root)
	if e != nil {
		return fmt.Errorf("%w：无法读取目录属性", ErrUnavailable)
	}
	defer windows.CloseHandle(selected)
	info, rootID, e := fileInfo(selected)
	if e != nil || info.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 || info.FileAttributes&windows.FILE_ATTRIBUTE_DIRECTORY == 0 {
		return ErrUnavailable
	}
	canonical, e := openMeta(windows.UTF16ToString(guid))
	if e != nil {
		return ErrUnavailable
	}
	volumeInfo, volumeRootID, e := fileInfo(canonical)
	windows.CloseHandle(canonical)
	if e != nil || volumeInfo.VolumeSerialNumber != info.VolumeSerialNumber {
		return fmt.Errorf("%w：卷身份不一致", ErrUnavailable)
	}
	volumePath, _ := windows.UTF16PtrFromString(strings.TrimSuffix(windows.UTF16ToString(guid), `\`))
	volume, e := windows.CreateFile(volumePath, windows.GENERIC_READ, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, 0, 0)
	if e != nil {
		return fmt.Errorf("%w：缺少卷读取权限，可使用管理员入口", ErrUnavailable)
	}
	defer windows.CloseHandle(volume)
	// USN name metadata only. No raw sectors, source bodies or journal writes.
	request := make([]byte, 24)
	binary.LittleEndian.PutUint64(request[16:], 0x7fffffffffffffff)
	buffer := make([]byte, 1024*1024)
	cursor := uint64(0)
	initialised := false
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		binary.LittleEndian.PutUint64(request, cursor)
		var used uint32
		e = windows.DeviceIoControl(volume, 0x000900b3, &request[0], uint32(len(request)), &buffer[0], uint32(len(buffer)), &used, nil)
		if e == windows.ERROR_HANDLE_EOF {
			if !initialised {
				init(rootID, "ntfs")
			}
			return nil
		}
		if e != nil {
			if !initialised {
				return fmt.Errorf("%w：元数据接口无法读取", ErrUnavailable)
			}
			return errors.New("读取 NTFS 元数据失败；当前仅有部分结构")
		}
		if used < 8 || used > uint32(len(buffer)) {
			return errors.New("NTFS 元数据长度无效")
		}
		next := binary.LittleEndian.Uint64(buffer[:8])
		if next <= cursor {
			return errors.New("NTFS 枚举游标未前进")
		}
		records, err := parseUSN(buffer[8:used])
		if err != nil {
			return err
		}
		for _, r := range records {
			if r.Name == "." && r.ID != volumeRootID {
				return errors.New("NTFS 根目录记录无效")
			}
		}
		if !initialised {
			init(rootID, "ntfs")
			initialised = true
		}
		if err = add(records); err != nil {
			return err
		}
		cursor = next
	}
}
func parseUSN(data []byte) ([]record, error) {
	result := make([]record, 0, len(data)/100)
	bad := errors.New("NTFS 记录格式无效或版本不支持")
	for pos := 0; pos < len(data); {
		if len(data)-pos < 60 {
			return nil, bad
		}
		r := data[pos:]
		size := int(binary.LittleEndian.Uint32(r))
		major := binary.LittleEndian.Uint16(r[4:])
		length, offset := int(binary.LittleEndian.Uint16(r[56:])), int(binary.LittleEndian.Uint16(r[58:]))
		if major != 2 || size < 60 || size%8 != 0 || size > len(r) || length == 0 || length%2 != 0 || length > 510 || offset < 60 || offset+length > size {
			return nil, bad
		}
		units := make([]uint16, length/2)
		for i := range units {
			units[i] = binary.LittleEndian.Uint16(r[offset+i*2:])
			if units[i] == 0 || units[i] == '/' || units[i] == '\\' || units[i] == ':' {
				return nil, bad
			}
		}
		var text strings.Builder
		text.Grow(length * 2)
		for i := 0; i < len(units); i++ {
			u := units[i]
			if u >= 0xd800 && u <= 0xdbff && i+1 < len(units) && units[i+1] >= 0xdc00 && units[i+1] <= 0xdfff {
				text.WriteRune(utf16.DecodeRune(rune(u), rune(units[i+1])))
				i++
			} else if u >= 0xd800 && u <= 0xdfff {
				fmt.Fprintf(&text, "\\u%04x", u)
			} else {
				text.WriteRune(rune(u))
			}
		}
		name := text.String()
		if name == ".." {
			return nil, bad
		}
		result = append(result, record{ID: binary.LittleEndian.Uint64(r[8:]), Parent: binary.LittleEndian.Uint64(r[16:]), Attributes: binary.LittleEndian.Uint32(r[52:]), Name: name})
		pos += size
	}
	return result, nil
}
