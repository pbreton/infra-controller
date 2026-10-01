// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
package model

import (
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
)

func TestAPISitePrefixCreateRequest_Validate(t *testing.T) {
	for _, tc := range []struct {
		prefix string
		valid  bool
	}{
		{"10.0.0.0/8", true}, {"172.16.0.0/12", true}, {"192.168.1.0/31", true},
		{"10.0.0.1/24", false}, {"172.0.0.0/8", false}, {"192.168.0.1/32", false}, {"100.64.0.0/10", false}, {"8.8.8.0/24", false}, {"fd00::/64", false}, {"::ffff:10.0.0.0/104", false},
	} {
		t.Run(tc.prefix, func(t *testing.T) {
			r := APISitePrefixCreateRequest{Name: "root", SiteID: uuid.NewString(), Prefix: tc.prefix}
			err := r.Validate()
			if tc.valid {
				require.NoError(t, err)
			} else {
				require.Error(t, err)
			}
		})
	}
}
