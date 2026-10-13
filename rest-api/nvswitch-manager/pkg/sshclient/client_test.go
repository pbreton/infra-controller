// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

package sshclient

import (
	"bufio"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/NVIDIA/infra-controller/rest-api/common/pkg/credential"
	"github.com/NVIDIA/infra-controller/rest-api/nvswitch-manager/pkg/objects/nvos"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestNewWithPort(t *testing.T) {
	tests := []struct {
		name    string
		network string
		ip      string
	}{
		{name: "IPv4", network: "tcp4", ip: "127.0.0.1"},
		{name: "IPv6", network: "tcp6", ip: "::1"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ip := net.ParseIP(tt.ip)
			listener, err := net.ListenTCP(tt.network, &net.TCPAddr{IP: ip})
			if tt.network == "tcp6" && (errors.Is(err, syscall.EAFNOSUPPORT) || errors.Is(err, syscall.EADDRNOTAVAIL)) {
				t.Skipf("IPv6 loopback is unavailable: %v", err)
			}
			require.NoError(t, err)
			t.Cleanup(func() {
				assert.NoError(t, listener.Close())
			})

			deadline := time.Now().Add(5 * time.Second)
			err = listener.SetDeadline(deadline)
			require.NoError(t, err)

			serverDone := make(chan struct{})
			var serverErr error
			go func() {
				defer close(serverDone)
				conn, err := listener.AcceptTCP()
				if err != nil {
					serverErr = err
					return
				}
				defer func() {
					assert.NoError(t, conn.Close())
				}()

				serverErr = conn.SetDeadline(deadline)
				if serverErr != nil {
					return
				}
				// Read the client's SSH version before closing so the handshake
				// fails only after the constructor reaches this listener.
				_, serverErr = bufio.NewReader(conn).ReadString('\n')
			}()
			cred := credential.New("admin", "password")
			n := &nvos.NVOS{IP: ip, Credential: &cred}
			port := listener.Addr().(*net.TCPAddr).Port
			client, err := NewWithPort(t.Context(), n, port)
			<-serverDone

			require.NoError(t, serverErr, "SSH client must reach the configured listener")
			assert.Nil(t, client)
			assert.ErrorContains(t, err, "ssh: handshake failed: EOF")
		})
	}
}

func TestNVOSClient_CopyFile(t *testing.T) {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	require.NoError(t, err)
	signer, err := ssh.NewSignerFromKey(key)
	require.NoError(t, err)
	for _, tc := range []struct {
		name        string
		readFailure bool
	}{
		{name: "complete file"},
		{name: "local read failure after opening the file", readFailure: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			require.NoError(t, err)
			defer listener.Close()
			serverDone := make(chan struct{})
			var received []byte
			go func() {
				defer close(serverDone)
				conn, err := listener.Accept()
				if !assert.NoError(t, err) {
					return
				}
				defer conn.Close()
				if !assert.NoError(t, conn.SetDeadline(time.Now().Add(5*time.Second))) {
					return
				}
				config := &ssh.ServerConfig{NoClientAuth: true}
				config.AddHostKey(signer)
				server, channels, requests, err := ssh.NewServerConn(conn, config)
				if !assert.NoError(t, err) {
					return
				}
				defer server.Close()
				go ssh.DiscardRequests(requests)
				channelRequest := <-channels
				if !assert.NotNil(t, channelRequest) {
					return
				}
				channel, requests, err := channelRequest.Accept()
				if !assert.NoError(t, err) {
					return
				}
				defer channel.Close()
				request := <-requests
				if !assert.NotNil(t, request) {
					return
				}
				assert.Equal(t, "exec", request.Type)
				assert.NoError(t, request.Reply(true, nil))
				received, err = io.ReadAll(channel)
				assert.NoError(t, err)
				_, err = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
				assert.NoError(t, err)
			}()
			connection, err := ssh.Dial("tcp", listener.Addr().String(), &ssh.ClientConfig{
				User: "test", HostKeyCallback: ssh.InsecureIgnoreHostKey(), Timeout: 5 * time.Second,
			})
			require.NoError(t, err)
			defer connection.Close()
			localPath := t.TempDir()
			if !tc.readFailure {
				localPath = filepath.Join(localPath, "source")
				require.NoError(t, os.WriteFile(localPath, []byte("payload"), 0600))
			}
			client := &NVOSClient{client: connection}
			err = client.CopyFile(localPath, "/tmp/target")
			<-serverDone
			if tc.readFailure {
				require.ErrorContains(t, err, "copy file to scp")
			} else {
				require.NoError(t, err)
				assert.Equal(t, "C0644 7 target\npayload\x00", string(received))
			}
		})
	}
}
