// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/netip"
	"time"

	"github.com/google/uuid"
	"github.com/labstack/echo/v4"
	temporalEnums "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	tclient "go.temporal.io/sdk/client"
	tp "go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/handler/util/common"
	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/model"
	"github.com/NVIDIA/infra-controller/rest-api/api/pkg/api/pagination"
	sc "github.com/NVIDIA/infra-controller/rest-api/api/pkg/client/site"
	auth "github.com/NVIDIA/infra-controller/rest-api/auth/pkg/authorization"
	cutil "github.com/NVIDIA/infra-controller/rest-api/common/pkg/util"
	cdb "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db"
	cdbm "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/model"
	cdbp "github.com/NVIDIA/infra-controller/rest-api/db/pkg/db/paginator"
	corev1 "github.com/NVIDIA/infra-controller/rest-api/proto/core/gen/v1"
)

const (
	createSitePrefixMethod  = "/forge.Forge/CreateSitePrefix"
	updateSitePrefixMethod  = "/forge.Forge/UpdateSitePrefix"
	findSitePrefixesMethod  = "/forge.Forge/FindSitePrefixesByIds"
	deleteSitePrefixMethod  = "/forge.Forge/DeleteSitePrefix"
	sitePrefixNotFound      = "SitePrefix not found"
	sitePrefixUnknownResult = "Core result is unknown; retry the same request to recover it. Retirement waits for the earlier mutation to settle."
)

// sitePrefixHandler owns the tenant root lifecycle shared by its five public operations.
type sitePrefixHandler struct {
	dbSession *cdb.Session
	scp       *sc.ClientPool
}

type CreateSitePrefixHandler struct{ sitePrefixHandler }
type GetSitePrefixHandler struct{ sitePrefixHandler }
type GetAllSitePrefixHandler struct{ sitePrefixHandler }
type UpdateSitePrefixHandler struct{ sitePrefixHandler }
type DeleteSitePrefixHandler struct{ sitePrefixHandler }

func NewCreateSitePrefixHandler(s *cdb.Session, p *sc.ClientPool) CreateSitePrefixHandler {
	return CreateSitePrefixHandler{sitePrefixHandler{s, p}}
}
func NewGetSitePrefixHandler(s *cdb.Session, p *sc.ClientPool) GetSitePrefixHandler {
	return GetSitePrefixHandler{sitePrefixHandler{s, p}}
}
func NewGetAllSitePrefixHandler(s *cdb.Session, p *sc.ClientPool) GetAllSitePrefixHandler {
	return GetAllSitePrefixHandler{sitePrefixHandler{s, p}}
}
func NewUpdateSitePrefixHandler(s *cdb.Session, p *sc.ClientPool) UpdateSitePrefixHandler {
	return UpdateSitePrefixHandler{sitePrefixHandler{s, p}}
}
func NewDeleteSitePrefixHandler(s *cdb.Session, p *sc.ClientPool) DeleteSitePrefixHandler {
	return DeleteSitePrefixHandler{sitePrefixHandler{s, p}}
}

func (h CreateSitePrefixHandler) Handle(c echo.Context) error { return h.handle(c, http.MethodPost) }
func (h GetSitePrefixHandler) Handle(c echo.Context) error    { return h.handle(c, http.MethodGet) }
func (h GetAllSitePrefixHandler) Handle(c echo.Context) error { return h.handle(c, "List") }
func (h UpdateSitePrefixHandler) Handle(c echo.Context) error { return h.handle(c, http.MethodPatch) }
func (h DeleteSitePrefixHandler) Handle(c echo.Context) error { return h.handle(c, http.MethodDelete) }

func decodeSitePrefixRequest(c echo.Context, target any) error {
	decoder := json.NewDecoder(c.Request().Body)
	var raw json.RawMessage
	err := decoder.Decode(&raw)
	if err != nil || len(raw) == 0 || raw[0] != '{' {
		return cutil.NewAPIError(http.StatusBadRequest, "Request must contain one JSON object", nil)
	}
	strict := json.NewDecoder(bytes.NewReader(raw))
	strict.DisallowUnknownFields()
	err = strict.Decode(target)
	if err != nil {
		return cutil.NewAPIError(http.StatusBadRequest, "Invalid request body; only documented fields may be supplied", nil)
	}
	err = decoder.Decode(new(any))
	if err != io.EOF {
		return cutil.NewAPIError(http.StatusBadRequest, "Request must contain one JSON object", nil)
	}
	return nil
}

func (h sitePrefixHandler) handle(c echo.Context, operation string) error {
	org, user, ctx, logger, span := common.SetupHandler("SitePrefix", operation, c)
	if span != nil {
		defer span.End()
	}
	fail := func(err error) error { return common.HandleTxError(c, logger, err, "Failed to process SitePrefix") }
	if user == nil {
		return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to retrieve current user", nil)
	}
	{
		ok, _ := auth.ValidateOrgMembership(user, org)
		if !ok || !auth.ValidateUserRoles(user, org, nil, auth.TenantAdminRole) {
			return cutil.NewAPIErrorResponse(c, http.StatusForbidden, "Tenant Admin membership in the path org is required", nil)
		}
	}
	tenant, err := common.GetTenantForOrg(ctx, nil, h.dbSession, org)
	if err != nil {
		return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Could not resolve Tenant for org", nil)
	}
	if operation == "List" {
		return h.list(c, tenant)
	}
	dao := cdbm.NewIPBlockDAO(h.dbSession)
	var block *cdbm.IPBlock
	var create model.APISitePrefixCreateRequest
	var update model.APISitePrefixUpdateRequest
	var siteID uuid.UUID
	if operation == http.MethodPost {
		{
			err := decodeSitePrefixRequest(c, &create)
			if err != nil {
				return fail(err)
			}
		}
		{
			err := create.Validate()
			if err != nil {
				return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Invalid SitePrefix", err)
			}
		}
		siteID = uuid.MustParse(create.SiteID)
	} else {
		id, err := uuid.Parse(c.Param("id"))
		if err != nil {
			return cutil.NewAPIErrorResponse(c, http.StatusNotFound, sitePrefixNotFound, nil)
		}
		block, err = dao.GetByID(ctx, nil, id, nil)
		if errors.Is(err, cdb.ErrDoesNotExist) || (err == nil && (block.TenantID == nil || *block.TenantID != tenant.ID || block.SitePrefixID == nil)) {
			return cutil.NewAPIErrorResponse(c, http.StatusNotFound, sitePrefixNotFound, nil)
		}
		if err != nil {
			return fail(err)
		}
		siteID = block.SiteID
		if operation == http.MethodPatch {
			{
				err := decodeSitePrefixRequest(c, &update)
				if err != nil {
					return fail(err)
				}
			}
			{
				err := update.Validate()
				if err != nil {
					return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Invalid SitePrefix metadata", err)
				}
			}
		}
	}
	site, err := h.authorizeSite(ctx, tenant.ID, siteID, operation != http.MethodPost)
	if err != nil {
		return fail(err)
	}
	if operation == http.MethodGet {
		return h.respond(c, http.StatusOK, block)
	}
	prefix := create.Prefix
	if block != nil {
		prefix = fmt.Sprintf("%s/%d", block.Prefix, block.PrefixLength)
	}
	// A dedicated connection keeps this session lock out of the query pool. Transactions
	// below commit before any Core call; unrelated roots use different locks.
	err = h.withLock(ctx, tenant.ID, siteID, prefix, func() error {
		if operation == http.MethodPost {
			p := netip.MustParsePrefix(create.Prefix)
			err = cdb.WithTx(ctx, h.dbSession, func(tx *cdb.Tx) error {
				currentSite, e := cdbm.NewSiteDAO(h.dbSession).GetByIDForUpdate(ctx, tx, siteID)
				if e != nil {
					return e
				}
				if currentSite.Status != cdbm.SiteStatusRegistered {
					return cutil.NewAPIError(http.StatusBadRequest, "Site must be Registered", nil)
				}
				{
					_, e := cdbm.NewTenantSiteDAO(h.dbSession).GetByTenantIDAndSiteID(ctx, tx, tenant.ID, siteID, nil)
					if e != nil {
						return cutil.NewAPIError(http.StatusBadRequest, "Site is not attached to this Tenant", nil)
					}
				}
				roots, _, e := dao.GetAll(ctx, tx, cdbm.IPBlockFilterInput{TenantIDs: []uuid.UUID{tenant.ID}, SiteIDs: []uuid.UUID{siteID}, CoreLinkedOnly: true, Prefixes: []string{p.Addr().String()}, PrefixLengths: []int{p.Bits()}}, cdbp.PageInput{}, nil)
				if e != nil {
					return e
				}
				if len(roots) > 0 {
					block = &roots[0]
					return nil
				}
				if currentSite.Config == nil || !currentSite.Config.TenantSitePrefix {
					return cutil.NewAPIError(http.StatusPreconditionFailed, "Site does not enable tenant-managed SitePrefixes", nil)
				}
				id := uuid.New()
				block, e = dao.Create(ctx, tx, cdbm.IPBlockCreateInput{IPBlockID: &id, SitePrefixID: &id, TenantID: &tenant.ID, SiteID: site.ID, InfrastructureProviderID: site.InfrastructureProviderID,
					Name: create.Name, Description: create.Description, Prefix: p.Addr().String(), PrefixLength: p.Bits(), ProtocolVersion: cdbm.IPBlockProtocolVersionV4, RoutingType: cdbm.IPBlockRoutingTypeDatacenterOnly, Status: cdbm.IPBlockStatusProvisioning, CreatedBy: &user.ID})
				if e != nil {
					return e
				}
				block, e = dao.Update(ctx, tx, cdbm.IPBlockUpdateInput{IPBlockID: block.ID, SitePrefixState: &cdbm.SitePrefixState{}})
				return e
			})
			if err != nil {
				return err
			}
		} else {
			block, err = dao.GetByID(ctx, nil, block.ID, nil)
			if err != nil {
				return err
			}
		}
		if operation == http.MethodDelete {
			block, err = dao.RequestSitePrefixRetirement(ctx, nil, block.ID, tenant.ID)
			if err != nil {
				return err
			}
		} else if block.SitePrefixRetirementRequestedAt != nil || block.Status == cdbm.IPBlockStatusDeleting {
			return cutil.NewAPIError(http.StatusConflict, "SitePrefix retirement has been accepted", nil)
		}
		if block.SitePrefixState != nil && block.SitePrefixState.Operation != nil {
			pending := block.SitePrefixState.Operation
			if operation == http.MethodPatch && (pending.Kind != cdbm.SitePrefixUpdate || (update.Name != nil && *update.Name != pending.Name) || (update.Description != nil && *update.Description != pending.Description)) {
				return cutil.NewAPIError(http.StatusConflict, "A different SitePrefix mutation is still pending; retry that operation first", nil)
			}
			block, err = h.execute(ctx, tenant.Org, block)
			if err != nil {
				return err
			}
			if block.SitePrefixState.Operation != nil {
				return nil
			}
			// A matching PATCH retry recovered the original operation; do not dispatch it twice.
			if operation == http.MethodPatch || operation == http.MethodPost {
				return nil
			}
		}
		kind := cdbm.SitePrefixCreate
		if operation == http.MethodDelete {
			kind = cdbm.SitePrefixRetire
		}
		if operation == http.MethodPatch {
			kind = cdbm.SitePrefixUpdate
		}
		state := block.SitePrefixState
		if operation == http.MethodPost && (state == nil || state.CreateSettled) {
			return nil
		}
		if operation == http.MethodDelete && state != nil && state.RetirementSettledAt != nil {
			return nil
		}
		if operation == http.MethodPatch && state != nil && !state.CreateSettled {
			return cutil.NewAPIError(http.StatusConflict, "Retry the original create before updating metadata", nil)
		}
		name, description := block.Name, ""
		if block.Description != nil {
			description = *block.Description
		}
		if operation == http.MethodPatch {
			if update.Name != nil {
				name = *update.Name
			}
			if update.Description != nil {
				description = *update.Description
			}
		}
		var metadata json.RawMessage
		version := ""
		if operation == http.MethodPatch {
			current, e := h.readCore(ctx, tenant.Org, block)
			if e != nil {
				return e
			}
			if current.GetVersion() == "" {
				return cutil.NewAPIError(http.StatusBadGateway, "Core did not return a SitePrefix version", nil)
			}
			version = current.Version
			value := current.GetMetadata()
			if value == nil {
				value = &corev1.Metadata{}
			}
			if update.Name != nil {
				value.Name = *update.Name
			}
			if update.Description != nil {
				value.Description = *update.Description
			}
			name, description = value.Name, value.Description
			metadata, err = protojson.Marshal(value)
			if err != nil {
				return err
			}
		}

		err = cdb.WithTx(ctx, h.dbSession, func(tx *cdb.Tx) error {
			fresh, e := dao.GetByIDForUpdate(ctx, tx, block.ID)
			if e != nil {
				return e
			}
			if fresh.SitePrefixState == nil {
				fresh.SitePrefixState = &cdbm.SitePrefixState{CreateSettled: true}
			}
			fresh.SitePrefixState.Operation = &cdbm.SitePrefixOperation{Kind: kind, WorkflowID: "site-prefix-" + uuid.NewString(), CreatedAt: cdb.GetCurTime(), Name: name, Description: description, Version: version, Metadata: metadata}
			fresh.SitePrefixState.RetryMessage = nil
			block, e = dao.Update(ctx, tx, cdbm.IPBlockUpdateInput{IPBlockID: fresh.ID, SitePrefixState: fresh.SitePrefixState})
			return e
		})
		if err != nil {
			return err
		}
		block, err = h.execute(ctx, tenant.Org, block)
		return err
	})
	if err != nil {
		return fail(err)
	}
	code := http.StatusOK
	if operation == http.MethodPost {
		code = http.StatusCreated
		c.Response().Header().Set(echo.HeaderLocation, fmt.Sprintf("/v2/org/%s/nico/site-prefix/%s", org, block.ID))
	}
	if operation == http.MethodDelete {
		code = http.StatusAccepted
	}
	return h.respond(c, code, block)
}

func (h sitePrefixHandler) authorizeSite(ctx context.Context, tenantID, siteID uuid.UUID, item bool) (*cdbm.Site, error) {
	code := http.StatusBadRequest
	message := "Site is not attached to this Tenant"
	if item {
		code = http.StatusNotFound
		message = sitePrefixNotFound
	}
	{
		_, err := cdbm.NewTenantSiteDAO(h.dbSession).GetByTenantIDAndSiteID(ctx, nil, tenantID, siteID, nil)
		if err != nil {
			if errors.Is(err, cdb.ErrDoesNotExist) {
				return nil, cutil.NewAPIError(code, message, nil)
			}
			return nil, err
		}
	}
	site, err := cdbm.NewSiteDAO(h.dbSession).GetByID(ctx, nil, siteID, nil, false)
	if errors.Is(err, cdb.ErrDoesNotExist) {
		return nil, cutil.NewAPIError(code, message, nil)
	}
	if err != nil {
		return nil, err
	}
	if site.Status != cdbm.SiteStatusRegistered {
		return nil, cutil.NewAPIError(http.StatusBadRequest, "Site must be Registered", nil)
	}
	return site, nil
}

func (h sitePrefixHandler) withLock(ctx context.Context, tenantID, siteID uuid.UUID, prefix string, fn func() error) (retErr error) {
	conn, err := h.dbSession.AcquireSessionLockConnection(ctx)
	if err != nil {
		return err
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		// Closing the physical connection releases every session lock. Never
		// return a possibly locked connection to another request.
		retErr = errors.Join(retErr, conn.Conn().Close(closeCtx))
		conn.Release()
	}()
	key := cdb.GetAdvisoryLockIDFromString(fmt.Sprintf("tenant-site-prefix:%s:%s:%s", tenantID, siteID, prefix))
	var locked bool
	err = conn.QueryRow(ctx, "SELECT pg_try_advisory_lock($1)", key).Scan(&locked)
	if err != nil {
		return err
	}
	if !locked {
		return cutil.NewAPIError(http.StatusConflict, "Another request is updating this SitePrefix; retry shortly", nil)
	}
	return fn()
}

// sitePrefixClient retains ExecuteCoreGRPC's typed encoding and error mapping,
// while a durable resource operation supplies its workflow identity. The SDK
// attaches to running and completed executions instead of replaying the RPC.
type sitePrefixClient struct {
	tclient.Client
	operation   *cdbm.SitePrefixOperation
	resultError *error
}

func (c sitePrefixClient) ExecuteWorkflow(ctx context.Context, options tclient.StartWorkflowOptions, workflow any, args ...any) (tclient.WorkflowRun, error) {
	options.ID = c.operation.WorkflowID
	options.WorkflowIDConflictPolicy = temporalEnums.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING
	options.WorkflowIDReusePolicy = temporalEnums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE
	// After retention, absence of workflow history cannot prove a mutation never ran.
	// Fetch first; missing history is ambiguous and must never confirm retirement.
	if time.Since(c.operation.CreatedAt) >= SiteWorkflowRetentionPeriod-time.Hour {
		return sitePrefixRun{WorkflowRun: c.GetWorkflow(ctx, options.ID, ""), resultError: c.resultError}, nil
	}
	run, err := c.Client.ExecuteWorkflow(ctx, options, workflow, args...)
	if err != nil {
		return nil, err
	}
	return sitePrefixRun{WorkflowRun: run, resultError: c.resultError}, nil
}

// Preserve the Temporal outcome: a history lookup failure is not a Core NotFound.
type sitePrefixRun struct {
	tclient.WorkflowRun
	resultError *error
}

func (r sitePrefixRun) Get(ctx context.Context, value any) error {
	err := r.WorkflowRun.Get(ctx, value)
	*r.resultError = err
	return err
}

func matchesSitePrefix(result *corev1.SitePrefix, org string, block *cdbm.IPBlock) bool {
	return result.GetId().GetValue() == block.SitePrefixID.String() &&
		result.GetConfig().GetTenantOrganizationId() == org &&
		result.GetConfig().GetPrefix() == fmt.Sprintf("%s/%d", block.Prefix, block.PrefixLength) &&
		result.GetStatus().GetAuthority() == corev1.SitePrefixAuthority_SITE_PREFIX_AUTHORITY_TENANT_MANAGED &&
		result.GetConfig().GetRoutingScope() == corev1.SitePrefixRoutingScope_SITE_PREFIX_ROUTING_SCOPE_DATACENTER_ONLY
}

func (h sitePrefixHandler) readCore(ctx context.Context, org string, block *cdbm.IPBlock) (*corev1.SitePrefix, error) {
	client, err := h.scp.GetClientByID(block.SiteID)
	if err != nil {
		return nil, cutil.NewAPIError(http.StatusServiceUnavailable, "Site connection unavailable", nil)
	}
	var result corev1.SitePrefixList
	apiErr := common.ExecuteCoreGRPC(ctx, client, findSitePrefixesMethod, &corev1.SitePrefixesByIdsRequest{SitePrefixIds: []*corev1.SitePrefixId{{Value: block.SitePrefixID.String()}}}, &result, "")
	if apiErr != nil {
		return nil, cutil.NewAPIError(http.StatusServiceUnavailable, "Could not read current SitePrefix from Core; retry the request", nil)
	}
	if len(result.SitePrefixes) != 1 || !matchesSitePrefix(result.SitePrefixes[0], org, block) {
		return nil, cutil.NewAPIError(http.StatusConflict, "Core has not confirmed this SitePrefix identity; retry after inventory reconciliation", nil)
	}
	return result.SitePrefixes[0], nil
}

func (h sitePrefixHandler) execute(ctx context.Context, org string, block *cdbm.IPBlock) (*cdbm.IPBlock, error) {
	op := block.SitePrefixState.Operation
	stc, err := h.scp.GetClientByID(block.SiteID)
	id := &corev1.SitePrefixId{Value: block.SitePrefixID.String()}
	var req, resp proto.Message
	result := &corev1.SitePrefix{}
	var deleted corev1.SitePrefixDeletionResult
	method := createSitePrefixMethod
	switch op.Kind {
	case cdbm.SitePrefixCreate:
		req = &corev1.SitePrefixCreationRequest{Id: id, TenantOrganizationId: org, Prefix: fmt.Sprintf("%s/%d", block.Prefix, block.PrefixLength), Metadata: &corev1.Metadata{Name: op.Name, Description: op.Description}}
		resp = result
	case cdbm.SitePrefixUpdate:
		method = updateSitePrefixMethod
		metadata := &corev1.Metadata{}
		{
			err := protojson.Unmarshal(op.Metadata, metadata)
			if err != nil {
				return nil, err
			}
		}
		req = &corev1.SitePrefixUpdateRequest{Id: id, TenantOrganizationId: org, Metadata: metadata, IfVersionMatch: &op.Version}
		resp = result
	case cdbm.SitePrefixRetire:
		method = deleteSitePrefixMethod
		req = &corev1.SitePrefixDeletionRequest{Id: id, TenantOrganizationId: org}
		resp = &deleted
	default:
		return nil, errors.New("invalid persisted SitePrefix operation")
	}
	var apiErr *cutil.APIError
	var resultError error
	if err != nil {
		apiErr = cutil.NewAPIError(http.StatusServiceUnavailable, "Site connection unavailable", nil)
	} else {
		apiErr = common.ExecuteCoreGRPC(ctx, sitePrefixClient{stc, op, &resultError}, method, req, resp, "")
	}
	if op.Kind == cdbm.SitePrefixRetire && deleted.SitePrefix != nil {
		result = deleted.SitePrefix
	}
	var applicationError *tp.ApplicationError
	coreError := errors.As(resultError, &applicationError)
	var closedError *tp.WorkflowExecutionError
	var timeoutError *tp.TimeoutError
	var missingHistory *serviceerror.NotFound
	closed := coreError || errors.As(resultError, &closedError) || errors.As(resultError, &timeoutError) || errors.As(resultError, &missingHistory)
	// A CAS retry may find that its earlier attempt already committed.
	if op.Kind == cdbm.SitePrefixUpdate && coreError && apiErr != nil && apiErr.Code == http.StatusPreconditionFailed {
		current, readErr := h.readCore(ctx, org, block)
		metadata := &corev1.Metadata{}
		decodeErr := protojson.Unmarshal(op.Metadata, metadata)
		if readErr == nil && decodeErr == nil && proto.Equal(current.GetMetadata(), metadata) {
			result = current
			apiErr = nil
		}
	}
	absent := coreError && op.Kind == cdbm.SitePrefixRetire && apiErr != nil && apiErr.Code == http.StatusNotFound
	if apiErr == nil {
		if !matchesSitePrefix(result, org, block) || sitePrefixLifecycle(result.GetStatus().GetLifecycleState()) == "" || (op.Kind == cdbm.SitePrefixRetire && result.GetStatus().GetLifecycleState() != corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_DELETING) {
			apiErr = cutil.NewAPIError(http.StatusInternalServerError, "Core returned an inconsistent SitePrefix; result remains unknown", nil)
		}
	}
	return cdb.WithTxResult(ctx, h.dbSession, func(tx *cdb.Tx) (*cdbm.IPBlock, error) {
		dao := cdbm.NewIPBlockDAO(h.dbSession)
		fresh, e := dao.GetByIDForUpdate(ctx, tx, block.ID)
		if e != nil {
			return nil, e
		}
		state := fresh.SitePrefixState
		if state == nil || state.Operation == nil || state.Operation.WorkflowID != op.WorkflowID {
			return nil, errors.New("SitePrefix operation identity changed")
		}
		status := fresh.Status
		update := cdbm.IPBlockUpdateInput{IPBlockID: fresh.ID, SitePrefixState: state}
		message := "Core accepted SitePrefix mutation; waiting for lifecycle inventory"
		definite := coreError && apiErr != nil && ((apiErr.Code >= 400 && apiErr.Code < 500 && apiErr.Code != http.StatusRequestTimeout) || apiErr.Code == http.StatusNotImplemented)
		if apiErr == nil || absent {
			state.Operation = nil
			state.RetryMessage = nil
			if op.Kind == cdbm.SitePrefixCreate {
				state.CreateSettled = true
			}
			if op.Kind == cdbm.SitePrefixRetire {
				now := cdb.GetCurTime()
				state.RetirementSettledAt = &now
			}
			if op.Kind == cdbm.SitePrefixUpdate {
				update.Name = &op.Name
				update.Description = &op.Description
			}
			if !absent {
				mapped := sitePrefixLifecycle(result.GetStatus().GetLifecycleState())
				if mapped != "" {
					state.CoreStatus = &mapped
					status = mapped
				}
				{
					quota := result.GetStatus().GetQuota()
					if quota != nil {
						state.Quota = &cdbm.SitePrefixQuota{Used: quota.Used, Limit: quota.Limit}
					}
				}
			}
		} else if definite && (op.Kind != cdbm.SitePrefixCreate || !op.Ambiguous) {
			state.Operation = nil
			status = cdbm.IPBlockStatusError
			message = "Core rejected the SitePrefix mutation; verify Site support and address conflicts, then retry"
			if apiErr.Code == http.StatusTooManyRequests {
				message = "Tenant SitePrefix quota exceeded; retire unused prefixes before retrying"
			}
			state.RetryMessage = &message
		} else {
			message = sitePrefixUnknownResult
			if closed {
				state.Operation.Ambiguous = true
				// Core retains retiring IDs, so same-ID create/retire replay is
				// monotonic. PATCH replay uses the persisted version fence.
				// Never reuse the immutable result of a closed Temporal attempt.
				state.Operation.WorkflowID = "site-prefix-" + uuid.NewString()
				state.Operation.CreatedAt = cdb.GetCurTime()
			}
			state.RetryMessage = &message
			if op.Kind == cdbm.SitePrefixCreate && fresh.Status != cdbm.IPBlockStatusError {
				status = cdbm.IPBlockStatusProvisioning
			}
		}
		if fresh.SitePrefixRetirementRequestedAt != nil {
			status = cdbm.IPBlockStatusDeleting
		}
		update.Status = &status
		fresh, e = dao.Update(ctx, tx, update)
		if e != nil {
			return nil, e
		}
		_, e = cdbm.NewStatusDetailDAO(h.dbSession).Create(ctx, tx, cdbm.StatusDetailCreateInput{EntityID: fresh.ID.String(), Status: status, Message: &message})
		return fresh, e
	})
}

func sitePrefixLifecycle(value corev1.SitePrefixLifecycleState) string {
	switch value {
	case corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_PROVISIONING:
		return cdbm.IPBlockStatusProvisioning
	case corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_READY:
		return cdbm.IPBlockStatusReady
	case corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_DELETING:
		return cdbm.IPBlockStatusDeleting
	case corev1.SitePrefixLifecycleState_SITE_PREFIX_LIFECYCLE_STATE_ERROR:
		return cdbm.IPBlockStatusError
	}
	return ""
}

func (h sitePrefixHandler) response(ctx context.Context, block *cdbm.IPBlock) (*model.APISitePrefix, error) {
	result := new(model.APISitePrefix)
	result.FromDBModel(block)
	children, _, err := cdbm.NewVpcPrefixDAO(h.dbSession).GetAll(ctx, nil, cdbm.VpcPrefixFilterInput{TenantIDs: []uuid.UUID{*block.TenantID}, SiteIDs: []uuid.UUID{block.SiteID}, IpBlockIDs: []uuid.UUID{block.ID}}, cdbp.PageInput{Limit: cutil.GetPtr(cdbp.TotalLimit)}, nil)
	if err != nil {
		return nil, err
	}
	for _, child := range children {
		result.VpcPrefixIDs = append(result.VpcPrefixIDs, child.ID.String())
	}
	details, err := cdbm.NewStatusDetailDAO(h.dbSession).GetRecentByEntityIDs(ctx, nil, []string{block.ID.String()}, common.RECENT_STATUS_DETAIL_COUNT)
	if err != nil {
		return nil, err
	}
	for _, detail := range details {
		result.StatusHistory = append(result.StatusHistory, model.NewAPIStatusDetail(detail))
	}
	return result, nil
}
func (h sitePrefixHandler) respond(c echo.Context, code int, block *cdbm.IPBlock) error {
	result, err := h.response(c.Request().Context(), block)
	if err != nil {
		return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to populate SitePrefix response", nil)
	}
	return c.JSON(code, result)
}

func (h sitePrefixHandler) list(c echo.Context, tenant *cdbm.Tenant) error {
	ctx := c.Request().Context()
	page := pagination.PageRequest{}
	filters := struct {
		SiteID string `query:"siteId"`
		Status string `query:"status"`
		Query  string `query:"query"`
	}{}
	{
		err := common.ValidateKnownQueryParams(c.QueryParams(), page, filters)
		if err != nil {
			return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, err.Error(), nil)
		}
	}
	{
		err := (&echo.DefaultBinder{}).BindQueryParams(c, &page)
		if err != nil {
			return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Invalid pagination", nil)
		}
	}
	{
		err := page.Validate(cdbm.IPBlockOrderByFields)
		if err != nil {
			return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, err.Error(), nil)
		}
	}
	attached, _, err := cdbm.NewTenantSiteDAO(h.dbSession).GetAll(ctx, nil, cdbm.TenantSiteFilterInput{TenantIDs: []uuid.UUID{tenant.ID}}, cdbp.PageInput{Limit: cutil.GetPtr(cdbp.TotalLimit)}, nil)
	if err != nil {
		return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to resolve Tenant Sites", nil)
	}
	siteIDs := []uuid.UUID{}
	for _, relation := range attached {
		site, e := cdbm.NewSiteDAO(h.dbSession).GetByID(ctx, nil, relation.SiteID, nil, false)
		if e == nil && site.Status == cdbm.SiteStatusRegistered {
			siteIDs = append(siteIDs, site.ID)
		} else if e != nil && !errors.Is(e, cdb.ErrDoesNotExist) {
			return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to resolve Tenant Sites", nil)
		}
	}
	{
		selector := c.QueryParam("siteId")
		if selector != "" {
			id, e := uuid.Parse(selector)
			if e != nil {
				return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Invalid Site ID", nil)
			}
			{
				_, e = h.authorizeSite(ctx, tenant.ID, id, false)
				if e != nil {
					var apiErr *cutil.APIError
					if errors.As(e, &apiErr) {
						return apiErr.Send(c)
					}
					return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to resolve Tenant Site", nil)
				}
			}
			siteIDs = []uuid.UUID{id}
		}
	}
	filter := cdbm.IPBlockFilterInput{TenantIDs: []uuid.UUID{tenant.ID}, SiteIDs: siteIDs, CoreLinkedOnly: true, SearchQuery: common.GetSearchQuery(c)}
	{
		status := c.QueryParam("status")
		if status != "" {
			if !cdbm.IPBlockStatusMap[status] || status == cdbm.IPBlockStatusPending {
				return cutil.NewAPIErrorResponse(c, http.StatusBadRequest, "Invalid SitePrefix status", nil)
			}
			filter.Statuses = []string{status}
		}
	}
	blocks, total, err := cdbm.NewIPBlockDAO(h.dbSession).GetAll(ctx, nil, filter, cdbp.PageInput{Offset: page.Offset, Limit: page.Limit, OrderBy: page.OrderBy}, nil)
	if err != nil {
		return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to list SitePrefixes", nil)
	}
	results := []*model.APISitePrefix{}
	for i := range blocks {
		result, e := h.response(ctx, &blocks[i])
		if e != nil {
			return cutil.NewAPIErrorResponse(c, http.StatusInternalServerError, "Failed to populate SitePrefix response", nil)
		}
		results = append(results, result)
	}
	header, err := json.Marshal(pagination.NewPageResponse(*page.PageNumber, *page.PageSize, total, page.OrderByStr))
	if err != nil {
		return err
	}
	c.Response().Header().Set(pagination.ResponseHeaderName, string(header))
	return c.JSON(http.StatusOK, results)
}
