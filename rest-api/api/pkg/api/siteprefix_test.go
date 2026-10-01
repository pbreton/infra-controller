// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
	temporalEnums "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	tclient "go.temporal.io/sdk/client"
	tp "go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/handler/util/common"
	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/model"
	sc "github.com/NVIDIA/infra-controller/rest-api/api/pkg/client/site"
	auth "github.com/NVIDIA/infra-controller/rest-api/auth/pkg/authorization"
	"github.com/NVIDIA/infra-controller/rest-api/common/pkg/grpcproxy"
	cutil "github.com/NVIDIA/infra-controller/rest-api/common/pkg/util"
	cdb "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db"
	cdbm "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/model"
	cdbp "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/paginator"
	corev1 "github.com/NVIDIA/infra-controller/rest-api/proto/core/gen/v1"
	sdk "github.com/NVIDIA/infra-controller/rest-api/sdk/standard"
	swe "github.com/NVIDIA/infra-controller/rest-api/site-workflow/pkg/error"
)

type sitePrefixTestClient struct {
	tclient.Client
	call func(context.Context, tclient.StartWorkflowOptions, grpcproxy.Request) (tclient.WorkflowRun, error)
}

func (c *sitePrefixTestClient) ExecuteWorkflow(ctx context.Context, options tclient.StartWorkflowOptions, workflow any, args ...any) (tclient.WorkflowRun, error) {
	return c.call(ctx, options, args[0].(grpcproxy.Request))
}

func (c *sitePrefixTestClient) GetWorkflow(context.Context, string, string) tclient.WorkflowRun {
	return sitePrefixTestRun{err: serviceerror.NewNotFound("workflow history expired")}
}

type sitePrefixTestRun struct {
	tclient.WorkflowRun
	data []byte
	err  error
}

func (r sitePrefixTestRun) Get(_ context.Context, value any) error {
	value.(*grpcproxy.Response).ResponseJSON = r.data
	return r.err
}

type sitePrefixFixture struct {
	session              *cdb.Session
	site                 *cdbm.Site
	tenant               *cdbm.Tenant
	user                 *cdbm.User
	router               *echo.Echo
	pool                 *sc.ClientPool
	calls                []grpcproxy.Request
	workflowIDs          []string
	nextError            error
	nextErrorAfterCommit error
	completed            map[string]sitePrefixTestRun
	core                 map[string]*corev1.SitePrefix
}

func newSitePrefixFixture(t *testing.T) *sitePrefixFixture {
	t.Helper()
	f := &sitePrefixFixture{session: common.TestInitDB(t), completed: map[string]sitePrefixTestRun{}, core: map[string]*corev1.SitePrefix{}}
	t.Cleanup(f.session.Close)
	common.TestSetupSchema(t, f.session)
	f.user = common.TestBuildUser(t, f.session, uuid.NewString(), "tenant", []string{auth.TenantAdminRole})
	providerUser := common.TestBuildUser(t, f.session, uuid.NewString(), "provider", []string{auth.ProviderAdminRole})
	provider := common.TestBuildInfrastructureProvider(t, f.session, "provider", "provider", providerUser)
	f.site = common.TestBuildSite(t, f.session, provider, "site", providerUser)
	var err error
	f.site, err = cdbm.NewSiteDAO(f.session).Update(context.Background(), nil, cdbm.SiteUpdateInput{SiteID: f.site.ID, Status: cutil.GetPtr(cdbm.SiteStatusRegistered), Config: &cdbm.SiteConfigUpdateInput{TenantSitePrefix: cutil.GetPtr(true)}})
	require.NoError(t, err)
	f.tenant = common.TestBuildTenant(t, f.session, "tenant", "tenant", f.user)
	common.TestBuildTenantSite(t, f.session, f.tenant, f.site, f.user)
	f.pool = sc.NewClientPool(nil)
	f.pool.IDClientMap[f.site.ID.String()] = &sitePrefixTestClient{call: func(ctx context.Context, options tclient.StartWorkflowOptions, request grpcproxy.Request) (tclient.WorkflowRun, error) {
		if request.FullMethod == "/forge.Forge/FindSitePrefixesByIds" {
			var ids corev1.SitePrefixesByIdsRequest
			require.NoError(t, protojson.Unmarshal(request.RequestJSON, &ids))
			result := &corev1.SitePrefixList{}
			for _, id := range ids.SitePrefixIds {
				{
					value := f.core[id.Value]
					if value != nil {
						result.SitePrefixes = append(result.SitePrefixes, value)
					}
				}
			}
			data, err := protojson.Marshal(result)
			require.NoError(t, err)
			return sitePrefixTestRun{data: data}, nil
		}

		require.Equal(t, temporalEnums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE, options.WorkflowIDReusePolicy)
		require.Equal(t, temporalEnums.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING, options.WorkflowIDConflictPolicy)
		require.Empty(t, request.EncryptedSecrets)
		f.calls = append(f.calls, request)
		f.workflowIDs = append(f.workflowIDs, options.ID)
		{
			result, ok := f.completed[options.ID]
			if ok {
				return result, nil
			}
		}
		if f.nextError != nil {
			err := f.nextError
			f.nextError = nil
			var appErr *tp.ApplicationError
			if errors.As(err, &appErr) {
				f.completed[options.ID] = sitePrefixTestRun{err: err}
			}
			return sitePrefixTestRun{err: err}, nil
		}
		var wire struct {
			ID       string `json:"id"`
			Tenant   string `json:"tenantOrganizationId"`
			Prefix   string `json:"prefix"`
			Metadata struct {
				Name        string `json:"name"`
				Description string `json:"description"`
			} `json:"metadata"`
		}
		// Protojson represents SitePrefixId as an object.
		var raw map[string]json.RawMessage
		require.NoError(t, json.Unmarshal(request.RequestJSON, &raw))
		var id struct {
			Value string `json:"value"`
		}
		require.NoError(t, json.Unmarshal(raw["id"], &id))
		delete(raw, "id")
		data, err := json.Marshal(raw)
		require.NoError(t, err)
		require.NoError(t, json.Unmarshal(data, &wire))
		require.Equal(t, f.tenant.Org, wire.Tenant)
		block, err := cdbm.NewIPBlockDAO(f.session).GetBySitePrefixID(ctx, nil, uuid.MustParse(id.Value))
		require.NoError(t, err, "durable row must be committed before Core is invoked")
		require.Equal(t, block.SitePrefixState.Operation.WorkflowID, options.ID)
		state := corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_PROVISIONING
		if request.FullMethod == "/forge.Forge/DeleteSitePrefix" {
			state = corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_DELETING
		}
		result := &corev1.SitePrefix{Version: fmt.Sprint(len(f.calls)), Id: &corev1.SitePrefixId{Value: id.Value}, Config: &corev1.SitePrefixConfig{Prefix: block.Prefix + "/24", TenantOrganizationId: &f.tenant.Org, RoutingScope: corev1.SitePrefixRoutingScope_SITE_PREFIX_ROUTING_SCOPE_DATACENTER_ONLY}, Metadata: &corev1.Metadata{Name: wire.Metadata.Name, Description: wire.Metadata.Description}, Status: &corev1.SitePrefixStatus{Authority: corev1.SitePrefixAuthority_SITE_PREFIX_AUTHORITY_TENANT_MANAGED, LifecycleState: state, Quota: &corev1.SitePrefixQuotaUsage{Used: 1, Limit: 8}}}
		if request.FullMethod == "/forge.Forge/UpdateSitePrefix" {
			var update corev1.SitePrefixUpdateRequest
			require.NoError(t, protojson.Unmarshal(request.RequestJSON, &update))
			require.NotNil(t, update.IfVersionMatch)
			old := f.core[id.Value]
			if update.GetIfVersionMatch() != old.Version {
				run := sitePrefixTestRun{err: tp.NewNonRetryableApplicationError("version changed", swe.ErrTypeNICoFailedPrecondition, nil)}
				f.completed[options.ID] = run
				return run, nil
			}
			result.Metadata = update.Metadata
		}
		f.core[id.Value] = result
		if f.nextErrorAfterCommit != nil {
			run := sitePrefixTestRun{err: f.nextErrorAfterCommit}
			f.nextErrorAfterCommit = nil
			f.completed[options.ID] = run
			return run, nil
		}

		if request.FullMethod == "/forge.Forge/DeleteSitePrefix" {
			data, err = protojson.Marshal(&corev1.SitePrefixDeletionResult{SitePrefix: result})
		} else {
			data, err = protojson.Marshal(result)
		}
		require.NoError(t, err)
		run := sitePrefixTestRun{data: data}
		f.completed[options.ID] = run
		return run, nil
	}}
	f.router = echo.New()
	f.router.Use(func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			if f.user != nil {
				c.Set("user", f.user)
			}
			return next(c)
		}
	})
	for _, route := range NewAPIRoutes(f.session, nil, nil, f.pool, common.GetTestConfig(), nil) {
		f.router.Add(route.Method, "/v2"+route.Path, route.Handler.Handle)
	}
	return f
}
func (f *sitePrefixFixture) request(t *testing.T, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var data []byte
	if body != nil {
		var err error
		data, err = json.Marshal(body)
		require.NoError(t, err)
	}
	req := httptest.NewRequest(method, "/v2/org/tenant/nico/site-prefix"+path, strings.NewReader(string(data)))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	f.router.ServeHTTP(rec, req)
	return rec
}
func (f *sitePrefixFixture) createBody(prefix string) model.APISitePrefixCreateRequest {
	return model.APISitePrefixCreateRequest{Name: "tenant-root", SiteID: f.site.ID.String(), Prefix: prefix}
}
func readSitePrefix(t *testing.T, rec *httptest.ResponseRecorder, code int) model.APISitePrefix {
	t.Helper()
	require.Equal(t, code, rec.Code, rec.Body.String())
	var result model.APISitePrefix
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &result))
	return result
}

func TestSitePrefixRoutes(t *testing.T) {
	cases := []struct {
		name  string
		check func(*testing.T, *sitePrefixFixture)
	}{
		{"generated SDK exercises all public operations and nullable responses", func(t *testing.T, f *sitePrefixFixture) {
			server := httptest.NewServer(f.router)
			defer server.Close()
			config := sdk.NewConfiguration()
			config.Servers = sdk.ServerConfigurations{{URL: server.URL}}
			client := sdk.NewAPIClient(config).SitePrefixAPI
			ctx := context.Background()
			created, response, err := client.CreateSitePrefix(ctx, "tenant").SitePrefixCreateRequest(*sdk.NewSitePrefixCreateRequest("sdk-root", f.site.ID.String(), "10.0.0.0/24")).Execute()
			require.NoError(t, err)
			require.Equal(t, http.StatusCreated, response.StatusCode)
			require.NotEmpty(t, response.Header.Get(echo.HeaderLocation))
			require.Nil(t, created.RetryMessage.Get())
			require.Empty(t, created.VpcPrefixIds)
			item, _, err := client.GetSitePrefix(ctx, "tenant", created.Id).Execute()
			require.NoError(t, err)
			require.Equal(t, created.Id, item.Id)
			list, response, err := client.GetAllSitePrefix(ctx, "tenant").SiteId(f.site.ID.String()).OrderBy("CREATED_ASC").PageSize(1).Execute()
			require.NoError(t, err)
			require.Len(t, list, 1)
			require.NotEmpty(t, response.Header.Get("X-Pagination"))
			update := sdk.NewSitePrefixUpdateRequest()
			update.SetName("sdk-updated")
			item, _, err = client.UpdateSitePrefix(ctx, "tenant", created.Id).SitePrefixUpdateRequest(*update).Execute()
			require.NoError(t, err)
			require.Equal(t, "sdk-updated", item.Name)
			item, response, err = client.DeleteSitePrefix(ctx, "tenant", created.Id).Execute()
			require.NoError(t, err)
			require.Equal(t, http.StatusAccepted, response.StatusCode)
			require.Equal(t, cdbm.IPBlockStatusDeleting, item.Status)
		}},

		{"authorization and attached Registered Site required", func(t *testing.T, f *sitePrefixFixture) {
			original := f.user
			f.user = common.TestBuildUser(t, f.session, uuid.NewString(), "tenant", []string{auth.ProviderAdminRole})
			require.Equal(t, 403, f.request(t, http.MethodGet, "", nil).Code)
			f.user = nil
			require.Equal(t, 500, f.request(t, http.MethodGet, "", nil).Code)
			f.user = original
			body := f.createBody("10.0.0.0/24")
			body.SiteID = uuid.NewString()
			require.Equal(t, 400, f.request(t, http.MethodPost, "", body).Code)
			_, err := cdbm.NewSiteDAO(f.session).Update(context.Background(), nil, cdbm.SiteUpdateInput{SiteID: f.site.ID, Status: cutil.GetPtr(cdbm.SiteStatusPending)})
			require.NoError(t, err)
			require.Equal(t, 400, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")).Code)
			require.Empty(t, f.calls)
		}},
		{"capability defaults off and tenant cannot enable it", func(t *testing.T, f *sitePrefixFixture) {
			require.False(t, (&cdbm.SiteConfig{}).TenantSitePrefix)
			request := model.APISiteUpdateRequest{Capabilities: &model.APISiteCapabilitiesUpdateRequest{TenantSitePrefix: cutil.GetPtr(true)}}
			require.Error(t, request.Validate(false, true))
			require.NoError(t, request.Validate(true, false))
			config := request.Capabilities.ToSiteConfig(&cdbm.SiteConfig{})
			require.True(t, config.TenantSitePrefix)
		}},

		{"create retry preserves identity and metadata", func(t *testing.T, f *sitePrefixFixture) {
			body := f.createBody("10.0.0.0/24")
			rec := f.request(t, http.MethodPost, "", body)
			first := readSitePrefix(t, rec, 201)
			require.Equal(t, "/v2/org/tenant/nico/site-prefix/"+first.ID, rec.Header().Get(echo.HeaderLocation))
			require.Equal(t, cdbm.IPBlockStatusProvisioning, first.Status)
			require.Equal(t, uint32(8), first.Quota.Limit)
			body.Name = "ignored-retry-name"
			again := readSitePrefix(t, f.request(t, http.MethodPost, "", body), 201)
			require.Equal(t, first.ID, again.ID)
			require.Equal(t, first.Name, again.Name)
			require.Len(t, f.calls, 1)
			changed := readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, map[string]any{"name": "renamed", "description": ""}), 200)
			require.Equal(t, "renamed", changed.Name)
			require.Equal(t, cutil.GetPtr(""), changed.Description)
			get := f.request(t, http.MethodGet, "/"+first.ID, nil)
			readSitePrefix(t, get, 200)
			for _, field := range []string{"description", "coreStatus", "retryMessage", "retirementRequestedAt", "quota", "vpcPrefixIds", "statusHistory"} {
				require.Contains(t, get.Body.String(), "\""+field+"\":")
			}
		}},
		{"interrupted before Core call resumes durable row", func(t *testing.T, f *sitePrefixFixture) {
			id := uuid.New()
			block, err := cdbm.NewIPBlockDAO(f.session).Create(context.Background(), nil, cdbm.IPBlockCreateInput{IPBlockID: &id, SitePrefixID: &id, TenantID: &f.tenant.ID, SiteID: f.site.ID, InfrastructureProviderID: f.site.InfrastructureProviderID, Name: "tenant-root", Prefix: "10.0.0.0", PrefixLength: 24, ProtocolVersion: cdbm.IPBlockProtocolVersionV4, RoutingType: cdbm.IPBlockRoutingTypeDatacenterOnly, Status: cdbm.IPBlockStatusProvisioning})
			require.NoError(t, err)
			_, err = cdbm.NewIPBlockDAO(f.session).Update(context.Background(), nil, cdbm.IPBlockUpdateInput{IPBlockID: block.ID, SitePrefixState: &cdbm.SitePrefixState{}})
			require.NoError(t, err)
			first := model.APISitePrefix{ID: id.String()}

			again := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, first.ID, again.ID)
			require.Len(t, f.calls, 1)
		}},
		{"unknown result retry attaches to same workflow", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = serviceerror.NewDeadlineExceeded("caller stopped waiting")
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, cdbm.IPBlockStatusProvisioning, first.Status)
			require.NotNil(t, first.RetryMessage)
			again := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, first.ID, again.ID)
			require.Equal(t, f.workflowIDs[0], f.workflowIDs[1])
			require.Nil(t, again.RetryMessage)
		}},
		{"closed transient failure retries a new workflow with the same Core identity", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = tp.NewNonRetryableApplicationError("temporarily unavailable", swe.ErrTypeNICoUnavailable, nil)
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, cdbm.IPBlockStatusProvisioning, first.Status)
			again := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, first.ID, again.ID)
			require.NotEqual(t, f.workflowIDs[0], f.workflowIDs[1])
			require.Nil(t, again.RetryMessage)
		}},
		{"a rejected retry cannot settle an earlier ambiguous create", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = tp.NewNonRetryableApplicationError("unknown transport result", swe.ErrTypeNICoUnavailable, nil)
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.nextError = tp.NewNonRetryableApplicationError("quota changed", swe.ErrTypeNICoResourceExhausted, nil)
			readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.nextError = tp.NewNonRetryableApplicationError("still rejected", swe.ErrTypeNICoResourceExhausted, nil)
			readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
			block, err := cdbm.NewIPBlockDAO(f.session).GetByID(context.Background(), nil, uuid.MustParse(first.ID), nil)
			require.NoError(t, err)
			require.NotNil(t, block.SitePrefixState.Operation)
			require.True(t, block.SitePrefixState.Operation.Ambiguous)
			require.Nil(t, block.SitePrefixState.RetirementSettledAt)
			for _, call := range f.calls {
				require.Equal(t, "/forge.Forge/CreateSitePrefix", call.FullMethod)
			}
		}},

		{"expired history preserves uncertainty while retrying the same resource", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = serviceerror.NewDeadlineExceeded("caller stopped waiting")
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			dao := cdbm.NewIPBlockDAO(f.session)
			block, err := dao.GetByID(context.Background(), nil, uuid.MustParse(first.ID), nil)
			require.NoError(t, err)
			block.SitePrefixState.Operation.CreatedAt = time.Now().Add(-8 * 24 * time.Hour)
			_, err = dao.Update(context.Background(), nil, cdbm.IPBlockUpdateInput{IPBlockID: block.ID, SitePrefixState: block.SitePrefixState})
			require.NoError(t, err)
			unknown := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.NotNil(t, unknown.RetryMessage)
			block, err = dao.GetByID(context.Background(), nil, block.ID, nil)
			require.NoError(t, err)
			require.True(t, block.SitePrefixState.Operation.Ambiguous)
			again := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, first.ID, again.ID)
			require.Nil(t, again.RetryMessage)
			require.NotEqual(t, f.workflowIDs[0], f.workflowIDs[1])
		}},

		{"Temporal history NotFound never confirms retirement", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.nextError = serviceerror.NewNotFound("workflow history expired")
			readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
			block, err := cdbm.NewIPBlockDAO(f.session).GetByID(context.Background(), nil, uuid.MustParse(first.ID), nil)
			require.NoError(t, err)
			require.NotNil(t, block.SitePrefixState.Operation)
			require.Nil(t, block.SitePrefixState.RetirementSettledAt)
		}},
		{"metadata retry keeps a version fence and preserves labels", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.core[first.ID].Metadata.Labels = []*corev1.Label{{Key: "retained", Value: cutil.GetPtr("value")}}
			f.nextError = tp.NewNonRetryableApplicationError("transient", swe.ErrTypeNICoUnavailable, nil)
			body := map[string]any{"name": "updated-name"}
			readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, body), 200)
			firstAttempt := f.calls[len(f.calls)-1]
			again := readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, body), 200)
			require.Equal(t, "updated-name", again.Name)
			require.JSONEq(t, string(firstAttempt.RequestJSON), string(f.calls[len(f.calls)-1].RequestJSON))
			require.Equal(t, "retained", f.core[first.ID].Metadata.Labels[0].Key)
		}},
		{"lost committed PATCH response recovers through version conflict", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.nextErrorAfterCommit = tp.NewNonRetryableApplicationError("response lost after commit", swe.ErrTypeNICoUnavailable, nil)
			body := map[string]any{"name": "committed-name"}
			pending := readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, body), 200)
			require.Equal(t, first.Name, pending.Name)
			require.Equal(t, "committed-name", f.core[first.ID].Metadata.Name)
			again := readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, body), 200)
			require.Equal(t, "committed-name", again.Name)
			require.Nil(t, again.RetryMessage)
			require.JSONEq(t, string(f.calls[1].RequestJSON), string(f.calls[2].RequestJSON))
			newer := readSitePrefix(t, f.request(t, http.MethodPatch, "/"+first.ID, map[string]any{"name": "newer-name"}), 200)
			require.Equal(t, "newer-name", newer.Name)
		}},

		{"resource lock does not consume the only query connection", func(t *testing.T, f *sitePrefixFixture) {
			f.session.DB.SetMaxOpenConns(1)
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			body, err := json.Marshal(f.createBody("10.0.0.0/24"))
			require.NoError(t, err)
			req := httptest.NewRequest(http.MethodPost, "/v2/org/tenant/nico/site-prefix", strings.NewReader(string(body))).WithContext(ctx)
			req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
			rec := httptest.NewRecorder()
			f.router.ServeHTTP(rec, req)
			readSitePrefix(t, rec, http.StatusCreated)
		}},

		{"same root is serialized while unrelated roots progress", func(t *testing.T, f *sitePrefixFixture) {
			conn, err := f.session.DB.Conn(context.Background())
			require.NoError(t, err)
			defer conn.Close()
			key := cdb.GetAdvisoryLockIDFromString(fmt.Sprintf("tenant-site-prefix:%s:%s:%s", f.tenant.ID, f.site.ID, "10.0.0.0/24"))
			_, err = conn.ExecContext(context.Background(), "SELECT pg_advisory_lock(?)", key)
			require.NoError(t, err)
			defer func() {
				_, err := conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(?)", key)
				require.NoError(t, err)
			}()
			require.Equal(t, 409, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")).Code)
			readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.1.0.0/24")), 201)
		}},

		{"retirement waits for unknown create", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = serviceerror.NewDeadlineExceeded("unknown")
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			f.nextError = serviceerror.NewDeadlineExceeded("still unknown")
			retired := readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
			require.Equal(t, cdbm.IPBlockStatusDeleting, retired.Status)
			require.NotNil(t, retired.RetirementRequestedAt)
			require.Len(t, f.calls, 2)
			require.Equal(t, f.workflowIDs[0], f.workflowIDs[1])
			require.Equal(t, "/forge.Forge/CreateSitePrefix", f.calls[1].FullMethod)
			retired = readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
			require.Equal(t, first.ID, retired.ID)
			require.Len(t, f.calls, 4)
			require.Equal(t, "/forge.Forge/DeleteSitePrefix", f.calls[3].FullMethod)
			readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
			require.Len(t, f.calls, 4)
			readSitePrefix(t, f.request(t, http.MethodGet, "/"+first.ID, nil), 200)
			require.Equal(t, 409, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")).Code)
		}},
		{"definitive quota rejection is recoverable with same resource", func(t *testing.T, f *sitePrefixFixture) {
			f.nextError = tp.NewNonRetryableApplicationError("quota", swe.ErrTypeNICoResourceExhausted, nil)
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, cdbm.IPBlockStatusError, first.Status)
			require.Contains(t, *first.RetryMessage, "quota")
			again := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			require.Equal(t, first.ID, again.ID)
			require.NotEqual(t, f.workflowIDs[0], f.workflowIDs[1])
		}},
		{"capability off preserves reads and retirement", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			_, err := cdbm.NewSiteDAO(f.session).Update(context.Background(), nil, cdbm.SiteUpdateInput{SiteID: f.site.ID, Config: &cdbm.SiteConfigUpdateInput{TenantSitePrefix: cutil.GetPtr(false)}})
			require.NoError(t, err)
			require.Equal(t, 412, f.request(t, http.MethodPost, "", f.createBody("10.1.0.0/24")).Code)
			readSitePrefix(t, f.request(t, http.MethodGet, "/"+first.ID, nil), 200)
			readSitePrefix(t, f.request(t, http.MethodDelete, "/"+first.ID, nil), 202)
		}},
		{"child references cannot expose another tenant", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			id := uuid.MustParse(first.ID)
			provider := &cdbm.InfrastructureProvider{ID: f.site.InfrastructureProviderID}
			vpc := common.TestBuildVPC(t, f.session, "own-vpc", provider, f.tenant, f.site, nil, nil, nil, cdbm.VpcStatusReady, f.user)
			child := common.TestBuildVPCPrefix(t, f.session, "own-child", f.site, f.tenant, vpc.ID, &id, cutil.GetPtr("10.0.0.0"), cutil.GetPtr(26), cdbm.VpcPrefixStatusReady, f.user)
			other := common.TestBuildTenant(t, f.session, "other", "other", f.user)
			foreignVpc := common.TestBuildVPC(t, f.session, "foreign-vpc", provider, other, f.site, nil, nil, nil, cdbm.VpcStatusReady, f.user)
			common.TestBuildVPCPrefix(t, f.session, "foreign-child", f.site, other, foreignVpc.ID, &id, cutil.GetPtr("10.0.0.64"), cutil.GetPtr(26), cdbm.VpcPrefixStatusReady, f.user)
			got := readSitePrefix(t, f.request(t, http.MethodGet, "/"+first.ID, nil), 200)
			require.Equal(t, []string{child.ID.String()}, got.VpcPrefixIDs)
		}},

		{"tenant privacy and deterministic pagination", func(t *testing.T, f *sitePrefixFixture) {
			first := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")), 201)
			second := readSitePrefix(t, f.request(t, http.MethodPost, "", f.createBody("10.1.0.0/24")), 201)
			other := common.TestBuildTenant(t, f.session, "other", "other", f.user)
			id := uuid.New()
			foreign, err := cdbm.NewIPBlockDAO(f.session).Create(context.Background(), nil, cdbm.IPBlockCreateInput{Name: "private-other", TenantID: &other.ID, SitePrefixID: &id, SiteID: f.site.ID, InfrastructureProviderID: f.site.InfrastructureProviderID, Prefix: "10.2.0.0", PrefixLength: 24, ProtocolVersion: cdbm.IPBlockProtocolVersionV4, RoutingType: cdbm.IPBlockRoutingTypeDatacenterOnly, Status: cdbm.IPBlockStatusReady})
			require.NoError(t, err)
			unknown := f.request(t, http.MethodGet, "/"+uuid.NewString(), nil)
			for _, method := range []string{http.MethodGet, http.MethodPatch, http.MethodDelete} {
				rec := f.request(t, method, "/"+foreign.ID.String(), nil)
				require.Equal(t, unknown.Code, rec.Code)
				require.Equal(t, unknown.Body.String(), rec.Body.String())
			}
			rec := f.request(t, http.MethodGet, "?pageSize=1&orderBy=CREATED_ASC", nil)
			require.Equal(t, 200, rec.Code, rec.Body.String())
			require.NotEmpty(t, rec.Header().Get("X-Pagination"))
			require.Contains(t, rec.Body.String(), first.ID)
			require.NotContains(t, rec.Body.String(), foreign.ID.String())
			rec = f.request(t, http.MethodGet, "?pageSize=1&pageNumber=2&orderBy=CREATED_ASC", nil)
			require.Contains(t, rec.Body.String(), second.ID)
			rec = f.request(t, http.MethodGet, "?status=Ready", nil)
			require.JSONEq(t, "[]", rec.Body.String())
			rec = f.request(t, http.MethodGet, "?query=not-present", nil)
			require.JSONEq(t, "[]", rec.Body.String())
			blocks, _, err := cdbm.NewIPBlockDAO(f.session).GetAll(context.Background(), nil, cdbm.IPBlockFilterInput{ExcludeTenantSitePrefixes: true}, cdbp.PageInput{}, nil)
			require.NoError(t, err)
			require.Empty(t, blocks)
		}},
		{"reject immutable fields and unauthorized caller", func(t *testing.T, f *sitePrefixFixture) {
			body := map[string]any{"name": "root", "siteId": f.site.ID.String(), "prefix": "10.0.0.0/24", "tenantId": f.tenant.ID.String()}
			require.Equal(t, 400, f.request(t, http.MethodPost, "", body).Code)
			require.Empty(t, f.calls)
			f.user = common.TestBuildUser(t, f.session, uuid.NewString(), "different-org", []string{auth.TenantAdminRole})
			require.Equal(t, 403, f.request(t, http.MethodPost, "", f.createBody("10.0.0.0/24")).Code)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) { tc.check(t, newSitePrefixFixture(t)) })
	}
}
