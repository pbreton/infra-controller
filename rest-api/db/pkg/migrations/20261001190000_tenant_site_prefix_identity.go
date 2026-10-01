// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package migrations

import (
	"context"
	"errors"

	"github.com/uptrace/bun"
)

func init() {
	Migrations.MustRegister(func(ctx context.Context, db *bun.DB) error {
		// Create retries must find the same active tenant root, independently of metadata.
		_, err := db.ExecContext(ctx, `CREATE UNIQUE INDEX IF NOT EXISTS ip_block_tenant_site_prefix_cidr_key
			ON ip_block (tenant_id, site_id, network(set_masklen(prefix::inet, prefix_length::integer)))
			WHERE tenant_id IS NOT NULL AND site_prefix_id IS NOT NULL AND deleted IS NULL`)
		return err
	}, func(_ context.Context, _ *bun.DB) error {
		return errors.New("cannot discard active tenant SitePrefix identity uniqueness")
	})
}
