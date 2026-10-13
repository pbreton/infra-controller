// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package workflow

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"github.com/rs/zerolog"
	zlogadapter "logur.dev/adapter/zerolog"
	"logur.dev/logur"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/worker"

	ctemporal "github.com/NVIDIA/infra-controller/rest-api/common/pkg/temporal"
	computils "github.com/NVIDIA/infra-controller/rest-api/site-agent/pkg/components/utils"
	"github.com/NVIDIA/infra-controller/rest-api/site-agent/pkg/datatypes/elektratypes"
	workflowtypes "github.com/NVIDIA/infra-controller/rest-api/site-agent/pkg/datatypes/managertypes/workflow"
	swu "github.com/NVIDIA/infra-controller/rest-api/site-workflow/pkg/util"
)

// Orchestrator - Workflow Orchestrator
func Orchestrator() {
	log := ManagerAccess.Data.EB.Log
	state := ManagerAccess.Data.EB.Managers.Workflow.State

	// Health checks treat the Site Agent as connecting until this attempt finishes.
	state.SetWorker(nil)

	// Cleanup resources
	if ManagerAccess.Data.EB.Managers.Workflow.Temporal.Worker != nil {
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Worker.Stop()
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Worker = nil
	}
	if ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher != nil {
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher.Close()
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher = nil
	}
	if ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber != nil {
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber.Close()
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber = nil
	}

	// keep track how many events we've seen.
	state.ConnectionAttempted.Inc()
	state.SetConnectionTime(time.Now())

	status, err := workflowOrchestrator()
	if err != nil {
		state.HealthStatus.Store(uint64(computils.CompUnhealthy))
		errMsg := err.Error()
		state.SetErr(errMsg)
		log.Error().Msg(errMsg)
		status = workflowtypes.NewWorkerStatus()
		status.SetErr(err)
	} else {
		// keep track how many succeeded.
		state.ConnectionSucc.Inc()
		state.HealthStatus.Store(uint64(computils.CompHealthy))
	}
	state.SetWorker(status)
}

// stopWorker records that the Temporal SDK stopped the worker on an error it
// does not retry. The SDK never restarts it, so the liveness check fails from
// here on and Kubernetes restarts the Site Agent.
func stopWorker(eb *elektratypes.Elektra, status *workflowtypes.WorkerStatus, err error) {
	status.SetErr(err)
	state := eb.Managers.Workflow.State
	// A worker that a reload already replaced has nothing left to report.
	if state.Worker() != status {
		return
	}
	eb.Log.Error().Err(err).Msg("Workflow: Temporal worker stopped, failing the liveness check")
	state.HealthStatus.Store(uint64(computils.CompUnhealthy))
	state.SetErr(err.Error())
}

// StartWorkflow - Workflow init function
func workflowOrchestrator() (*workflowtypes.WorkerStatus, error) {
	// Set the global handle here
	log := ManagerAccess.Data.EB.Log

	// Initialize Temporal client
	log.Info().Msg("Workflow: Creating Elektra site agent Temporal workflow orchestrator")

	// The shared interceptor also implements the worker interface, so the
	// worker built from the subscriber client inherits it and must not
	// register it again.
	var clientInterceptors []interceptor.ClientInterceptor
	// otelErr, not err: `var err error` is declared further down.
	otelInterceptor, otelErr := ctemporal.TracingInterceptor()
	if otelErr != nil {
		return nil, fmt.Errorf("creating Temporal tracing interceptor: %w", otelErr)
	}
	if otelInterceptor != nil {
		clientInterceptors = append(clientInterceptors, otelInterceptor)
	}

	// Create logger for temporal using
	// zero logger
	// This is optional
	// ManagerAccess.Data.EB.Managers.Workflow.Temporal.Logger = lg.NewTemporalLogger(log)

	var publishClientConnOptions client.ConnectionOptions
	var subscribeClientConnOptions client.ConnectionOptions

	if ManagerAccess.Conf.EB.EnableTLS {
		log.Info().Msg("Workflow: Creating Forge Cluster Temporal client with TLS enable")

		// TemporalCertPath should exist
		if ManagerAccess.Conf.EB.Temporal.TemporalCertPath == "" {
			log.Panic().Err(errors.New("unable to find temporal cert path")).Msg("Workflow: Unable to find temporal cert path")
		}

		// Load client cert
		// CACertPath
		fileName, TemporalCACertPath := ManagerAccess.Conf.EB.Temporal.GetTemporalCACertFilePath()
		TemporalCACertPath = TemporalCACertPath + fileName

		// ClientCertPath
		kpFileName, TemporalClientCertPath := ManagerAccess.Conf.EB.Temporal.GetTemporalClientCertFilePath()
		log.Info().Msgf("Workflow: Paths are client: %s, ca: %s", TemporalClientCertPath, TemporalCACertPath)
		clientcert, err := tls.LoadX509KeyPair(fmt.Sprintf("%v/%v", TemporalClientCertPath, kpFileName[0]),
			fmt.Sprintf("%v/%v", TemporalClientCertPath, kpFileName[1]))
		if err != nil {
			log.Error().Msg("Workflow: Unable to read client certificates")
			return nil, err
		}

		// Each pod loads its own certificate on startup and reload.
		leaf := clientcert.Leaf
		if leaf == nil {
			// GODEBUG=x509keypairleaf=0 leaves Leaf unset after a successful load.
			leaf, err = x509.ParseCertificate(clientcert.Certificate[0])
		}
		if err == nil && leaf != nil && CertExpirationMetric != nil {
			CertExpirationMetric.Set(float64(leaf.NotAfter.Unix()))
		} else {
			log.Warn().Err(err).Msg("Workflow: Unable to update Temporal certificate expiration metric")
		}

		// Load server cert
		caCert, err := os.ReadFile(TemporalCACertPath)
		if err != nil {
			log.Error().Msg("Workflow: Unable to read server certificates")
			return nil, err
		}
		caCertPool := x509.NewCertPool()
		caCertPool.AppendCertsFromPEM(caCert)

		// provide tls cert option for publishing workflows
		publishClientConnOptions = client.ConnectionOptions{
			TLS: &tls.Config{
				Certificates: []tls.Certificate{clientcert},
				ServerName:   ManagerAccess.Conf.EB.Temporal.TemporalServer,
				RootCAs:      caCertPool,
			},
			KeepAliveTime:    10 * time.Second,
			KeepAliveTimeout: 60 * time.Second,
		}

		// provide tls cert option for subscribing workflows
		subscribeClientConnOptions = client.ConnectionOptions{
			TLS: &tls.Config{
				Certificates: []tls.Certificate{clientcert},
				ServerName:   ManagerAccess.Conf.EB.Temporal.TemporalServer,
				RootCAs:      caCertPool,
			},
			KeepAliveTime:    10 * time.Second,
			KeepAliveTimeout: 60 * time.Second,
		}
	}
	var err error
	// Initialize client for publish namespace
	tLogger := logur.LoggerToKV(zlogadapter.New(zerolog.New(os.Stderr)))

	host := ManagerAccess.Conf.EB.Temporal.Host
	if strings.HasPrefix(host, "[") && strings.HasSuffix(host, "]") {
		host = host[1 : len(host)-1]
	}
	target := net.JoinHostPort(host, ManagerAccess.Conf.EB.Temporal.Port)
	log.Info().Msgf("Workflow: Connecting to %s", target)
	clientOptions := client.Options{
		HostPort:          target,
		Namespace:         ManagerAccess.Conf.EB.Temporal.TemporalPublishNamespace,
		ConnectionOptions: publishClientConnOptions,
		DataConverter:     swu.NewTemporalDataConverter(),
		Interceptors:      clientInterceptors,
		Logger:            tLogger,
	}

	if ManagerAccess.Data.EB.Conf.UtMode {
		log.Info().Msg("Workflow: UT mode Temporal client")
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher, err = client.NewLazyClient(clientOptions)
	} else {
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher, err = client.Dial(clientOptions)
	}
	if err != nil {
		log.Error().Msg("Workflow: Failed to create Temporal client")
		return nil, err
	}

	// Initialize client for subscribe namespace
	clientOptions = client.Options{
		HostPort:          target,
		Namespace:         ManagerAccess.Conf.EB.Temporal.TemporalSubscribeNamespace,
		ConnectionOptions: subscribeClientConnOptions,
		DataConverter:     swu.NewTemporalDataConverter(),
		Interceptors:      clientInterceptors,
		Logger:            tLogger,
	}

	if ManagerAccess.Data.EB.Conf.UtMode {
		log.Info().Msg("Workflow: UT mode Temporal client")
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber, err = client.NewLazyClient(clientOptions)
	} else {
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber, err = client.Dial(clientOptions)
	}
	if err != nil {
		log.Error().Msg("Workflow: Failed to create Temporal client")
		return nil, err
	}

	status := workflowtypes.NewWorkerStatus(
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Publisher,
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber)
	eb := ManagerAccess.Data.EB
	ManagerAccess.Data.EB.Managers.Workflow.Temporal.Worker = worker.New(
		ManagerAccess.Data.EB.Managers.Workflow.Temporal.Subscriber,
		ManagerAccess.Conf.EB.Temporal.TemporalSubscribeQueue,
		worker.Options{
			WorkflowPanicPolicy: worker.FailWorkflow,
			OnFatalError: func(err error) {
				stopWorker(eb, status, err)
			},
		})
	log.Info().Msg("Workflow: Registering orchestrator workflows and activities for elektra cluster ")

	if ManagerAccess.Conf.EB.DevMode {
		log.Info().Msg("Workflow: Enabled orchestrator for development")
	} else {
		log.Info().Msg("Workflow: Enabled orchestrator for production")
	}

	// Register all manager flows here
	err = ManagerAccess.API.Site.RegisterPublisher()
	if err != nil {
		return nil, err
	}

	err = ManagerAccess.API.VPC.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VPC: failed to register subscriber")
	}
	err = ManagerAccess.API.VPC.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VPC: failed to register inventory publisher")
	}

	err = ManagerAccess.API.VpcPrefix.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VpcPrefix: failed to register subscriber")
	}
	err = ManagerAccess.API.VpcPrefix.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VpcPrefix: failed to register inventory publisher")
	}

	err = ManagerAccess.API.VpcPeering.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VpcPeering: failed to register subscriber")
	}
	err = ManagerAccess.API.VpcPeering.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("VpcPeering: failed to register inventory publisher")
	}

	// Inventory only: SpectrumX Partition CRUD goes through the generic Core gRPC proxy.
	err = ManagerAccess.API.SpectrumXPartition.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("SpectrumXPartition: failed to register inventory publisher")
	}

	err = ManagerAccess.API.Subnet.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Subnet: failed to register subscriber")
	}
	err = ManagerAccess.API.Subnet.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Subnet: failed to register inventory publisher")
	}

	err = ManagerAccess.API.InfiniBandPartition.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("InfiniBandPartition: failed to register subscriber")
	}
	err = ManagerAccess.API.InfiniBandPartition.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("InfiniBandPartition: failed to register inventory publisher")
	}

	err = ManagerAccess.API.SSHKeyGroup.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("SSHKeyGroup: failed to register subscriber")
	}
	err = ManagerAccess.API.SSHKeyGroup.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("SSHKeyGroup: failed to register inventory publisher")
	}

	err = ManagerAccess.API.Machine.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Machine: failed to register subscriber")
	}
	err = ManagerAccess.API.Machine.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Machine: failed to register inventory publisher")
	}

	err = ManagerAccess.API.Instance.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Instance: failed to register subscriber")
	}
	err = ManagerAccess.API.Instance.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Instance: failed to register inventory publisher")
	}

	err = ManagerAccess.API.Bootstrap.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Bootstrap: failed to register subscriber")
	}

	err = ManagerAccess.API.Tenant.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Tenant: failed to register subscriber")
	}
	err = ManagerAccess.API.Tenant.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("Tenant: failed to register inventory publisher")
	}

	err = ManagerAccess.API.OperatingSystem.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("OperatingSystem: failed to register subscriber")
	}
	err = ManagerAccess.API.OperatingSystem.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("OperatingSystem: failed to register inventory publisher")
	}

	err = ManagerAccess.API.MachineValidation.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("MachineValidation: failed to register subscriber")
	}

	// Generic Core gRPC proxy: one workflow/activity for all proxied operations,
	// registered on the Core gRPC manager that owns the connection.
	err = ManagerAccess.API.CoreGrpc.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("CoreGrpc: failed to register subscriber")
	}

	err = ManagerAccess.API.InstanceType.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("InstanceType: failed to register subscriber")
	}
	err = ManagerAccess.API.InstanceType.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("InstanceType: failed to register inventory publisher")
	}

	err = ManagerAccess.API.NetworkSecurityGroup.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("NetworkSecurityGroup: failed to register subscriber")
	}
	err = ManagerAccess.API.NetworkSecurityGroup.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("NetworkSecurityGroup: failed to register inventory publisher")
	}

	err = ManagerAccess.API.ExpectedMachine.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedMachine: failed to register subscriber")
	}
	err = ManagerAccess.API.ExpectedMachine.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedMachine: failed to register inventory publisher")
	}

	err = ManagerAccess.API.ExpectedPowerShelf.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedPowerShelf: failed to register subscriber")
	}
	err = ManagerAccess.API.ExpectedPowerShelf.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedPowerShelf: failed to register inventory publisher")
	}

	err = ManagerAccess.API.ExpectedRack.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedRack: failed to register subscriber")
	}
	err = ManagerAccess.API.ExpectedRack.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedRack: failed to register inventory publisher")
	}
	err = ManagerAccess.API.ExpectedRackGroup.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedRackGroup: failed to register publisher")
	}

	err = ManagerAccess.API.ExpectedSwitch.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedSwitch: failed to register subscriber")
	}
	err = ManagerAccess.API.ExpectedSwitch.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("ExpectedSwitch: failed to register inventory publisher")
	}

	err = ManagerAccess.API.SKU.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("SKU: failed to register subscriber")
	}
	err = ManagerAccess.API.SKU.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("SKU: failed to register inventory publisher")
	}

	err = ManagerAccess.API.DpuExtensionService.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("DpuExtensionService: failed to register subscriber")
	}
	err = ManagerAccess.API.DpuExtensionService.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("DpuExtensionService: failed to register inventory publisher")
	}

	err = ManagerAccess.API.NVLinkLogicalPartition.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("NVLinkLogicalPartition: failed to register subscriber")
	}
	err = ManagerAccess.API.NVLinkLogicalPartition.RegisterPublisher()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("NVLinkLogicalPartition: failed to register inventory publisher")
	}

	err = ManagerAccess.API.TenantIdentity.RegisterSubscriber()
	if err != nil {
		ManagerAccess.Data.EB.Log.Error().Err(err).Msg("TenantIdentity: failed to register subscriber")
	}

	// Flow workflows (only registered if Flow gRPC is enabled)
	if ManagerAccess.Conf.EB.FlowGrpc.Enabled {
		if ManagerAccess.API.FlowGrpc != nil {
			err = ManagerAccess.API.FlowGrpc.RegisterSubscriber()
			if err != nil {
				ManagerAccess.Data.EB.Log.Error().Err(err).Msg("FlowGrpc: failed to register subscriber")
			}
		} else {
			log.Error().Msg("FlowGrpc: Flow gRPC is enabled in config but Flow gRPC manager is not initialized")
		}
	}

	// Start listening to the Task Queue
	log.Info().Msg("Workflow: Starting Temporal worker")
	err = ManagerAccess.Data.EB.Managers.Workflow.Temporal.Worker.Start()
	if err != nil {
		log.Error().Msg("Workflow: Failed to start orchestrator worker")
		return nil, err
	}

	return status, nil
}
