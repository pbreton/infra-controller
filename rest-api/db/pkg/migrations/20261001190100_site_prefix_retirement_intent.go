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
		// Accepted retirement survives independently of subsequently reported Core state.
		_, err := db.ExecContext(ctx, `ALTER TABLE ip_block
			ADD COLUMN IF NOT EXISTS site_prefix_retirement_requested_at TIMESTAMPTZ`)
		return err
	}, func(_ context.Context, _ *bun.DB) error {
		return errors.New("cannot discard accepted SitePrefix retirement intent")
	})
}
