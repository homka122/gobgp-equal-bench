// flapper is a downstream peer that goes away and comes back: each time its
// session comes up, gobgpd sends it the whole table. It connects to gobgpd,
// reads everything it gets and reads commands from stdin, one per line:
//
//	flap I S R  for S seconds: drop the session I seconds after it came up,
//	            without a NOTIFICATION, and connect again; while gobgpd
//	            refuses the connection, try again every R seconds. I = 0
//	            keeps the session up
//	quit
//
// It prints {"ready": true} once the first session is up and one JSON line
// when a command is done: the sessions it had during the command. A session
// has when it came up, when all -routes routes of the table had come (the
// markers and probes of blaster, 198.18.0.0/15, left out), when it ended and
// how many attempts it took to come up. Times are Unix nanoseconds.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/netip"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"

	"gobgp-equal-bench/live/speaker"
)

var (
	family = flag.String("family", "ipv4", "ipv4 or vpnv4")
	local  = flag.String("local", "127.0.0.3", "local address")
	remote = flag.String("remote", "127.0.0.1:10179", "gobgpd address")
	id     = flag.String("id", "4.4.4.4", "BGP identifier")
	routes = flag.Int64("routes", 0, "routes in the table of gobgpd")
	hold   = flag.Int("hold", 90, "Hold Time to offer, s")
)

var blasterRoutes = netip.MustParsePrefix("198.18.0.0/15")

type record struct {
	Up            int64 `json:"up_unix_nano"`
	Full          int64 `json:"full_unix_nano,omitempty"`
	Down          int64 `json:"down_unix_nano,omitempty"`
	Routes        int64 `json:"routes"`
	Attempts      int   `json:"attempts"`
	DroppedByPeer bool  `json:"dropped_by_peer,omitempty"`
}

type session struct {
	s    *speaker.Session
	done chan struct{}
	mu   sync.Mutex
	rec  record
}

func (x *session) snapshot() record {
	x.mu.Lock()
	defer x.mu.Unlock()
	return x.rec
}

func (x *session) read() {
	defer close(x.done)
	for {
		m, err := x.s.Read()
		if err != nil {
			x.mu.Lock()
			x.rec.Down = time.Now().UnixNano()
			x.rec.DroppedByPeer = !x.s.Closed()
			x.mu.Unlock()
			return
		}
		u, ok := m.Body.(*bgp.BGPUpdate)
		if !ok {
			continue
		}
		reach := u.NLRI
		for _, a := range u.PathAttributes {
			if mp, ok := a.(*bgp.PathAttributeMpReachNLRI); ok {
				reach = append(slices.Clip(reach), mp.Value...)
			}
		}
		n := int64(0)
		for _, r := range reach {
			if p := prefix(r.NLRI); !p.IsValid() || !blasterRoutes.Contains(p.Addr()) {
				n++
			}
		}
		if n == 0 {
			continue
		}
		x.mu.Lock()
		x.rec.Routes += n
		if x.rec.Full == 0 && x.rec.Routes >= *routes {
			x.rec.Full = time.Now().UnixNano()
		}
		x.mu.Unlock()
	}
}

func prefix(n bgp.NLRI) netip.Prefix {
	switch n := n.(type) {
	case *bgp.IPAddrPrefix:
		return n.Prefix
	case *bgp.LabeledVPNIPAddrPrefix:
		return n.Prefix
	}
	return netip.Prefix{}
}

// connect tries to bring a session up every retry until it is up or until
// deadline; nil if the deadline passed.
func connect(fam bgp.Family, retry time.Duration, deadline time.Time) *session {
	for attempts := 1; ; attempts++ {
		s, err := speaker.Dial(*local, *remote, 300, netip.MustParseAddr(*id), []bgp.Family{fam})
		if err == nil {
			x := &session{s: s, done: make(chan struct{}), rec: record{Up: time.Now().UnixNano(), Attempts: attempts}}
			go x.read()
			return x
		}
		if !time.Now().Add(retry).Before(deadline) {
			return nil
		}
		time.Sleep(retry)
	}
}

func main() {
	flag.Parse()
	speaker.HoldTime = uint16(*hold)
	fam := bgp.RF_IPv4_UC
	if *family == "vpnv4" {
		fam = bgp.RF_IPv4_VPN
	}
	cur := connect(fam, 100*time.Millisecond, time.Now().Add(time.Minute))
	if cur == nil {
		log.Fatal("no session with gobgpd in a minute")
	}
	out := json.NewEncoder(os.Stdout)
	out.Encode(map[string]any{"ready": true})
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		var cmd string
		var interval, secs, retry float64
		fmt.Sscan(in.Text(), &cmd, &interval, &secs, &retry)
		switch cmd {
		case "flap":
			end := time.Now().Add(seconds(secs))
			var done []*session
			for cur != nil && time.Now().Before(end) {
				wait := time.Until(end)
				if interval > 0 {
					wait = min(wait, time.Until(time.Unix(0, cur.snapshot().Up).Add(seconds(interval))))
				}
				select {
				case <-cur.done:
				case <-time.After(wait):
				}
				if !time.Now().Before(end) {
					break
				}
				cur.s.Close()
				<-cur.done
				done = append(done, cur)
				cur = connect(fam, seconds(retry), end)
			}
			var recs []record
			for _, x := range done {
				recs = append(recs, x.snapshot())
			}
			if cur != nil {
				recs = append(recs, cur.snapshot())
			}
			out.Encode(map[string]any{"cmd": cmd, "sessions": recs})
		case "quit":
			if cur != nil {
				cur.s.Close()
			}
			return
		default:
			log.Fatalf("unknown command %q", strings.TrimSpace(in.Text()))
		}
	}
}

func seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }
