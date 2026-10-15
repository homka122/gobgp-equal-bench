// blaster is the upstream peer of the gobgpd under test. It builds every
// UPDATE itself, so it can send the same routes with the attributes or the
// communities in another order, which gobgpd as a peer never does.
//
// It reads commands from stdin, one per line:
//
//	load        announce all prefixes once, attributes in canonical order
//	storm R     re-announce all prefixes R more times according to -mode
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
	mode      = flag.String("mode", "same", "storm mode: same, change, reorder-comm, reorder-attrs")
	n         = flag.Int("n", 10000, "number of prefixes")
	perUpdate = flag.Int("per-update", 1, "prefixes per UPDATE")
	local     = flag.String("local", "127.0.0.1", "local address")
	remote    = flag.String("remote", "127.0.0.1:10179", "gobgpd address")
)

func main() {
	flag.Parse()
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
	nlris := make([]bgp.PathNLRI, *n)
	for i := range *n {
		p := netip.PrefixFrom(netip.AddrFrom4([4]byte{10, byte(i >> 8), byte(i), 0}), 24)
		var nlri bgp.NLRI
		if fam == bgp.RF_IPv4_VPN {
			nlri, err = bgp.NewLabeledVPNIPAddrPrefix(p, *bgp.NewMPLSLabelStack(label), bgp.NewRouteDistinguisherTwoOctetAS(65000, 1))
		} else {
			nlri, err = bgp.NewIPAddrPrefix(p)
		}
		if err != nil {
			log.Fatal(err)
		}
		nlris[i] = bgp.PathNLRI{NLRI: nlri}
	}

	out := json.NewEncoder(os.Stdout)
	out.Encode(map[string]any{"ready": true})
	round := 0
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		var cmd string
		var rounds int
		fmt.Sscan(in.Text(), &cmd, &rounds)
		start := time.Now()
		updates := 0
		switch cmd {
		case "load":
			updates = announce(s, fam, nlris, attrs(fam, "same", round))
			round++
		case "storm":
			for range rounds {
				updates += announce(s, fam, nlris, attrs(fam, *mode, round))
				round++
			}
		case "quit":
			s.Close()
			return
		default:
			log.Fatalf("unknown command %q", strings.TrimSpace(in.Text()))
		}
		out.Encode(map[string]any{"cmd": cmd, "updates": updates, "seconds": time.Since(start).Seconds()})
	}
}

// attrs returns the path attributes, without the NLRI, for a round. The
// canonical encoding is sorted by type code with sorted communities.
func attrs(fam bgp.Family, mode string, round int) []bgp.PathAttributeInterface {
	comms := []uint32{100<<16 | 1, 100<<16 | 2, 200<<16 | 50}
	if mode == "change" {
		comms[2] = 200<<16 | uint32(50+round)
	}
	if mode == "reorder-comm" && round%2 == 1 {
		slices.Reverse(comms)
	}
	a := []bgp.PathAttributeInterface{
		bgp.NewPathAttributeOrigin(bgp.BGP_ORIGIN_ATTR_TYPE_IGP),
		bgp.NewPathAttributeAsPath([]bgp.AsPathParamInterface{
			bgp.NewAs4PathParam(bgp.BGP_ASPATH_ATTR_TYPE_SEQ, []uint32{localAS, 64512}),
		}),
	}
	if fam == bgp.RF_IPv4_UC {
		nh, _ := bgp.NewPathAttributeNextHop(netip.MustParseAddr("127.0.0.1"))
		a = append(a, nh)
	}
	a = append(a, bgp.NewPathAttributeMultiExitDisc(0), bgp.NewPathAttributeCommunities(comms))
	if mode == "reorder-attrs" && round%2 == 1 {
		// COMMUNITIES first, the rest in the same order
		a = append(a[len(a)-1:], a[:len(a)-1]...)
	}
	return a
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
			m = bgp.NewBGPUpdateMessage(nil, append(slices.Clone(a), mp), nil)
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
