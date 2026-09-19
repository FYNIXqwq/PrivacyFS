//go:build windows

package browser

import (
	"encoding/binary"
	"testing"
)

func usnName(units []uint16) []byte {
	size := (60 + len(units)*2 + 7) &^ 7
	b := make([]byte, size)
	binary.LittleEndian.PutUint32(b, uint32(size))
	binary.LittleEndian.PutUint16(b[4:], 2)
	binary.LittleEndian.PutUint64(b[8:], 1<<63+100)
	binary.LittleEndian.PutUint64(b[16:], 5)
	binary.LittleEndian.PutUint16(b[56:], uint16(len(units)*2))
	binary.LittleEndian.PutUint16(b[58:], 60)
	for i, v := range units {
		binary.LittleEndian.PutUint16(b[60+i*2:], v)
	}
	return b
}
func TestUSNRecordValidation(t *testing.T) {
	good := usnName([]uint16{'张', '三', '.', 't', 'x', 't'})
	r, e := parseUSN(good)
	if e != nil || len(r) != 1 || r[0].ID != 1<<63+100 || r[0].Name != "张三.txt" {
		t.Fatal(r, e)
	}
	for _, v := range [][]byte{good[:len(good)-1], make([]byte, 64), usnName([]uint16{'.', '.'}), usnName([]uint16{'a', '\\', 'b'})} {
		if _, e := parseUSN(v); e == nil {
			t.Fatal("invalid record accepted")
		}
	}
	malformed := append([]byte{}, good...)
	binary.LittleEndian.PutUint16(malformed[4:], 3)
	if _, e := parseUSN(malformed); e == nil {
		t.Fatal("unknown version accepted")
	}
	r, e = parseUSN(usnName([]uint16{'a', 0xd800}))
	if e != nil || r[0].Name != `a\ud800` {
		t.Fatal(r, e)
	}
}
