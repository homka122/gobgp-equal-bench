package main

import (
	"fmt"
	"math/rand/v2"
	"net/netip"
	"slices"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"
)

// What a storm round sends for a route that is already announced:
//
//	same                  the same bytes
//	changed               another MED every round: the route really changes
//	standard-reversed     the COMMUNITIES values in reverse order
//	extended-reversed     the extended community values in reverse order
//	large-reversed        the large community values in reverse order
//	attributes-reordered  the COMMUNITIES attribute before the other attributes
//
// The last four keep the route the same and change only its encoding. They
// apply to every other round, the load included, so that each round differs
// from the one before.
var resends = []string{"same", "changed", "standard-reversed", "extended-reversed", "large-reversed", "attributes-reordered"}

// communitySet is the community values every route carries: distinct values
// (COMMUNITIES 65000:x, route targets 65000:x, large 65000:1:x) in the order
// asked for. A random order comes from the seed and is never sorted by
// chance.
type communitySet struct {
	standard []uint32
	extended []bgp.ExtendedCommunityInterface
	large    []*bgp.LargeCommunity
}

func newCommunitySet(nStandard, nExtended, nLarge int, order string, seed uint64) (*communitySet, error) {
	c := &communitySet{}
	for i := range nStandard {
		c.standard = append(c.standard, 65000<<16|uint32(100+10*i))
	}
	for i := range nExtended {
		c.extended = append(c.extended, bgp.NewTwoOctetAsSpecificExtended(bgp.EC_SUBTYPE_ROUTE_TARGET, 65000, uint32(100+10*i), true))
	}
	for i := range nLarge {
		c.large = append(c.large, bgp.NewLargeCommunity(65000, 1, uint32(100+10*i)))
	}
	switch order {
	case "sorted":
	case "reversed":
		slices.Reverse(c.standard)
		slices.Reverse(c.extended)
		slices.Reverse(c.large)
	case "random":
		r := rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))
		shuffle(r, c.standard, slices.IsSorted[[]uint32])
		shuffle(r, c.extended, func(s []bgp.ExtendedCommunityInterface) bool {
			return slices.IsSortedFunc(s, func(a, b bgp.ExtendedCommunityInterface) int {
				return int(a.(*bgp.TwoOctetAsSpecificExtended).LocalAdmin) - int(b.(*bgp.TwoOctetAsSpecificExtended).LocalAdmin)
			})
		})
		shuffle(r, c.large, func(s []*bgp.LargeCommunity) bool {
			return slices.IsSortedFunc(s, func(a, b *bgp.LargeCommunity) int { return int(a.LocalData2) - int(b.LocalData2) })
		})
	default:
		return nil, fmt.Errorf("unknown order %q", order)
	}
	return c, nil
}

func shuffle[T any](r *rand.Rand, s []T, sorted func([]T) bool) {
	if len(s) < 2 {
		return
	}
	for {
		r.Shuffle(len(s), func(i, j int) { s[i], s[j] = s[j], s[i] })
		if !sorted(s) {
			return
		}
	}
}

// supports tells whether the set has the values the resend needs.
func (c *communitySet) supports(resend string) error {
	need := map[string]int{
		"same": 0, "changed": 0, "attributes-reordered": 1 - len(c.standard),
		"standard-reversed": 2 - len(c.standard), "extended-reversed": 2 - len(c.extended), "large-reversed": 2 - len(c.large),
	}
	missing, ok := need[resend]
	if !ok {
		return fmt.Errorf("unknown resend %q", resend)
	}
	if missing > 0 {
		return fmt.Errorf("resend %s needs more community values of that kind", resend)
	}
	return nil
}

func reversed[S ~[]E, E any](s S) S {
	r := slices.Clone(s)
	slices.Reverse(r)
	return r
}

// attrs returns the path attributes of a round, without the NLRI; round 0 is
// the load.
func (c *communitySet) attrs(fam bgp.Family, resend string, round int) []bgp.PathAttributeInterface {
	other := round%2 == 0
	med := uint32(0)
	if resend == "changed" {
		med = uint32(round)
	}
	standard, extended, large := c.standard, c.extended, c.large
	if other {
		switch resend {
		case "standard-reversed":
			standard = reversed(standard)
		case "extended-reversed":
			extended = reversed(extended)
		case "large-reversed":
			large = reversed(large)
		}
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
	a = append(a, bgp.NewPathAttributeMultiExitDisc(med))
	if len(standard) > 0 {
		a = append(a, bgp.NewPathAttributeCommunities(standard))
	}
	if len(extended) > 0 {
		a = append(a, bgp.NewPathAttributeExtendedCommunities(extended))
	}
	if len(large) > 0 {
		a = append(a, bgp.NewPathAttributeLargeCommunities(large))
	}
	if resend == "attributes-reordered" && other {
		i := slices.IndexFunc(a, func(x bgp.PathAttributeInterface) bool { return x.GetType() == bgp.BGP_ATTR_TYPE_COMMUNITIES })
		a = append([]bgp.PathAttributeInterface{a[i]}, slices.Delete(slices.Clone(a), i, i+1)...)
	}
	return a
}
