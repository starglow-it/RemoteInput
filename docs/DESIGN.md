# Protocol and safety design

## Authentication and identity

The target saves a 256-bit random ownership secret and 192-bit random pairing password in the native user vault **before** its first network request. The relay stores a SHA-256 digest of the high-entropy ownership secret. A transaction and unique index allocate `TARGET-001`, `TARGET-002`, etc.; registration retries using the same secret return the same ID. Numeric allocations are never reused within an intact registry. Existing target registration cannot overwrite its password.

Pairing passwords are stored at the relay as salted scrypt hashes (N=16384, r=8, p=1, 32 bytes). Hash work is off the asyncio thread, with four jobs allowed concurrently. Authentication attempts are limited by IP and target ID, before expensive hashing. Connections, websocket frames and limiter buckets are bounded. These controls are intended for a small private deployment, not to replace a hosting provider's volumetric attack protection.

The target authenticates its own ownership secret at each connection. Controller admission checks the password generation under the same lock used for rotation. There is one connected target and one controller per target ID. Rotation is journaled locally before sending and carries an idempotent rotation ID; a lost reply can be retried after a restart. The relay increments the generation and revokes the current controller. Old input queued before the change is ignored while the target rotates. The original target ID stays unchanged.

The controller saves its target ID/password only after successful pairing. Vault records are scoped to relay URL and role. Windows uses user-bound DPAPI-encrypted files with atomic replacement; Mac uses the login Keychain directly through the Security framework. There is no plaintext storage fallback. Local instance locks prevent concurrent first launches from overwriting the same vault entry.

## Input and timing

Each binary input frame is 41 bytes: opcode, activation epoch, sequence number, controller monotonic send timestamp, target lease nonce, and two integer arguments. There is no arbitrary command, file, clipboard, or screen channel. Only relative motion, key/button transitions, wheel deltas and session/timing controls are accepted. Infrequent authentication/status messages use bounded JSON.

Input callbacks do bounded in-memory queue work. Consecutive moves add their deltas; a key, click, scroll, or other event separates runs. Coalescing keeps the first arrival time so it cannot conceal stale work. Network writes use persistent uncompressed WSS connections. There is no wait for an acknowledgment per production input event. Separate small acknowledgments sample every 32nd input sequence; probes run twice a second even while paused.

Queues cap at 256 items and 100 ms of local age. Network buffers and websocket frame queues are bounded too. A full/expired queue causes a pause/reset or connection abort. It never silently discards a release and keeps controlling. Target injection occurs on a separate worker thread, not in the websocket receive loop.

The Windows controller suppresses ordinary local input only while active. It anchors the local cursor inside the primary display, derives relative deltas from proposed low-level mouse positions and suppresses those moves; the cursor cannot walk to an edge. Pause restores its original position. This native behavior must be checked on physical mice, touchpads and multi-monitor arrangements. Ctrl/Alt modifier downs are deferred until a nonreserved input so the activation/stop chord stays local. Physical keys held during activation are ignored until released.

Native capture activation and cursor restoration run on the hook thread via one coalesced posted state message. A pause disables suppression immediately and supersedes a queued activation. This avoids holding the policy lock on the network thread across native calls that can dispatch hook callbacks. The health timestamp advances during keyboard/mouse callbacks as well as message-loop returns: [Windows dispatches low-level hooks on their installing thread](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc), and [GetMessage can dispatch sent messages before returning, with timers at lower priority](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmessagew). Both callbacks and timer messages enforce the separate network-loop watchdog.

## Independent expiry and cleanup

The target issues an unpredictable lease nonce every 200 ms. The controller must echo a nonce younger than 850 ms with an application heartbeat; websocket ping/pong traffic is insufficient. The target independently releases held input when no accepted heartbeat arrives for 850 ms. Incoming input does not itself refresh that deadline. The controller sends heartbeats during a pending activation as well as during control, ordered after ACTIVATE on the same connection, so the activation reply need not complete another trip before the first heartbeat.

Input, activation and diagnostic-probe tokens have a separate **1,150 ms** maximum age on the **target's** monotonic clock: the 850 ms heartbeat-token budget plus one 200 ms challenge interval and one 100 ms permitted queue interval. A token's age includes its outward trip to the controller and return trip to the target, plus time until the next challenge arrives; it is not the input event's age or one-way latency. The old shared 850 ms cutoff could reject normal input on a slower route even while immediately echoed heartbeats remained fresh. The separate local queue age limit remains 100 ms, expired current-session input still pauses/resets, and the heartbeat deadline remains 850 ms. Old activation epochs and duplicate sequence numbers are ignored before freshness validation so they cannot cancel current control. A bounded token history is retained for numeric pause diagnostics; retaining a token does not extend its acceptance deadline.

The target worker checks permissions and its lease between events and during idle periods. It releases tracked remote keys/buttons when a lease expires, on pause, on controller disconnect, permission loss, input failure, queue overload or shutdown. Failed OS releases are retried and further input remains blocked. Reconnection starts paused with a fresh activation handshake. A new controller cannot inherit previous holds.

The controller's hook thread has a separate message-pump timer. It releases local suppression if the network loop stops updating it, and the network loop stops renewing target access if the hook thread becomes unhealthy. Native OS freezes, process termination in the middle of a system call, secure desktop transitions and kernel failures cannot have a hard real-time release guarantee. The target watchdog handles ordinary controller/network failures independently of relay liveness.

## Privacy and platform boundaries

The target intentionally prints the pairing password to its own console. No disk input log is created. Websocket debug logging is disabled because it can include payloads. Numeric metric samples are bounded; they contain no typed text or passwords. The relay stores hashes and registration metadata, not input events. Caddy has no access/payload logging enabled by this configuration. Anyone operating the trusted relay could alter its code to inspect traffic.

Windows `SendInput` is subject to integrity-level restrictions; it cannot control higher-integrity applications by default or bypass secure attention/UAC. macOS requires Accessibility/event-posting permission, rechecked before activation and while controlling. The Mac target does not install a local input tap or suppress its own controls. Typing is key-based and uses the target layout; there is no clipboard or Unicode-text injection protocol.

## API references used

- [Microsoft SendInput and integrity restrictions](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)
- [Microsoft low-level mouse hooks and callback timing](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelmouseproc)
- [Apple event-posting permission check](https://developer.apple.com/documentation/coregraphics/cgpreflightposteventaccess())
- [websockets asyncio server, compression and buffer settings](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html)
- [Caddy reverse proxy and streaming settings](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
