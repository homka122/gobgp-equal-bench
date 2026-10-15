package table

// Copied into internal/pkg/table of every variant by harness/micro.py. Uses
// only the API of the base commit so that the same file builds everywhere.

import (
	"fmt"
	"net/netip"
	"slices"
	"testing"
	"time"

	"github.com/osrg/gobgp/v4/pkg/config/oc"
	"github.com/osrg/gobgp/v4/pkg/packet/bgp"
)

var standComms = []uint32{100<<16 | 1, 100<<16 | 2, 200<<16 | 50}

// standMsg is an UPDATE for prefix i as a peer sends it.
func standMsg(fam bgp.Family, i int, comms []uint32) *bgp.BGPMessage {
	p := netip.PrefixFrom(netip.AddrFrom4([4]byte{10, byte(i >> 8), byte(i), 0}), 24)
	attrs := []bgp.PathAttributeInterface{
		bgp.NewPathAttributeOrigin(bgp.BGP_ORIGIN_ATTR_TYPE_IGP),
		bgp.NewPathAttributeAsPath([]bgp.AsPathParamInterface{
			bgp.NewAs4PathParam(bgp.BGP_ASPATH_ATTR_TYPE_SEQ, []uint32{100, 64512}),
		}),
	}
	if fam == bgp.RF_IPv4_UC {
		nh, _ := bgp.NewPathAttributeNextHop(netip.MustParseAddr("10.0.0.2"))
		attrs = append(attrs, nh)
	}
	attrs = append(attrs, bgp.NewPathAttributeMultiExitDisc(0), bgp.NewPathAttributeCommunities(comms))
	if fam == bgp.RF_IPv4_UC {
		nlri, _ := bgp.NewIPAddrPrefix(p)
		return bgp.NewBGPUpdateMessage(nil, attrs, []bgp.PathNLRI{{NLRI: nlri}})
	}
	nlri, _ := bgp.NewLabeledVPNIPAddrPrefix(p, *bgp.NewMPLSLabelStack(100), bgp.NewRouteDistinguisherTwoOctetAS(65000, 1))
	mp, _ := bgp.NewPathAttributeMpReachNLRI(fam, []bgp.PathNLRI{{NLRI: nlri}}, netip.MustParseAddr("10.0.0.2"))
	return bgp.NewBGPUpdateMessage(nil, append(attrs, mp), nil)
}

var standFamilies = []struct {
	name string
	fam  bgp.Family
}{{"ipv4", bgp.RF_IPv4_UC}, {"vpnv4", bgp.RF_IPv4_VPN}}

// Path.Equal on two received copies of the same route, as AdjRib and the
// table compare a re-announced route with the one they hold.
func BenchmarkStandEqual(b *testing.B) {
	for _, f := range standFamilies {
		b.Run(f.name, func(b *testing.B) {
			peer := peerR2()
			lhs := ProcessMessage(standMsg(f.fam, 1, standComms), peer, time.Now(), false)[0]
			rhs := ProcessMessage(standMsg(f.fam, 1, standComms), peer, time.Now(), false)[0]
			if !lhs.Equal(rhs) {
				b.Fatal("paths differ")
			}
			b.ReportAllocs()
			for range b.N {
				_ = lhs.Equal(rhs)
			}
		})
	}
}

// Re-announcements of routes already in the table:
// UPDATE -> ProcessMessage -> AdjRib -> TableManager.
func BenchmarkStandStorm(b *testing.B) {
	const prefixes = 10_000
	reversed := slices.Clone(standComms)
	slices.Reverse(reversed)
	for _, f := range standFamilies {
		for _, mode := range []string{"same", "reorder-comm"} {
			b.Run(fmt.Sprintf("%s/%s", f.name, mode), func(b *testing.B) {
				peer := peerR2()
				rf := []bgp.Family{f.fam}
				adj := NewAdjRib(logger, rf)
				tm := NewTableManager(logger, rf, oc.RouteSelectionOptionsConfig{}, oc.UseMultiplePathsConfig{})
				msgs := make([]*bgp.BGPMessage, prefixes)
				alt := make([]*bgp.BGPMessage, prefixes)
				for i := range prefixes {
					msgs[i] = standMsg(f.fam, i, standComms)
					alt[i] = standMsg(f.fam, i, reversed)
				}
				changed := 0
				process := func(m *bgp.BGPMessage) {
					paths := ProcessMessage(m, peer, time.Now(), false)
					adj.Update(paths)
					for _, p := range paths {
						for _, u := range tm.Update(p) {
							if u.Changed {
								changed++
							}
						}
					}
				}
				for _, m := range msgs {
					process(m)
				}
				changed = 0
				b.ReportAllocs()
				b.ResetTimer()
				for i := range b.N {
					m := msgs[i%prefixes]
					if mode == "reorder-comm" && (i/prefixes)%2 == 1 {
						m = alt[i%prefixes]
					}
					process(m)
				}
				b.ReportMetric(float64(changed)/float64(b.N), "changed/op")
			})
		}
	}
}
