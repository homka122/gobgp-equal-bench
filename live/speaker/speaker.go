// Package speaker is a minimal BGP speaker: it opens a session to gobgpd and
// then only sends and reads raw messages, so the test controls every byte of
// the UPDATEs it sends.
package speaker

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"
)

// HoldTime is the Hold Time the OPEN offers, s; gobgpd takes the smaller of
// it and its own.
var HoldTime uint16 = 90

type Session struct {
	conn   net.Conn
	r      *bufio.Reader
	mu     sync.Mutex
	w      *bufio.Writer
	closed atomic.Bool
}

// Dial connects from local to remote and exchanges OPEN and KEEPALIVE. The
// OPEN carries more capabilities after the multiprotocol ones, if given.
// Like a BGP speaker, the session ends when nothing comes from the peer for
// HoldTime seconds: Read and ReadRaw fail then.
func Dial(local, remote string, as uint32, id netip.Addr, families []bgp.Family, more ...bgp.ParameterCapabilityInterface) (*Session, error) {
	d := net.Dialer{Timeout: 5 * time.Second}
	if local != "" {
		d.LocalAddr = &net.TCPAddr{IP: net.ParseIP(local)}
	}
	var conn net.Conn
	var err error
	for range 50 {
		if conn, err = d.Dial("tcp", remote); err == nil {
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	if err != nil {
		return nil, err
	}
	s := &Session{conn: conn, r: bufio.NewReaderSize(conn, 1<<16), w: bufio.NewWriterSize(conn, 1<<16)}
	if err := s.handshake(as, id, families, more); err != nil {
		conn.Close()
		return nil, err
	}
	go s.keepalive()
	return s, nil
}

func (s *Session) handshake(as uint32, id netip.Addr, families []bgp.Family, more []bgp.ParameterCapabilityInterface) error {
	caps := []bgp.ParameterCapabilityInterface{bgp.NewCapFourOctetASNumber(as)}
	for _, f := range families {
		caps = append(caps, bgp.NewCapMultiProtocol(f))
	}
	caps = append(caps, more...)
	open, err := bgp.NewBGPOpenMessage(uint16(as), HoldTime, id, []bgp.OptionParameterInterface{bgp.NewOptionParameterCapability(caps)})
	if err != nil {
		return err
	}
	if err := s.Send(open); err != nil {
		return err
	}
	if err := s.Flush(); err != nil {
		return err
	}
	m, err := s.Read()
	if err != nil {
		return err
	}
	if m.Header.Type != bgp.BGP_MSG_OPEN {
		return fmt.Errorf("expected OPEN, got type %d", m.Header.Type)
	}
	if err := s.Send(bgp.NewBGPKeepAliveMessage()); err != nil {
		return err
	}
	if err := s.Flush(); err != nil {
		return err
	}
	if m, err = s.Read(); err != nil {
		return err
	}
	if m.Header.Type != bgp.BGP_MSG_KEEPALIVE {
		return fmt.Errorf("expected KEEPALIVE, got type %d", m.Header.Type)
	}
	return nil
}

func (s *Session) keepalive() {
	if HoldTime == 0 {
		return // no Hold Time, no KEEPALIVE (RFC 4271 4.4)
	}
	for range time.Tick(time.Duration(HoldTime) * time.Second / 3) {
		if s.Send(bgp.NewBGPKeepAliveMessage()) != nil || s.Flush() != nil {
			return
		}
	}
}

// Send buffers a message; call Flush to write it out.
func (s *Session) Send(m *bgp.BGPMessage) error {
	b, err := m.Serialize()
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = s.w.Write(b)
	return err
}

func (s *Session) Flush() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.w.Flush()
}

// ReadRaw returns the next message without decoding its body.
func (s *Session) ReadRaw() ([]byte, error) {
	if HoldTime > 0 {
		s.conn.SetReadDeadline(time.Now().Add(time.Duration(HoldTime) * time.Second))
	}
	h := make([]byte, bgp.BGP_HEADER_LENGTH)
	if _, err := io.ReadFull(s.r, h); err != nil {
		if errors.Is(err, os.ErrDeadlineExceeded) {
			return nil, fmt.Errorf("hold timer expired: nothing from the peer for %d s", HoldTime)
		}
		return nil, err
	}
	n := int(binary.BigEndian.Uint16(h[16:18]))
	if n < bgp.BGP_HEADER_LENGTH {
		return nil, fmt.Errorf("bad message length %d", n)
	}
	b := make([]byte, n)
	copy(b, h)
	if _, err := io.ReadFull(s.r, b[bgp.BGP_HEADER_LENGTH:]); err != nil {
		return nil, err
	}
	return b, nil
}

func (s *Session) Read() (*bgp.BGPMessage, error) {
	b, err := s.ReadRaw()
	if err != nil {
		return nil, err
	}
	return bgp.ParseBGPMessage(b)
}

// Close closes the TCP connection without a NOTIFICATION.
func (s *Session) Close() error {
	s.closed.Store(true)
	return s.conn.Close()
}

// Closed reports whether Close was called: a read error after it is
// expected.
func (s *Session) Closed() bool { return s.closed.Load() }
