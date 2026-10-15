// sink is the downstream peer of the gobgpd under test. It counts the routes
// gobgpd sends it and serves the counters as JSON on -http. Routes in
// 198.18.0.0/16 are the markers of blaster: they are not counted, Marker is
// the highest third octet seen.
package main

import (
	"encoding/json"
	"flag"
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
}

var markers = netip.MustParsePrefix("198.18.0.0/16")

// marker returns the marker number of nlri, or -1 for a normal route.
func marker(nlri bgp.NLRI) int {
	var p netip.Prefix
	switch n := nlri.(type) {
	case *bgp.IPAddrPrefix:
		p = n.Prefix
	case *bgp.LabeledVPNIPAddrPrefix:
		p = n.Prefix
	}
	if p.IsValid() && markers.Overlaps(p) {
		return int(p.Addr().As4()[2])
	}
	return -1
}

func main() {
	family := flag.String("family", "ipv4", "ipv4 or vpnv4")
	local := flag.String("local", "::1", "local address")
	remote := flag.String("remote", "[::1]:10179", "gobgpd address")
	addr := flag.String("http", "127.0.0.1:10180", "address to serve the counters on")
	flag.Parse()
	fam := bgp.RF_IPv4_UC
	if *family == "vpnv4" {
		fam = bgp.RF_IPv4_VPN
	}

	var mu sync.Mutex
	var st stats
	http.HandleFunc("/stats", func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		json.NewEncoder(w).Encode(st)
	})
	go func() { log.Fatal(http.ListenAndServe(*addr, nil)) }()

	s, err := speaker.Dial(*local, *remote, 300, netip.MustParseAddr("3.3.3.3"), []bgp.Family{fam})
	if err != nil {
		log.Fatal(err)
	}
	mu.Lock()
	st.Established = true
	mu.Unlock()
	for {
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
		mu.Lock()
		st.Updates++
		for _, n := range reach {
			if k := marker(n.NLRI); k >= 0 {
				st.Marker = max(st.Marker, k)
			} else {
				st.NLRI++
			}
		}
		for _, n := range unreach {
			if marker(n.NLRI) < 0 {
				st.Withdrawn++
			}
		}
		st.LastUpdate = time.Now().UnixNano()
		mu.Unlock()
	}
}
