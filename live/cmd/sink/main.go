// sink is the downstream peer of the gobgpd under test. It counts the routes
// gobgpd sends it and serves the counters as JSON on -http.
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"net/netip"
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
		nlri, withdrawn := len(u.NLRI), len(u.WithdrawnRoutes)
		for _, a := range u.PathAttributes {
			switch a := a.(type) {
			case *bgp.PathAttributeMpReachNLRI:
				nlri += len(a.Value)
			case *bgp.PathAttributeMpUnreachNLRI:
				withdrawn += len(a.Value)
			}
		}
		mu.Lock()
		st.Updates++
		st.NLRI += int64(nlri)
		st.Withdrawn += int64(withdrawn)
		st.LastUpdate = time.Now().UnixNano()
		mu.Unlock()
	}
}
