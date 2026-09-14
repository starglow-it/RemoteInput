# Validation and acceptance checks

## Automated checks performed in the development environment

Linux, Python 3.12.14, websockets 16.0. Run `python -m pytest -q` and `python -m ruff check src tests scripts` from the repository root. The TLS tests generate a short-lived local test certificate, verify it through an explicit test trust context, and verify that the default untrusted context rejects it. Production has no unverified TLS mode.

The automated suite covers:

- Persistent unique allocation under concurrent registration, idempotent first-launch retries, and ownership authentication.
- Random credential generation, persisted local rotation journal, password change, wrong-password rejection and credential forgetting.
- Relay rate limiting, one-controller admission, and revocation during an active connection.
- Frame validation, bounded queues, adjacent-motion addition, ordering around keys/clicks/scrolling, and stale backlog rejection.
- Key repeats/holds, dragging, local reserved hotkeys, permission denial and repeated permission checks through test doubles.
- Reset, disconnect/reconnect, duplicate sequences, expired nonces, and releasing holds with both WSS sockets still connected while application heartbeats stop.
- Full real Controller/Target lifecycle using fake platform backends, including password rotation and forgetting the rejected old password.
- The same verified WSS lifecycle with 225 ms and 350 ms simulated propagation in each direction (450 ms and 700 ms added full-path RTT), including capture failure, held-input cleanup and explicit reactivation. The 700 ms case sustains a held drag for six seconds across many token refreshes; the earlier shared token cutoff reproduced `Paused: stale input`. Exact freshness boundaries use a deterministic clock. These are not measurements on the user's network.
- Independent input-token/heartbeat deadlines: a still-fresh heartbeat permits input bearing the previous challenge, genuinely expired input resets/releases, an old token cannot refresh the heartbeat deadline, and old epochs/duplicates cannot pause the current activation.
- Windows activation dispatch, a concurrent physical callback during local modifier release, busy hook health, cancellation of queued activation, cursor restoration, activation failure/retry and network-loop watchdog behavior through Win32 API doubles on every test OS. These tests do not install real hooks or send native input.
- Native DPAPI encryption, persistence and deletion **on Windows runners only**; skipped on Linux/macOS.

The initial 750 ms wall-clock lifecycle passed on Linux, Windows and Intel Mac. An Apple Silicon CI runner delayed one heartbeat to 856 ms, correctly triggering the separate 850 ms safety cutoff. The current 700 ms case leaves more scheduler headroom and still reproduces stale-input rejection with the old input cutoff; the exact token-boundary tests retain a deterministic 750 ms heartbeat / 950 ms input scenario. Production safety deadlines were not changed to accommodate the runner.

Native input is never sent by automated tests. The fake sink exists only under `tests/`; production launchers instantiate only the native OS backend.

## Required manual checks on owned test machines

These checks have **not been performed on the user's computers**. Mark a row passed only after observing its result on Windows 11 -> Windows and Windows 11 -> macOS.

| Check | Procedure and expected result |
|---|---|
| Clean installation | Extract the native package on a PC without Python/VPN; launch using Start Target/Controller. No missing DLL/module errors. |
| Console behavior | Launch twice: once by double-click and once from an existing terminal. Exactly one normal target console; existing terminal reused. |
| Persistent credentials | Note the ID/password, restart the target and relay with the same data volume, and confirm both values persist. |
| First-launch race | Launch Start Target twice quickly. One instance continues; the other reports already running. No extra target ID or overwritten vault record. |
| Permission denied/granted | On Mac deny Accessibility. No input accepted. Open the offered Settings page, grant permission, press Enter, and confirm input only then works. Revoke during a hold and verify pause/cleanup. |
| Basic typing | In Notepad/TextEdit, type letters, punctuation, Shift/Caps Lock, Enter, arrows, Backspace and Delete. Hold a letter and verify repeat stops on release. Verify matching keyboard layouts. |
| Shortcuts | Test copy/paste, select all, save, undo and find. On Mac verify Left Ctrl=Command, Right Ctrl=Control, Windows=Control, Alt=Option. |
| Mouse | Test left/right/middle and side buttons, double click, drag selection, window drag, vertical and horizontal scrolling. |
| Continuous movement | Move beyond each controller screen edge repeatedly, including multi-monitor and high-DPI setups. Target keeps moving; pausing restores the local cursor. |
| Hotkeys and suppression | Start paused. Toggle with Ctrl+Alt+F9; hold the chord and confirm it toggles only once. Move the mouse while activating and verify it stays Controlling. Press Ctrl+Alt+F10 while holding a key/button. Chords stay local, target holds release, and ordinary local typing resumes. Test with controller console unfocused. |
| Target local access | Move/type locally at the target while connected. There is no target-side suppression. Avoid simultaneous conflicting key holds during this check. |
| Password change | While a remote key/button is held, type P and Enter locally at the target. Holds release; the old controller loses access. New password works, old one fails, ID persists. |
| Forget Target | Use F and Enter while paused. Restart; credentials are requested again and old vault entry is gone. |
| Drop network | While dragging or holding a modifier, disable controller networking and then relay networking in separate trials. Holds release; restored connectivity remains paused. |
| Freeze controller | Suspend its process while holding a key/button, leaving the relay alive. Target releases within roughly 850 ms after its last accepted heartbeat, plus scheduling time; in-flight heartbeats can add network transit time after suspension. Resume; control must be reactivated. |
| Overload | Introduce a long process pause/slow injection or sustained excessive event rate in a test build. The app pauses and releases; it does not replay a long backlog on recovery. |
| Elevated/protected Windows app | Confirm ordinary apps work. Secure desktop/UAC must require local interaction. No claim of higher-integrity injection from a normal process. |
| Real latency | Collect median/p95 live target-ACK measurements and relay-link measurements from both PCs. Record region, wired/Wi-Fi, location, time and network conditions. Measure the screen feed separately. |

## Build status boundaries

The build workflow targets Windows x64, Intel macOS and Apple Silicon macOS. Successful packaging/self-checks do not certify the manual rows above. GitHub's Windows build host may be Windows Server rather than Windows 11. Intel and ARM packages are separate; no cross-platform executable is implied.

Code signing, notarization, clean-machine installation, physical hook suppression/cursor anchoring, macOS TCC/Keychain approval dialogs, non-US layouts/IME, secure desktops, real US-to-China routes and the separate screen feed are not validated in the Linux development environment. The relay Docker deployment also requires a real host/domain for end-to-end deployment verification.

The `relay-deployment` CI job builds and starts the actual Compose services in an isolated project, using Caddy's local CA for `localhost`. It verifies the certificate, `/health` HTTP 200, an unknown route HTTP 404, and the real `/ws` diagnostic exchange and ping/pong. It does not disable TLS verification. Public DNS, ACME issuance, and the user's server firewall still need checking on the deployed host.
