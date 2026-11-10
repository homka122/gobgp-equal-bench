// blaster is the sender: the upstream peer of the gobgpd under test. It
// builds every UPDATE itself, so it can announce a route again with the same
// content in another encoding, which gobgpd as a peer never does.
//
// It reads commands from stdin, one per line:
//
//	load        announce all routes once
//	storm R     announce all routes R more times, as -resend says
//	mark K      announce the marker route 198.18.K.0/24
//	quit
//
// and prints one JSON line on stdout when a command has been sent.
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
	"time"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"

	"gobgp-equal-bench/live/speaker"
)

const (
	localAS = 100
	label   = 100
)

var (
	family    = flag.String("family", "ipv4", "ipv4 or vpnv4")
	routes    = flag.Int("routes", 10000, "number of routes")
	perUpdate = flag.Int("per-update", 1, "routes per UPDATE")
	local     = flag.String("local", "127.0.0.1", "local address")
	remote    = flag.String("remote", "127.0.0.1:10179", "gobgpd address")
	standard  = flag.Int("standard", 0, "COMMUNITIES values per route")
	extended  = flag.Int("extended", 0, "route-target extended communities per route")
	large     = flag.Int("large", 0, "large communities per route")
	order     = flag.String("order", "sorted", "order of the community values: sorted, random or reversed")
	seed      = flag.Uint64("seed", 1, "seed of the random order")
	resend    = flag.String("resend", "same", "what a storm round sends: "+strings.Join(resends, ", "))
)

func main() {
	flag.Parse()
	set, err := newCommunitySet(*standard, *extended, *large, *order, *seed)
	if err != nil {
		log.Fatal(err)
	}
	if err := set.supports(*resend); err != nil {
		log.Fatal(err)
	}
	fam := bgp.RF_IPv4_UC
	if *family == "vpnv4" {
		fam = bgp.RF_IPv4_VPN
	}
	s, err := speaker.Dial(*local, *remote, localAS, netip.MustParseAddr("1.1.1.1"), []bgp.Family{fam})
	if err != nil {
		log.Fatal(err)
	}
	go func() { // drain whatever gobgpd sends us
		for {
			if _, err := s.ReadRaw(); err != nil {
				log.Fatalf("session closed: %v", err)
			}
		}
	}()
	nlris := make([]bgp.PathNLRI, *routes)
	for i := range *routes {
		// 10.x.y.0/24, then 11.x.y.0/24 after 65536 routes
		nlris[i] = nlri(fam, netip.PrefixFrom(netip.AddrFrom4([4]byte{byte(10 + i>>16), byte(i >> 8), byte(i), 0}), 24))
	}

	out := json.NewEncoder(os.Stdout)
	out.Encode(map[string]any{"ready": true})
	round := 0
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		var cmd string
		var arg int
		fmt.Sscan(in.Text(), &cmd, &arg)
		start := time.Now()
		updates := 0
		switch cmd {
		case "load":
			updates = announce(s, fam, nlris, set.attrs(fam, *resend, round))
			round++
		case "storm":
			for range arg {
				updates += announce(s, fam, nlris, set.attrs(fam, *resend, round))
				round++
			}
		case "mark":
			// gobgpd handles the UPDATEs of a peer in the order they come
			// and sends to each peer through a FIFO queue: when a receiver
			// has the marker, gobgpd has handled every UPDATE before it and
			// sent whatever they caused.
			p := netip.PrefixFrom(netip.AddrFrom4([4]byte{198, 18, byte(arg), 0}), 24)
			updates = announce(s, fam, []bgp.PathNLRI{nlri(fam, p)}, set.attrs(fam, "same", 0))
		case "quit":
			s.Close()
			return
		default:
			log.Fatalf("unknown command %q", strings.TrimSpace(in.Text()))
		}
		out.Encode(map[string]any{"cmd": cmd, "updates": updates, "seconds": time.Since(start).Seconds()})
	}
}

func nlri(fam bgp.Family, p netip.Prefix) bgp.PathNLRI {
	var n bgp.NLRI
	var err error
	if fam == bgp.RF_IPv4_VPN {
		n, err = bgp.NewLabeledVPNIPAddrPrefix(p, *bgp.NewMPLSLabelStack(label), bgp.NewRouteDistinguisherTwoOctetAS(65000, 1))
	} else {
		n, err = bgp.NewIPAddrPrefix(p)
	}
	if err != nil {
		log.Fatal(err)
	}
	return bgp.PathNLRI{NLRI: n}
}

// withByType returns a with x inserted before the first attribute of a
// higher type code, so a list sorted by type stays sorted.
func withByType(a []bgp.PathAttributeInterface, x bgp.PathAttributeInterface) []bgp.PathAttributeInterface {
	i := slices.IndexFunc(a, func(y bgp.PathAttributeInterface) bool { return y.GetType() > x.GetType() })
	if i < 0 {
		i = len(a)
	}
	return slices.Insert(slices.Clone(a), i, x)
}

func announce(s *speaker.Session, fam bgp.Family, nlris []bgp.PathNLRI, a []bgp.PathAttributeInterface) int {
	updates := 0
	for i := 0; i < len(nlris); i += *perUpdate {
		batch := nlris[i:min(i+*perUpdate, len(nlris))]
		var m *bgp.BGPMessage
		if fam == bgp.RF_IPv4_UC {
			m = bgp.NewBGPUpdateMessage(nil, a, batch)
		} else {
			mp, err := bgp.NewPathAttributeMpReachNLRI(fam, batch, netip.MustParseAddr("127.0.0.1"))
			if err != nil {
				log.Fatal(err)
			}
			m = bgp.NewBGPUpdateMessage(nil, withByType(a, mp), nil)
		}
		if err := s.Send(m); err != nil {
			log.Fatal(err)
		}
		updates++
	}
	if err := s.Flush(); err != nil {
		log.Fatal(err)
	}
	return updates
}
