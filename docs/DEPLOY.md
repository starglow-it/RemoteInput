# Developer setup: one relay, then native packages

This is a one-time developer task. The end users only extract and launch the resulting configured packages. Deploy and verify the relay before embedding its address in those packages.

## Deploy the relay

Use a small Linux host with Docker Compose, a persistent disk, and a public DNS hostname. Point that hostname at the host. Open host ports 80/443 for certificate issuance and WSS; do not expose port 8765. Select the region using measurements from **both** PCs as described in LATENCY.md. No hosting vendor purchase or specific region is assumed.

From this repository's root on the server:

```sh
export RELAY_DOMAIN=relay.your-domain.example
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml ps
```

Replace the example hostname with your actual DNS name. Caddy provisions and renews the public TLS certificate. The distributed address is `wss://YOUR-ACTUAL-DOMAIN/ws`. Client certificate and hostname verification are enabled. There is no insecure production switch.

The Python relay runs as a non-root user on a private Docker network. Only Caddy is exposed. Caddy replaces the client-supplied `X-Real-IP` header before forwarding it, so application rate limits use the actual client address. `--behind-proxy` must only be used behind this isolated trusted proxy. For direct TLS hosting, supply `--cert` and `--key` instead; do not set `--behind-proxy` on a publicly exposed plain websocket port.

Check the actual websocket route from both PCs, not just the HTTP health page. From the source checkout, first create the virtual environment described in README.md. On Windows:

```text
.venv\Scripts\python.exe -m remoteinput probe --relay wss://YOUR-ACTUAL-DOMAIN/ws --count 30
```

On macOS/Linux:

```sh
.venv/bin/python -m remoteinput probe --relay wss://YOUR-ACTUAL-DOMAIN/ws --count 30
```

The public diagnostic connection exposes websocket ping/pong timing only, expires after 60 seconds, and cannot register a session or forward input. Input connections require target ownership authentication or the target's pairing password.

The probe reports certificate/upgrade success and whether the Python relay accepted the diagnostic request. If it fails, follow [relay connection troubleshooting](TROUBLESHOOTING.md) to check DNS, TLS, the proxy route, and container status. A working `/health` page alone does not confirm that the Python relay is reachable.

## Keep identities persistent

Run **one relay process/replica** using the `registry` volume. It holds the AUTOINCREMENT registry, ownership-token digests, salted scrypt password hashes, password generations, and rotation IDs. IDs are unique within this persistent relay registry. Never replace or delete that volume during an upgrade. Do not use `docker compose down -v` for routine maintenance.

For a consistent backup, stop the relay briefly and snapshot/copy its whole volume, including SQLite WAL/SHM files if present; then start it again. Restore the entire backup to the same relay hostname. A stale database restore can roll back password revocations and allocations: after a disaster restore, rotate all target passwords before allowing use. Losing both database and backups prevents identity recovery; there is deliberately no unauthenticated reclaim API.

Upgrade by pulling the reviewed source and rerunning `docker compose ... up -d --build`; the volume remains. When the Caddyfile changes, use `docker compose -f deploy/compose.yaml up -d --build --force-recreate` to load the new bind-mounted configuration. Caddy restart/config changes can close WSS sessions; clients reconnect paused.

## Embed the address and build

Set the repository Actions variable **REMOTEINPUT_RELAY_URL** to the real `wss://.../ws` address, then run **Verify and package** in Actions. The address is public configuration, not a secret. The workflow builds Windows x64, Intel macOS and Apple Silicon macOS packages and uploads them as run artifacts. A missing variable produces visibly named setup-pending packages, never a fabricated working relay address.

Alternatively, create the isolated developer environment in README.md on each native build OS. Do not install the project's pinned dependencies into your shared Python environment.

Windows:

```text
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts/build.py --relay-url wss://YOUR-ACTUAL-DOMAIN/ws
```

macOS:

```sh
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
.venv/bin/python scripts/build.py --relay-url wss://YOUR-ACTUAL-DOMAIN/ws
```

PyInstaller bundles the Python runtime, websocket dependency and certifi CA bundle. This lets macOS validate public relay certificates without a developer Python installation or its OpenSSL trust files. Windows packages contain `.cmd` launchers and an `.exe`; macOS packages contain executable `.command` launchers and a native binary. Keep the entire extracted folder together. Native builds must be made on the destination OS/architecture; the Linux diagnostic build is not a Windows/macOS executable. [PyInstaller documents this platform dependency](https://pyinstaller.org/en/stable/operating-mode.html).

The build runs a packaged self-check and writes `BUILD-INFO.json`, a setup guide, and a SHA-256 checksum. The self-check imports the relevant native APIs and validates the protocol; it does not prove interactive desktop operation.

## Signing and release

Developer certificates were not supplied. Initial packages are unsigned Windows builds or ad-hoc signed macOS builds. For public distribution:

- Windows: sign the executable using your organization's Authenticode certificate, timestamp the signature, then rearchive the folder and regenerate the checksum.
- macOS: set `MACOS_SIGN_IDENTITY` to your Developer ID Application identity when building. Submit the signed distribution using Apple's notarization tools and use a suitable signed/notarized distribution container. Preserve a stable binary path/signing identity to avoid repeatedly changing Accessibility/Keychain trust.
- Run the manual acceptance checks in TESTING.md on clean machines after signing and before distributing the package. Upload verified packages as release assets through your normal release process.

This repository does not install a startup service or add unattended launch persistence. TLS protects each PC-to-relay connection; the relay is trusted and can see input in transit. It is not an end-to-end encrypted relay-blind design.
