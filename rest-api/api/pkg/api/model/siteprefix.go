// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package model

import (
	"errors"
	"net/netip"
	"time"

	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/model/util"
	cdbm "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/model"
	validation "github.com/go-ozzo/ozzo-validation/v4"
	validationis "github.com/go-ozzo/ozzo-validation/v4/is"
)

// APISitePrefixCreateRequest requests private address space at an attached Site.
type APISitePrefixCreateRequest struct {
	Name        string  `json:"name"`
	Description *string `json:"description"`
	SiteID      string  `json:"siteId"`
	Prefix      string  `json:"prefix"`
}

// Validate checks metadata and requires a network-aligned RFC1918 IPv4 /8 through /31.
func (r APISitePrefixCreateRequest) Validate() error {
	return validation.ValidateStruct(&r,
		validation.Field(&r.Name, validation.Required, validation.Length(2, 256), validationis.ASCII, validation.By(util.ValidateNameCharacters)),
		validation.Field(&r.Description, validation.Length(0, 1024)),
		validation.Field(&r.SiteID, validation.Required, validationis.UUID),
		validation.Field(&r.Prefix, validation.Required, validation.By(func(value any) error {
			p, err := netip.ParsePrefix(value.(string))
			if err == nil && p.Addr().Is4() && p == p.Masked() && p.Bits() >= 8 && p.Bits() <= 31 {
				for _, cidr := range []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} {
					root := netip.MustParsePrefix(cidr)
					if root.Bits() <= p.Bits() && root.Contains(p.Addr()) {
						return nil
					}
				}
			}
			return errors.New("must be a network-aligned RFC1918 IPv4 CIDR with prefix length /8 through /31")
		})),
	)
}

// APISitePrefixUpdateRequest changes metadata only; omission or null preserves it.
type APISitePrefixUpdateRequest struct {
	Name        *string `json:"name"`
	Description *string `json:"description"`
}

func (r APISitePrefixUpdateRequest) Validate() error {
	return validation.ValidateStruct(&r,
		validation.Field(&r.Name, validation.When(r.Name != nil, validation.Required, validation.Length(2, 256), validationis.ASCII, validation.By(util.ValidateNameCharacters))),
		validation.Field(&r.Description, validation.Length(0, 1024)))
}

// APISitePrefixQuota is the latest Core-reported quota for the owning tenant at this Site.
type APISitePrefixQuota struct {
	Used  uint32 `json:"used"`
	Limit uint32 `json:"limit"`
}

// APISitePrefix is tenant-private lifecycle state. Core inventory is authoritative for readiness.
type APISitePrefix struct {
	ID                    string              `json:"id"`
	Name                  string              `json:"name"`
	Description           *string             `json:"description"`
	SiteID                string              `json:"siteId"`
	TenantID              string              `json:"tenantId"`
	Prefix                string              `json:"prefix"`
	Authority             string              `json:"authority"`
	ProtocolVersion       string              `json:"protocolVersion"`
	RoutingType           string              `json:"routingType"`
	Status                string              `json:"status"`
	CoreStatus            *string             `json:"coreStatus"`
	RetryMessage          *string             `json:"retryMessage"`
	RetirementRequestedAt *time.Time          `json:"retirementRequestedAt"`
	Quota                 *APISitePrefixQuota `json:"quota"`
	VpcPrefixIDs          []string            `json:"vpcPrefixIds"`
	StatusHistory         []APIStatusDetail   `json:"statusHistory"`
	Created               time.Time           `json:"created"`
	Updated               time.Time           `json:"updated"`
}

func (r *APISitePrefix) FromDBModel(block *cdbm.IPBlock) {
	*r = APISitePrefix{ID: block.ID.String(), Name: block.Name, Description: block.Description,
		SiteID: block.SiteID.String(), TenantID: block.TenantID.String(),
		Prefix:    netip.PrefixFrom(netip.MustParseAddr(block.Prefix), block.PrefixLength).String(),
		Authority: "TenantManaged", ProtocolVersion: cdbm.IPBlockProtocolVersionV4, RoutingType: cdbm.IPBlockRoutingTypeDatacenterOnly,
		Status: block.Status, RetirementRequestedAt: block.SitePrefixRetirementRequestedAt,
		VpcPrefixIDs: []string{}, StatusHistory: []APIStatusDetail{}, Created: block.Created, Updated: block.Updated}
	if block.SitePrefixState != nil {
		r.CoreStatus = block.SitePrefixState.CoreStatus
		{
			q := block.SitePrefixState.Quota
			if q != nil {
				r.Quota = &APISitePrefixQuota{Used: q.Used, Limit: q.Limit}
			}
		}
		r.RetryMessage = block.SitePrefixState.RetryMessage
	}
}
