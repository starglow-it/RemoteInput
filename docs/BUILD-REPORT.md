# Configured builds — 14 September 2026

Source: commit `904ae1d73d90d0c79ceb8cf6a9102f4c4cbe0470`, tree `6a4c253cb39dc664f9ccea6ec5c20ce8872b10dd`.

[Successful verification and native packaging run](https://github.com/starglow-it/RemoteInput/actions/runs/34887713919).

These packages bundle Python, dependencies, public CA certificates, and **`wss://172-86-119-204.sslip.io/ws`**. This address was verified by the owner's Windows probe. The Ubuntu relay must remain running. No Python installation, relay argument, or configuration-file editing is needed to launch the packaged app.

| Package | Use | Download |
|---|---|---|
| Windows x64 | Target and controller | [Windows package](https://github.com/starglow-it/RemoteInput/actions/runs/34887713919/artifacts/10365882239) |
| macOS Intel | Target | [Intel Mac package](https://github.com/starglow-it/RemoteInput/actions/runs/34887713919/artifacts/10365467654) |
| macOS Apple Silicon | Target | [Apple Silicon Mac package](https://github.com/starglow-it/RemoteInput/actions/runs/34887713919/artifacts/10365732220) |

GitHub downloads are ZIP wrappers containing the platform archive and its SHA-256 checksum. Extract the wrapper, then extract the inner archive. Keep the complete extracted app folder together, including `_internal`. A GitHub sign-in may be required. These download artifacts expire on 14 October 2026; the workflow can rebuild them from source.

## Start using the app

1. On the target, run **Start Target.cmd** on Windows or **Start Target.command** on Mac. Approve Accessibility if macOS requests it.
2. On the Windows controller, run **Start Controller.cmd** and enter the target's displayed ID and password.
3. The connection starts paused. **Ctrl+Alt+F9** starts/pauses control; **Ctrl+Alt+F10** stops control. Keep both consoles open.
4. Use **Measure Relay** on both computers to measure their separate relay paths. While the controller is paused, **M + Enter** reports the full controller-to-target-to-controller RTT.

See [SETUP.md](SETUP.md) for password changes, forgetting a target, Mac key mappings, and permission handling.

## Verified build results

- Windows: **65 tests passed**. The packaged binary loaded Win32, reported desktop access on the runner, loaded certificate roots, and confirmed an embedded relay address.
- Intel and Apple Silicon Macs: **64 tests passed, one Windows-only DPAPI test skipped** on each. Both packaged binaries loaded CoreGraphics and certificate roots and confirmed an embedded relay address.
- Ubuntu CI: protocol/safety tests, lint, and the controlled latency experiment passed. The actual relay and Caddy Compose deployment passed verified TLS, `/health` HTTP 200, fallback HTTP 404, and the `/ws` diagnostic/ping exchange using a temporary local CA.
- Each native build log confirms that the embedded address is the verified `172-86-119-204.sslip.io` relay. These packages have no `setup-pending` suffix.

The builds are unsigned on Windows and ad-hoc signed on macOS; publisher signing and Apple notarization were not performed. CI imports and tests do not certify interactive desktop behavior on the user's computers. Complete [the manual checks](TESTING.md) for permissions, typing, held keys, dragging, hotkeys, screen edges, overload, and disconnect cleanup. The relay-container CI check uses a local CA; the owner's successful probe separately confirmed public certificate validation and the deployed route.

## Live latency status

The owner reported **225.67 ms median / 226.23 ms p95** over 10 Windows-to-relay-and-back samples. This excludes the other computer's relay leg, input injection, and the separate screen feed. The client's location and connection type were not supplied. Full-path and native-input measurements on this pair are still pending.

[LATENCY.md](LATENCY.md) records this live sample separately from the reproducible before/after optimization experiment and explains how to measure both PCs before selecting a relay region.

Earlier [setup-pending validation builds](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151) did not embed a relay address. Use the configured downloads above for everyday use.
