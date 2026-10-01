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
		// Retain mutation identity across interrupted REST requests and inventory updates.
		_, err := db.ExecContext(ctx, `ALTER TABLE ip_block ADD COLUMN IF NOT EXISTS site_prefix_state JSONB`)
		return err
	}, func(_ context.Context, _ *bun.DB) error {
		return errors.New("cannot discard SitePrefix mutation recovery state")
	})
}
