// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package migrations

import (
	"context"
	"testing"

	authz "github.com/NVIDIA/infra-controller/rest-api/auth/pkg/authorization"
	cutil "github.com/NVIDIA/infra-controller/rest-api/common/pkg/util"
	"github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/model"
	"github.com/NVIDIA/infra-controller/rest-api/db/pkg/util"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/uptrace/bun/migrate"
)

func TestTenantSitePrefixIdentityMigration(t *testing.T) {
	ctx := context.Background()
	session := util.GetTestDBSession(t, true)
	t.Cleanup(session.Close)
	model.TestSetupSchema(t, session)
	user := model.TestBuildUser(t, session, uuid.NewString(), "provider", []string{authz.ProviderAdminRole})
	provider := model.TestBuildInfrastructureProvider(t, session, "provider", "provider", user)
	site := model.TestBuildSite(t, session, provider, "site", user)
	tenant := model.TestBuildTenant(t, session, "tenant", "tenant", user)
	otherTenant := model.TestBuildTenant(t, session, "other", "other", user)
	otherSite := model.TestBuildSite(t, session, provider, "other-site", user)
	dao := model.NewIPBlockDAO(session)
	create := func(siteID uuid.UUID, tenantID, coreID *uuid.UUID, prefix string, bits int) (*model.IPBlock, error) {
		return dao.Create(ctx, nil, model.IPBlockCreateInput{
			Name: uuid.NewString(), SiteID: siteID, InfrastructureProviderID: provider.ID,
			TenantID: tenantID, SitePrefixID: coreID, Prefix: prefix, PrefixLength: bits,
			ProtocolVersion: model.IPBlockProtocolVersionV4, RoutingType: model.IPBlockRoutingTypeDatacenterOnly,
			Status: model.IPBlockStatusProvisioning, CreatedBy: &user.ID,
		})
	}
	original, err := create(site.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 24)
	require.NoError(t, err)
	// Populate the predecessor schema with both kinds of legacy IP Blocks.
	_, err = create(site.ID, nil, nil, "10.0.0.0", 24)
	require.NoError(t, err)
	_, err = create(site.ID, &tenant.ID, nil, "10.0.0.0", 24)
	require.NoError(t, err)

	target := migrate.NewMigrations()
	for _, migration := range Migrations.Sorted() {
		if migration.Name == "20261001190000" {
			target.Add(migration)
		}
	}
	require.Len(t, target.Sorted(), 1)
	migrator := migrate.NewMigrator(session.DB, target,
		migrate.WithTableName("tenant_site_prefix_identity_migrations_test"),
		migrate.WithLocksTableName("tenant_site_prefix_identity_locks_test"),
		migrate.WithMarkAppliedOnSuccess(true))
	require.NoError(t, migrator.Init(ctx))
	_, err = migrator.Migrate(ctx)
	require.NoError(t, err)

	cases := []struct {
		name             string
		siteID           uuid.UUID
		tenantID, coreID *uuid.UUID
		prefix           string
		bits             int
		conflict         bool
	}{
		{"duplicate active identity", site.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 24, true},
		{"canonical identity", site.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.1", 24, true},
		{"different tenant", site.ID, &otherTenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 24, false},
		{"different site", otherSite.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 24, false},
		{"different prefix length", site.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 25, false},
		{"provider root", site.ID, nil, cutil.GetPtr(uuid.New()), "10.0.0.0", 24, false},
		{"allocation", site.ID, &tenant.ID, nil, "10.0.0.0", 24, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := create(tc.siteID, tc.tenantID, tc.coreID, tc.prefix, tc.bits)
			if tc.conflict {
				require.ErrorContains(t, err, "ip_block_tenant_site_prefix_cidr_key")
			} else {
				require.NoError(t, err)
			}
		})
	}
	require.NoError(t, dao.Delete(ctx, nil, original.ID))
	_, err = create(site.ID, &tenant.ID, cutil.GetPtr(uuid.New()), "10.0.0.0", 24)
	require.NoError(t, err)
}
