# Configured builds — 14 September 2026

Source: commit `129c65bdab4c4c84ea94980c5ced9166f1a16008`, tree `fcfdc86b7094937a5bbd77acbbff8abc137a4def`.

[Successful verification and native packaging run](https://github.com/starglow-it/RemoteInput/actions/runs/34896131644).

Includes automatic network timing for the reported 900/927 ms heartbeat ages and target-update gaps. **Update both the target and controller**; the existing relay remains compatible. Both endpoints use full-path RTT and variation to choose a network deadline bounded to **2–3 seconds**. Input tokens add a 300 ms refresh/queue allowance. Calibration works while paused and adds no intentional delay before input delivery. See [network-pause troubleshooting and source update commands](TROUBLESHOOTING.md#target-reports-paused-stale-input).

The target now releases holds after **2–3 seconds without an accepted new heartbeat**, plus OS scheduling; heartbeats already in flight can add transit time after a controller freeze. Replayed challenges cannot renew a hold. Local queues retain their **100 ms** limit, the native capture watchdogs remain unchanged, and reconnection stays paused. The earlier Windows hook-thread activation fix is also included.

These packages bundle Python, dependencies, public CA certificates, and **`wss://172-86-119-204.sslip.io/ws`**. This address was verified by the owner's Windows probe. The Ubuntu relay must remain running. No Python installation, relay argument, or configuration-file editing is needed to launch the packaged app.

| Package | Use | Download |
|---|---|---|
| Windows x64 | Target and controller | [Windows package](https://github.com/starglow-it/RemoteInput/actions/runs/34896131644/artifacts/10369421372) |
| macOS Intel | Target | [Intel Mac package](https://github.com/starglow-it/RemoteInput/actions/runs/34896131644/artifacts/10369320935) |
| macOS Apple Silicon | Target | [Apple Silicon Mac package](https://github.com/starglow-it/RemoteInput/actions/runs/34896131644/artifacts/10368488513) |

GitHub downloads are ZIP wrappers containing the platform archive and its SHA-256 checksum. Extract the wrapper, then extract the inner archive. Keep the complete extracted app folder together, including `_internal`. A GitHub sign-in may be required. These download artifacts expire on 14 October 2026; the workflow can rebuild them from source.

## Start using the app

1. On the target, run **Start Target.cmd** on Windows or **Start Target.command** on Mac. Approve Accessibility if macOS requests it.
2. On the Windows controller, run **Start Controller.cmd** and enter the target's displayed ID and password.
3. The connection starts paused. **Ctrl+Alt+F9** starts/pauses control; **Ctrl+Alt+F10** stops control. Keep both consoles open.
4. Use **Measure Relay** on both computers to measure their separate relay paths. While the controller is paused, **M + Enter** reports the full controller-to-target-to-controller RTT.

See [SETUP.md](SETUP.md) for password changes, forgetting a target, Mac key mappings, and permission handling.

## Verified build results

- Windows: **84 tests passed**. The packaged binary loaded Win32, reported desktop access on the runner, loaded certificate roots, and confirmed an embedded relay address.
- Intel and Apple Silicon Macs: **83 tests passed, one Windows-only DPAPI test skipped** on each. Both packaged binaries loaded CoreGraphics and certificate roots and confirmed an embedded relay address.
- Ubuntu CI: protocol/safety tests, lint, and the controlled latency experiment passed. The actual relay and Caddy Compose deployment passed verified TLS, `/health` HTTP 200, fallback HTTP 404, and the `/ws` diagnostic/ping exchange using a temporary local CA.
- Each native build log confirms that the embedded address is the verified `172-86-119-204.sslip.io` relay. These packages have no `setup-pending` suffix.
- The delayed WSS lifecycle uses 350 ms and 450 ms simulated propagation each way with FIFO delivery (700 ms and 900 ms added full-path RTT). Both sustain a held drag for ten seconds. The 900 ms case also stops wire delivery for 750 ms, then drops only application heartbeats while relay pings/challenges/probes still flow. Holds release, sockets remain connected, and reactivation is explicit. The pre-fix source reproduced `Paused: stale heartbeat` at 903 ms on the same route.
- Deterministic tests cover the owner's 900/927 ms heartbeat ages, expired input, replayed challenges, paused calibration, controller update gaps and the three-second cleanup bound. Windows activation regressions use simulated Win32 APIs. These simulations do not measure physical desktop behavior or the owner's network latency; see [TESTING.md](TESTING.md).

The builds are unsigned on Windows and ad-hoc signed on macOS; publisher signing and Apple notarization were not performed. CI imports and tests do not certify interactive desktop behavior on the user's computers. Complete [the manual checks](TESTING.md) for permissions, typing, held keys, dragging, hotkeys, screen edges, overload, and disconnect cleanup. The relay-container CI check uses a local CA; the owner's successful probe separately confirmed public certificate validation and the deployed route.

## Live latency status

The owner reported **225.67 ms median / 226.23 ms p95** over 10 Windows-to-relay-and-back samples. This excludes the other computer's relay leg, input injection, and the separate screen feed. The client's location and connection type were not supplied. Full-path and native-input measurements on this pair are still pending.

Later target diagnostics reported heartbeat-token ages of **900 ms and 927 ms** against the old 850 ms cutoff. Those two failure samples include the challenge/echo trip and local processing; they are not a median/p95 report or a one-way delay measurement. Real desktop confirmation of the new network timing is still pending.

[LATENCY.md](LATENCY.md) records this live sample separately from the reproducible before/after optimization experiment and explains how to measure both PCs before selecting a relay region.

Earlier [setup-pending validation builds](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151) did not embed a relay address. The [previous input-token fix](https://github.com/starglow-it/RemoteInput/actions/runs/34893372072) retained the 850 ms heartbeat and 650 ms controller update limits. Use the current downloads above for everyday use.
