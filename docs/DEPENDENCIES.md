# Recover from a shared Python dependency conflict

An install can finish with `Successfully installed` while leaving another package's requirements unsatisfied. For example, RemoteInput requires `websockets==16.0`, while `frida-tools==14.9.0` requires `websockets>=13.0.0,<14.0.0`. They cannot share one compatible installed version. Keep RemoteInput in its own virtual environment; do not downgrade its websocket dependency or use `--no-deps` to hide the conflict.

## Windows recovery

Run these commands from the RemoteInput source directory in the same ordinary Command Prompt used for the original install. Do not activate a virtual environment for the base-environment repair commands below.

First install RemoteInput in an isolated environment:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test,build]"
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m remoteinput self-check
```

Continue after the install and checks succeed. Remove the RemoteInput package registration from the original shared interpreter, then restore the other tool's compatible websocket version:

```bat
python -m pip uninstall -y remoteinput
python -m pip install "websockets==13.1"
python -m pip check
```

Uninstalling an editable registration does not delete the source checkout or the separate `.venv` installation. These commands target different environments because the RemoteInput commands use `.venv\Scripts\python.exe` explicitly. If the original install also replaced other packages, its log shows their previous versions; restore those specific versions only in the original shared interpreter as needed. A full environment rollback requires a prior environment snapshot.

`pip check` should report `No broken requirements found.` in each environment. If it reports a different conflict, inspect that conflict before changing more packages. Do not apply the `websockets==13.1` repair inside RemoteInput's `.venv`.

For future RemoteInput use, always run its commands through the project interpreter:

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m remoteinput self-check
```

The pip upgrade notice is separate from this dependency conflict. Upgrading pip alone does not make incompatible package requirements compatible. Relay deployment is also separate; a successful Python install does not deploy or configure a hosted relay.

References: [Python virtual environments](https://docs.python.org/3.13/library/venv.html), [pip dependency checks](https://pip.pypa.io/en/stable/cli/pip_check/).
