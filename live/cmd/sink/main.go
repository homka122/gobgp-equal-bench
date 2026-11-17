// sink is the downstream peer of the gobgpd under test. It counts the routes
// gobgpd sends it and serves the counters as JSON on -http. Routes in
// 198.18.0.0/16 are the markers of blaster: they are not counted, Marker is
// the highest third octet seen. Routes in 198.19.0.0/16 are its probes: they
// are not counted either, /probes lists when each was sent and when it came. With -rate it is a slow peer: it takes that
// many routes per second and leaves the rest to queue in gobgpd. With
// -stall-at it is a peer that hangs: once it has marker K it reads nothing
// more, but keeps sending KEEPALIVEs, so gobgpd keeps the session.
//
// With -table it also keeps the routes it has: how many, how many of them
// carry LLGR_STALE, and a digest of the table that does not depend on the
// order the routes came in. It counts the announcements with LLGR_STALE and
// records when the last announcement, withdrawal and announcement with
// LLGR_STALE came. With -llgr its OPEN carries the Graceful Restart and the
// Long-Lived Graceful Restart Capabilities, so gobgpd sends it routes with
// LLGR_STALE instead of withdrawing them.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"hash/fnv"
	"log"
	"net/http"
	"net/netip"
	"slices"
	"sync"
	"time"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"

	"gobgp-equal-bench/live/speaker"
)

type stats struct {
	Established bool  `json:"established"`
	Updates     int64 `json:"updates"`
	NLRI        int64 `json:"nlri"`
	Withdrawn   int64 `json:"withdrawn"`
	LastUpdate  int64 `json:"last_update_unix_nano"`
	Marker      int   `json:"marker"`

	// with -table
	LLGRStale      int64  `json:"llgr_stale,omitempty"`
	LastAnnounce   int64  `json:"last_announce_unix_nano,omitempty"`
	LastWithdraw   int64  `json:"last_withdraw_unix_nano,omitempty"`
	LastLLGRStale  int64  `json:"last_llgr_stale_unix_nano,omitempty"`
	Table          int    `json:"table,omitempty"`
	TableLLGRStale int    `json:"table_llgr_stale,omitempty"`
	TableDigest    string `json:"table_digest,omitempty"`
}

// route is what the table keeps of a route: the hash of its prefix and
// attributes, and whether it carries LLGR_STALE.
type route struct {
	hash uint64
	llgr bool
}

// table is the routes the sink has, without the markers. digest is the sum
// of the hashes of the routes.
type table struct {
	routes map[netip.Prefix]route
	stale  int
	digest uint64
}

func (t *table) set(p netip.Prefix, r route) {
	t.del(p)
	t.routes[p] = r
	t.digest += r.hash
	if r.llgr {
		t.stale++
	}
}

func (t *table) del(p netip.Prefix) {
	if old, ok := t.routes[p]; ok {
		delete(t.routes, p)
		t.digest -= old.hash
		if old.llgr {
			t.stale--
		}
	}
}

// attrsHash hashes the path attributes of an UPDATE other than the
// multiprotocol ones, which carry the NLRI.
func attrsHash(u *bgp.BGPUpdate) (uint64, bool) {
	h := fnv.New64a()
	llgr := false
	for _, a := range u.PathAttributes {
		switch a := a.(type) {
		case *bgp.PathAttributeMpReachNLRI, *bgp.PathAttributeMpUnreachNLRI:
			continue
		case *bgp.PathAttributeCommunities:
			llgr = llgr || slices.Contains(a.Value, uint32(bgp.COMMUNITY_LLGR_STALE))
		}
		b, err := a.Serialize()
		if err != nil {
			log.Fatal(err)
		}
		h.Write(b)
	}
	return h.Sum64(), llgr
}

func routeHash(attrs uint64, p netip.Prefix) uint64 {
	h := fnv.New64a()
	b, _ := p.MarshalBinary()
	h.Write(b)
	fmt.Fprint(h, attrs)
	return h.Sum64()
}

var (
	markers = netip.MustParsePrefix("198.18.0.0/16")
	probes  = netip.MustParsePrefix("198.19.0.0/16")
)

// stamp returns the time blaster put into a probe (withStamp), in Unix
// nanoseconds, or 0.
func stamp(u *bgp.BGPUpdate) int64 {
	for _, a := range u.PathAttributes {
		if l, ok := a.(*bgp.PathAttributeLargeCommunities); ok {
			for _, c := range l.Values {
				if c.ASN == 65001 {
					return int64(uint64(c.LocalData1)<<32 | uint64(c.LocalData2))
				}
			}
		}
	}
	return 0
}

func prefix(nlri bgp.NLRI) netip.Prefix {
	switch n := nlri.(type) {
	case *bgp.IPAddrPrefix:
		return n.Prefix
	case *bgp.LabeledVPNIPAddrPrefix:
		return n.Prefix
	}
	return netip.Prefix{}
}

// marker returns the marker number of nlri, or -1 for a normal route.
func marker(nlri bgp.NLRI) int {
	if p := prefix(nlri); p.IsValid() && markers.Overlaps(p) {
		return int(p.Addr().As4()[2])
	}
	return -1
}

func main() {
	family := flag.String("family", "ipv4", "ipv4 or vpnv4")
	local := flag.String("local", "::1", "local address")
	remote := flag.String("remote", "[::1]:10179", "gobgpd address")
	addr := flag.String("http", "127.0.0.1:10180", "address to serve the counters on")
	id := flag.String("id", "3.3.3.3", "BGP identifier")
	rate := flag.Int("rate", 0, "routes to take per second; 0: as fast as they come")
	keep := flag.Bool("table", false, "keep the routes and report the table")
	llgr := flag.Bool("llgr", false, "advertise the Graceful Restart and Long-Lived Graceful Restart Capabilities")
	stallAt := flag.Int("stall-at", 0, "stop reading once this marker has come; 0: never")
	hold := flag.Int("hold", 90, "Hold Time to offer, s")
	flag.Parse()
	speaker.HoldTime = uint16(*hold)
	fam := bgp.RF_IPv4_UC
	if *family == "vpnv4" {
		fam = bgp.RF_IPv4_VPN
	}

	var mu sync.Mutex
	var st stats
	tab := table{routes: map[netip.Prefix]route{}}
	var probed [][2]int64 // [sent, came], Unix nanoseconds
	http.HandleFunc("/stats", func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if *keep {
			st.Table, st.TableLLGRStale, st.TableDigest = len(tab.routes), tab.stale, fmt.Sprintf("%016x", tab.digest)
		}
		json.NewEncoder(w).Encode(st)
	})
	http.HandleFunc("/probes", func(w http.ResponseWriter, _ *http.Request) {
		// copy under the lock and write without it: the reader of the
		// session must not wait for the HTTP client
		mu.Lock()
		got := slices.Clone(probed)
		mu.Unlock()
		json.NewEncoder(w).Encode(got)
	})
	go func() { log.Fatal(http.ListenAndServe(*addr, nil)) }()

	var caps []bgp.ParameterCapabilityInterface
	if *llgr {
		caps = append(caps,
			bgp.NewCapGracefulRestart(false, false, 15, []*bgp.CapGracefulRestartTuple{bgp.NewCapGracefulRestartTuple(fam, false)}),
			bgp.NewCapLongLivedGracefulRestart([]*bgp.CapLongLivedGracefulRestartTuple{bgp.NewCapLongLivedGracefulRestartTuple(fam, false, 86400)}))
	}
	s, err := speaker.Dial(*local, *remote, 300, netip.MustParseAddr(*id), []bgp.Family{fam}, caps...)
	if err != nil {
		log.Fatal(err)
	}
	mu.Lock()
	st.Established = true
	mu.Unlock()
	var next time.Time // with -rate: when the next UPDATE may be read
	for {
		// short waits are saved up: a sleep of microseconds takes far longer
		if d := time.Until(next); d > time.Millisecond {
			time.Sleep(d)
		}
		m, err := s.Read()
		if err != nil {
			log.Fatalf("session closed: %v", err)
		}
		u, ok := m.Body.(*bgp.BGPUpdate)
		if !ok {
			continue
		}
		reach, unreach := u.NLRI, u.WithdrawnRoutes
		for _, a := range u.PathAttributes {
			switch a := a.(type) {
			case *bgp.PathAttributeMpReachNLRI:
				reach = append(slices.Clip(reach), a.Value...)
			case *bgp.PathAttributeMpUnreachNLRI:
				unreach = append(slices.Clip(unreach), a.Value...)
			}
		}
		var attrs uint64
		var stale bool
		if *keep && len(reach) > 0 {
			attrs, stale = attrsHash(u)
		}
		mu.Lock()
		st.Updates++
		now := time.Now().UnixNano()
		for _, n := range reach {
			if k := marker(n.NLRI); k >= 0 {
				st.Marker = max(st.Marker, k)
			} else if probes.Contains(prefix(n.NLRI).Addr()) {
				probed = append(probed, [2]int64{stamp(u), now})
			} else {
				st.NLRI++
				if *keep {
					p := prefix(n.NLRI)
					tab.set(p, route{routeHash(attrs, p), stale})
					st.LastAnnounce = now
					if stale {
						st.LLGRStale++
						st.LastLLGRStale = now
					}
				}
			}
		}
		for _, n := range unreach {
			if marker(n.NLRI) < 0 && !probes.Contains(prefix(n.NLRI).Addr()) {
				st.Withdrawn++
				if *keep {
					tab.del(prefix(n.NLRI))
					st.LastWithdraw = now
				}
			}
		}
		st.LastUpdate = now
		stall := *stallAt > 0 && st.Marker >= *stallAt
		mu.Unlock()
		if stall {
			select {} // the speaker sends the KEEPALIVEs; /stats still answers
		}
		if *rate > 0 {
			// a sink that was idle does not read faster later: it makes up
			// for 0.1 s at most, which covers a sleep that took too long
			if floor := time.Now().Add(-100 * time.Millisecond); next.Before(floor) {
				next = floor
			}
			next = next.Add(time.Duration(len(reach)+len(unreach)) * time.Second / time.Duration(*rate))
		}
	}
}
