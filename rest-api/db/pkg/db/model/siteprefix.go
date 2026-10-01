// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package model

import (
	"encoding/json"
	"time"
)

const (
	// SitePrefixCreate, SitePrefixUpdate, and SitePrefixRetire identify recoverable root mutations.
	SitePrefixCreate = "Create"
	SitePrefixUpdate = "Update"
	SitePrefixRetire = "Retire"
)

// SitePrefixOperation retains the exact mutation whose Core result is still unknown.
// It is recovered only by a client retry, reattaching to a running workflow or replaying a closed attempt with the same Core identity.
type SitePrefixOperation struct {
	Kind        string          `json:"kind"`
	WorkflowID  string          `json:"workflowId"`
	CreatedAt   time.Time       `json:"createdAt"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Ambiguous   bool            `json:"ambiguous"`
	Version     string          `json:"version"`
	Metadata    json.RawMessage `json:"metadata"`
}

// SitePrefixQuota is Core's point-in-time quota for this tenant at the Site.
type SitePrefixQuota struct {
	Used  uint32 `json:"used"`
	Limit uint32 `json:"limit"`
}

// SitePrefixState separates REST mutation recovery from observed Core lifecycle.
// Inventory must never settle an outstanding operation or clear retirement intent.
type SitePrefixState struct {
	Operation           *SitePrefixOperation `json:"operation"`
	CreateSettled       bool                 `json:"createSettled"`
	RetirementSettledAt *time.Time           `json:"retirementSettledAt"`
	CoreStatus          *string              `json:"coreStatus"`
	Quota               *SitePrefixQuota     `json:"quota"`
	RetryMessage        *string              `json:"retryMessage"`
}
