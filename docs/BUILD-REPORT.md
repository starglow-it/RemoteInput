# Build report — 14 September 2026

Source: commit `689b9ccfe41422f06185712c6a170d978584f6d6`, tree `a2bfaae95715741b05f9ddcd89a62aa805323408`.

[Verification and native packaging run](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151).

These packages bundle Python, websockets and public CA certificates. They are **setup-pending builds**: no hosted relay domain was provided or deployed. They deliberately refuse pairing until the developer rebuilds with the real WSS address. They do not contain a shared/default password or a fabricated relay hostname.

| Package | Result | Download |
|---|---|---|
| Windows x64 | Tests and packaged Win32/CA self-check passed | [Windows artifact](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151/artifacts/10359897085) |
| macOS Intel | Tests and packaged CoreGraphics/CA self-check passed | [Intel Mac artifact](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151/artifacts/10359214394) |
| macOS Apple Silicon | Tests and packaged CoreGraphics/CA self-check passed | [Apple Silicon artifact](https://github.com/starglow-it/RemoteInput/actions/runs/34872458151/artifacts/10359073195) |

GitHub downloads are artifact wrappers containing the platform package and its SHA-256 checksum. Extract the wrapper, then extract the inner platform archive. A GitHub sign-in may be required. These native artifacts expire on 14 October 2026; the workflow can rebuild them from source. They have not been published as signed/notarized release assets.

Local Linux verification: **54 tests passed, one Windows-only DPAPI test skipped**; Ruff passed. The native Windows runner passed **55 tests**, including the DPAPI check. Both Mac runners passed 54 and skipped that Windows-only test. Packaged self-checks load native libraries and the bundled certificate roots; they do not inject input.

The local Linux packaged binary also passed its protocol/CA self-check. It supports relay/diagnostic commands; Linux is not an input target or controller platform for this app.

The TLS benchmark and its median/p95 data are in [LATENCY.md](LATENCY.md) and [latency-local.json](latency-local.json). These measure a loopback test harness and a controlled 40 ms delay simulation. Native OS input timing, either user's PC, the real internet route, and the separate screen feed were not measured.

Remaining setup: deploy the relay to an actual domain, set `REMOTEINPUT_RELAY_URL` in repository Actions variables, run the build workflow, and complete [the manual Windows/macOS acceptance checks](TESTING.md). See [DEPLOY.md](DEPLOY.md) for the developer steps and [SETUP.md](SETUP.md) for everyday use.
