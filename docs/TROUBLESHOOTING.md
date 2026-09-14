# Connection and input troubleshooting

## Target reports Paused: stale input

This section also applies to `stale heartbeat` and `target updates timed out`. The owner reported heartbeat-token ages of 900/927 ms against an 850 ms limit, plus controller update gaps beyond its old 650 ms cutoff. Those fixed limits were too short for that connection. The earlier input-only fix did not address both network limits. These messages are separate from Python dependency checks and Accessibility approval.

Update and restart **both the target and controller**. Both now use measured full-path RTT and variation to choose a network deadline bounded between **2 and 3 seconds**. The target calibrates from immediate challenge echoes even while paused; the controller uses target-acknowledged probes. Activation, heartbeat age and network silence use the same policy. Input tokens add a 300 ms refresh/queue allowance. This update works with the existing relay and adds no intentional delay before sending input.

Failure cleanup now allows **2–3 seconds after the last accepted new heartbeat**, plus OS scheduling and any heartbeats already in flight. Repeating a nonce cannot renew a hold. Severe stalls still pause/reset, reconnection stays paused, and local queues keep their 100 ms limit. The Windows capture and local network-loop watchdogs remain unchanged. No manual timeout setting is needed.

On the Mac target, type **Q + Enter** in the target console, then:

```sh
cd ~/RemoteInput
git pull --ff-only
.venv-mac/bin/python -m remoteinput target --relay wss://172-86-119-204.sslip.io/ws
```

Use `.venv/bin/python` instead if that is the environment used for your successful installation. On Windows, quit the paused controller with **Q + Enter**, then run from the source checkout:

```text
git pull --ff-only
.venv\Scripts\python.exe -m remoteinput controller --relay wss://172-86-119-204.sslip.io/ws
```

These are editable installs, so a source update does not require reinstalling dependencies. Packaged users should extract the current [native downloads](BUILD-REPORT.md) and restart their launchers.

Target freshness failures include token age and limit; heartbeat/update silence failures include the measured gap and limit. The controller's **M + Enter** report includes its current automatic timeout alongside median/p95 RTT and local processing measurements. If pauses continue, share those numbers and the matching target status line, omitting the password display. A heartbeat message still showing `limit 850 ms` means the old target process is running. Network deadlines are safety limits, not promised input delays; a route that stalls beyond the bounded deadline still pauses.

## Controlling changes immediately to Paused on Windows

Update the Windows controller. Early versions ran cursor positioning and local modifier releases on the network thread while holding the input-policy lock. A physical input callback arriving during that transition could wait for the same lock, stalling activation. A busy hook thread could also be reported as unresponsive because its health timestamp was updated only when `GetMessage` returned, not when it dispatched hook callbacks.

The fix performs native activation/cursor restoration on the hook thread using a coalesced posted message, counts hook callbacks as progress, and prints a specific local pause reason. Pause immediately disables local suppression; queued activation cannot undo a later pause. The existing lease, network-loop and overload safeguards still apply.

While paused, enter **Q** and press Enter to quit the controller. From the Windows source checkout:

```text
git pull --ff-only
.venv\Scripts\python.exe -m remoteinput controller --relay wss://172-86-119-204.sslip.io/ws
```

The editable install uses the updated source directly. The Windows controller update works with the existing target and relay. For packaged installations, use the newest [Windows package](BUILD-REPORT.md).

| Pause message | Meaning and next step |
|---|---|
| `Ctrl+Alt+F9` or `control stopped locally` | A local pause/stop was requested. Release the chord before pressing it again. Holding F9/F10 does not repeatedly toggle control. |
| `Windows input capture stopped responding` | The hook thread is unresponsive. Update/restart the controller and check that the Windows desktop is unlocked. |
| `controller network loop stopped responding` | Local processing stalled; input suppression was disabled to restore local control. |
| `Windows could not activate input capture; check the unlocked desktop` | A native desktop/cursor/input operation failed. Return to the normal unlocked desktop and retry. |
| `target updates timed out (gap ...; limit ...)` | Fresh target challenges stopped arriving beyond the automatic network deadline. Read the target console and collect M + Enter measurements. Older builds use a fixed 650 ms cutoff; update both endpoints. |
| `target activation timed out` | The target did not complete activation within the safety deadline. Check its console and network. |
| `target stopped control; see the target console for the reason` | Read the target's `Paused: ...` line for permission loss, stale input, unsupported keys, injection failure or overload. |
| A controller input queue safety message | Processing fell behind; the controller reset instead of replaying a backlog. |

If the issue continues, share the controller's `Paused: ...` line and the target's corresponding status line. Omit the target ID/password display. **M + Enter** while paused shows full controller-to-target-to-controller measurements. The standalone relay probe measures only that PC's relay link and cannot diagnose native input capture.

Regression tests use simulated Win32 calls and real verified WSS connections with fake input backends. A delayed-path test adds 225 ms in each direction (450 ms round trip); this is a simulation, not a measurement of the owner's Mac or Windows desktop. Physical desktop validation remains necessary.

`probe` checks the network connection. It does not request or require mouse, keyboard, Accessibility, or administrator permissions. Older builds print "Check connectivity and OS permissions" for many unrelated errors; this message alone cannot identify the cause.

## Get the specific failure

From an editable Windows source checkout, update and rerun the probe. Replace `relay.yourdomain.example` with the exact hostname used when deploying Caddy. The editable installation reads the updated source without reinstalling dependencies.

```text
git pull --ff-only
.venv\Scripts\python.exe -m remoteinput probe --relay wss://relay.yourdomain.example/ws --count 10
```

The probe reports when the TLS certificate and WebSocket upgrade succeed, then when the Python relay accepts the diagnostic request. Failures identify the stage and a safe error category. It never prints raw exception text, HTTP response bodies/headers, or WebSocket close reasons, which could include private data. Certificate verification remains enabled.

| Result | What to check |
|---|---|
| DNS lookup failed | The hostname must resolve to the relay server's public IP on this PC. |
| Connection refused | Caddy must be running and listening on the configured HTTPS port. |
| Timeout while opening WSS | Check the containers, inbound TCP 443 at the server/hosting firewall, and outbound connectivity from the PC. |
| TLS certificate verification failed | Check hostname, expiry, full certificate chain, and the PC clock. The default Caddy deployment uses a public DNS hostname; a bare IP gets a locally issued certificate that clients do not trust. |
| TLS negotiation failed | Read Caddy's certificate logs; confirm that it was started with the same hostname used by the client. |
| HTTP 502, 503, or 504 | The proxy rejected the upgrade. Check that the Python relay is running and reachable at `relay:8765` inside the Compose network. |
| HTTP 404 | Update and recreate Caddy as described below; early versions had a fallback that intercepted `/ws`. Also check the hostname. |
| HTTP 401 or 403 | Check proxy access rules; the diagnostic connection does not need a target password. |
| Rate limiting | Wait a minute before retrying. Avoid repeatedly starting probes in parallel. |
| Diagnostic reply or ping failure after the upgrade succeeds | Check the Python relay's status/logs and any proxy connection timeouts. |

## Check the Ubuntu server

Run from the repository root. Set the hostname again in each new SSH session because Compose reads it even for `ps` and `logs`. Use `sudo env RELAY_DOMAIN="$RELAY_DOMAIN" docker ...` if your account needs sudo; root can use the commands below directly.

```sh
export RELAY_DOMAIN=relay.yourdomain.example
docker compose -f deploy/compose.yaml ps -a
docker compose -f deploy/compose.yaml logs --tail=80 caddy relay
```

Both services should be running. If you originally launched with a bare IP or a different hostname, exporting a new value does not update the existing container. Apply it with:

```sh
docker compose -f deploy/compose.yaml up -d --build
```

This retains the existing registry volume. Do not remove the registry volume to fix a connection problem.

### Fix HTTP 404 from the initial Caddy configuration

The initial Caddyfile placed a catch-all `respond 404` beside `reverse_proxy`. Caddy sorts `respond` before `reverse_proxy`, so `/ws` was rejected before reaching Python. The corrected configuration uses mutually exclusive `handle` blocks for `/ws`, `/health`, and the fallback. [Caddy documents this directive ordering](https://caddyserver.com/docs/caddyfile/directives#directive-order).

From the server's repository root, with `RELAY_DOMAIN` set to the hostname used by the client:

```sh
git pull --ff-only
docker compose -f deploy/compose.yaml up -d --build --force-recreate
```

Recreation loads the updated bind-mounted Caddyfile and preserves the named registry/certificate volumes. A plain `up -d` does not reliably apply changes to the contents of a bind-mounted configuration file. Rerun the Windows probe after updating the server; updating only the Windows checkout cannot repair the server route.

The hostname's A/AAAA records must point to the server. Ports 80 and 443 must be available to Caddy and reachable through the hosting firewall for the default certificate setup. Read certificate failures in Caddy's logs; do not disable certificate verification or expose the private relay port 8765.

## Check HTTPS from Windows

From Command Prompt or PowerShell:

```text
nslookup relay.yourdomain.example
curl.exe --noproxy "*" --connect-timeout 10 --max-time 15 --fail --show-error https://relay.yourdomain.example/health
```

The expected HTTPS response is `RemoteInput relay`. This checks Caddy and TLS only: `/health` can work while the Python relay is down. Always rerun `probe` to verify the actual `/ws` connection. The curl command makes a direct connection, like the app, and retains certificate verification.

When reporting a connection issue, provide the probe output, container status, and relevant startup/TLS errors. Do not include the target console's ID/password display or authentication files.
