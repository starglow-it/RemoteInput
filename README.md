# RemoteInput

Control a Windows or macOS computer's mouse and keyboard from Windows 11. RemoteInput runs in a console and does not capture screens. Use your existing screen feed separately.

**Status:** source implementation with automated protocol, safety, and TLS integration tests. A hosted relay address has not been supplied or deployed. Packages built without `REMOTEINPUT_RELAY_URL` are explicitly marked **setup-pending** and cannot pair until the developer rebuilds them with the deployed address. Interactive Windows/macOS behavior still needs the checks in [TESTING.md](docs/TESTING.md).

- [Everyday setup](docs/SETUP.md)
- [One-time relay deployment and executable builds](docs/DEPLOY.md)
- [Checks and platform limits](docs/TESTING.md)
- [Latency measurements and relay selection](docs/LATENCY.md)
- [Protocol and safety design](docs/DESIGN.md)
- [Build workflow and downloadable artifacts](https://github.com/starglow-it/RemoteInput/actions/workflows/build.yml)

## Everyday use

1. Extract the package and run **Start Target** on the computer to control. Approve macOS Accessibility if requested.
2. Run **Start Controller** on Windows. Enter the target ID and password shown on the target console once.
3. The connection starts **Paused**. **Ctrl+Alt+F9** toggles control; **Ctrl+Alt+F10** stops control. Reconnections stay paused.

Target IDs are assigned sequentially by the relay. Each target generates its own random password and separate ownership secret. Windows DPAPI and macOS Keychain retain credentials. **P + Enter** in the target console changes its password and revokes existing access. **F + Enter** in the paused controller forgets its saved target.

## Development

Requires Python 3.12+ on developer machines only:

```sh
python -m pip install -e ".[test,build]"
python -m pytest -q
python -m ruff check src tests scripts
python scripts/benchmark.py --output docs/latency-local.json
python -m remoteinput self-check
```

Build native packages on Windows, Intel macOS, and Apple Silicon macOS. End users need no Python, VPN, or config-file editing. See [DEPLOY.md](docs/DEPLOY.md) for the developer's deployment and embedding step.

