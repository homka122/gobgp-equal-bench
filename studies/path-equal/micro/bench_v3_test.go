package table

// The gobgp v3 version of bench_v4_test.go: copied into internal/pkg/table of
// a v3 tree by micro.py.

import (
	"fmt"
	"net"
	"slices"
	"testing"
	"time"

	"github.com/osrg/gobgp/v3/pkg/packet/bgp"
)

var standComms = []uint32{100<<16 | 1, 100<<16 | 2, 200<<16 | 50}

func standExts() []bgp.ExtendedCommunityInterface {
	return []bgp.ExtendedCommunityInterface{
		bgp.NewTwoOctetAsSpecificExtended(bgp.EC_SUBTYPE_ROUTE_TARGET, 100, 1, true),
		bgp.NewTwoOctetAsSpecificExtended(bgp.EC_SUBTYPE_ROUTE_TARGET, 100, 2, true),
		bgp.NewTwoOctetAsSpecificExtended(bgp.EC_SUBTYPE_ROUTE_TARGET, 200, 50, true),
	}
}

func standLarge() []*bgp.LargeCommunity {
	return []*bgp.LargeCommunity{
		bgp.NewLargeCommunity(100, 1, 1),
		bgp.NewLargeCommunity(100, 1, 2),
		bgp.NewLargeCommunity(200, 50, 1),
	}
}

func reversed[S ~[]E, E any](s S) S {
	r := slices.Clone(s)
	slices.Reverse(r)
	return r
}

// standAttrs are the communities of a stand UPDATE. exts == nil means no
// EXTENDED_COMMUNITIES, except on VPN routes, which always carry the route
// targets as the blaster sends them.
type standAttrs struct {
	comms []uint32
	exts  []bgp.ExtendedCommunityInterface
	large []*bgp.LargeCommunity
}

// standMsg is an UPDATE for prefix i as a peer sends it.
func standMsg(fam bgp.RouteFamily, i int, s standAttrs) *bgp.BGPMessage {
	prefix := fmt.Sprintf("10.%d.%d.0", byte(i>>8), byte(i))
	attrs := []bgp.PathAttributeInterface{
		bgp.NewPathAttributeOrigin(bgp.BGP_ORIGIN_ATTR_TYPE_IGP),
		bgp.NewPathAttributeAsPath([]bgp.AsPathParamInterface{
			bgp.NewAs4PathParam(bgp.BGP_ASPATH_ATTR_TYPE_SEQ, []uint32{100, 64512}),
		}),
	}
	if fam == bgp.RF_IPv4_UC {
		attrs = append(attrs, bgp.NewPathAttributeNextHop("10.0.0.2"))
	}
	attrs = append(attrs, bgp.NewPathAttributeMultiExitDisc(0), bgp.NewPathAttributeCommunities(slices.Clone(s.comms)))
	if fam == bgp.RF_IPv4_UC {
		if s.exts != nil {
			attrs = append(attrs, bgp.NewPathAttributeExtendedCommunities(slices.Clone(s.exts)))
		}
		if s.large != nil {
			attrs = append(attrs, bgp.NewPathAttributeLargeCommunities(slices.Clone(s.large)))
		}
		return bgp.NewBGPUpdateMessage(nil, attrs, []*bgp.IPAddrPrefix{bgp.NewIPAddrPrefix(24, prefix)})
	}
	nlri := bgp.NewLabeledVPNIPAddrPrefix(24, prefix, *bgp.NewMPLSLabelStack(100), bgp.NewRouteDistinguisherTwoOctetAS(65000, 1))
	mp := bgp.NewPathAttributeMpReachNLRI("10.0.0.2", []bgp.AddrPrefixInterface{nlri})
	exts := slices.Clone(s.exts)
	if exts == nil {
		exts = standExts()
	}
	attrs = append(attrs, mp, bgp.NewPathAttributeExtendedCommunities(exts))
	if s.large != nil {
		attrs = append(attrs, bgp.NewPathAttributeLargeCommunities(slices.Clone(s.large)))
	}
	return bgp.NewBGPUpdateMessage(nil, attrs, nil)
}

// restore puts the community values of m back in the order of s: v3final
// sorts them in place, in the message. Every variant pays for it.
func restore(m *bgp.BGPMessage, s standAttrs) {
	for _, a := range m.Body.(*bgp.BGPUpdate).PathAttributes {
		switch a := a.(type) {
		case *bgp.PathAttributeCommunities:
			copy(a.Value, s.comms)
		case *bgp.PathAttributeExtendedCommunities:
			if s.exts != nil {
				copy(a.Value, s.exts)
			}
		case *bgp.PathAttributeLargeCommunities:
			copy(a.Values, s.large)
		}
	}
}

var standFamilies = []struct {
	name string
	fam  bgp.RouteFamily
}{{"ipv4", bgp.RF_IPv4_UC}, {"vpnv4", bgp.RF_IPv4_VPN}}

// standModes are the canonical and the reordered communities of the storm
// modes; the reordered form is sent every other round.
var standModes = []struct {
	name               string
	canonical, reorder standAttrs
}{
	{"same", standAttrs{comms: standComms}, standAttrs{comms: standComms}},
	{"reorder-comm", standAttrs{comms: standComms}, standAttrs{comms: reversed(standComms)}},
	{"reorder-ext", standAttrs{comms: standComms, exts: standExts()}, standAttrs{comms: standComms, exts: reversed(standExts())}},
	{"reorder-large", standAttrs{comms: standComms, large: standLarge()}, standAttrs{comms: standComms, large: reversed(standLarge())}},
}

// Path.Equal on two received copies of the same route, as AdjRib and the
// table compare a re-announced route with the one they hold, and on two
// exported clones (a changed attribute drops the cached hash in v3final).
// "fresh" drops the hash of one side before every call: the first
// comparison of a newly exported clone.
func BenchmarkStandEqual(b *testing.B) {
	peer := peerR2()
	received := func(fam bgp.RouteFamily) func() *Path {
		return func() *Path {
			return ProcessMessage(standMsg(fam, 1, standAttrs{comms: standComms}), peer, time.Now())[0]
		}
	}
	exported := func() *Path {
		p := received(bgp.RF_IPv4_UC)().Clone(false)
		p.SetNexthop(net.ParseIP("10.0.0.1").To4())
		return p
	}
	for _, c := range []struct {
		name  string
		path  func() *Path
		fresh bool
	}{
		{"ipv4", received(bgp.RF_IPv4_UC), false},
		{"vpnv4", received(bgp.RF_IPv4_VPN), false},
		{"ipv4-exported", exported, false},
		{"ipv4-exported-fresh", exported, true},
	} {
		b.Run(c.name, func(b *testing.B) {
			lhs, rhs := c.path(), c.path()
			if !lhs.Equal(rhs) {
				b.Fatal("paths differ")
			}
			b.ReportAllocs()
			for range b.N {
				if c.fresh {
					rhs.SetHash(0)
				}
				_ = lhs.Equal(rhs)
			}
		})
	}
}

// ProcessMessage on an UPDATE with one prefix, the worst case for the order
// fix: it sorts once per UPDATE. This is also the cost of the fix on the
// initial load.
func BenchmarkStandProcessMessage(b *testing.B) {
	for _, f := range standFamilies {
		for _, c := range []struct {
			name string
			s    standAttrs
		}{
			{"canonical", standAttrs{comms: standComms}},
			{"comm-unsorted", standAttrs{comms: reversed(standComms)}},
			{"ext-sorted", standAttrs{comms: standComms, exts: standExts()}},
			{"ext-unsorted", standAttrs{comms: standComms, exts: reversed(standExts())}},
			{"large-sorted", standAttrs{comms: standComms, large: standLarge()}},
			{"large-unsorted", standAttrs{comms: standComms, large: reversed(standLarge())}},
		} {
			b.Run(f.name+"/"+c.name, func(b *testing.B) {
				msg := standMsg(f.fam, 1, c.s)
				peer := peerR2()
				now := time.Now()
				b.ReportAllocs()
				for range b.N {
					restore(msg, c.s)
					_ = ProcessMessage(msg, peer, now)
				}
			})
		}
	}
}

// Re-announcements of routes already in the table:
// UPDATE -> ProcessMessage -> AdjRib -> TableManager. changed/op is the share
// of UPDATEs gobgpd would send on: the server sends a route to the peers when
// GetChanges returns a best path.
func BenchmarkStandStorm(b *testing.B) {
	const prefixes = 10_000
	for _, f := range standFamilies {
		for _, mode := range standModes {
			b.Run(fmt.Sprintf("%s/%s", f.name, mode.name), func(b *testing.B) {
				peer := peerR2()
				rf := []bgp.RouteFamily{f.fam}
				adj := NewAdjRib(logger, rf)
				tm := NewTableManager(logger, rf)
				msgs := make([]*bgp.BGPMessage, prefixes)
				alt := make([]*bgp.BGPMessage, prefixes)
				for i := range prefixes {
					msgs[i] = standMsg(f.fam, i, mode.canonical)
					alt[i] = standMsg(f.fam, i, mode.reorder)
				}
				changed := 0
				process := func(m *bgp.BGPMessage) {
					paths := ProcessMessage(m, peer, time.Now())
					adj.Update(paths)
					for _, p := range paths {
						for _, u := range tm.Update(p) {
							if best, _, _ := u.GetChanges(GLOBAL_RIB_NAME, 0, false); best != nil {
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
					m, s := msgs[i%prefixes], mode.canonical
					if (i/prefixes)%2 == 1 {
						m, s = alt[i%prefixes], mode.reorder
					}
					restore(m, s)
					process(m)
				}
				b.ReportMetric(float64(changed)/float64(b.N), "changed/op")
			})
		}
	}
}
