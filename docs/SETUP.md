# RemoteInput setup

The configured packages include the relay address. Extract the package and use its launchers; you do not need to enter a server address. Keep the Ubuntu relay running while using RemoteInput. Older packages whose names end in `setup-pending` are validation builds without an embedded relay address.

## First use

1. Extract the entire package. Keep the executable and `_internal` directory together.
2. On the target computer, open **Start Target.cmd** (Windows) or **Start Target.command** (Mac).
3. On Mac, approve Accessibility for the app or the Terminal host shown in System Settings. Return to the same console and press Enter. RemoteInput confirms permission before accepting input. It does not need Screen Recording permission.
4. Copy the ID and unique password from that console. On your Windows controller, open **Start Controller.cmd** and enter them. The password entry is hidden.
5. Wait for **Connected / Paused**, then press **Ctrl+Alt+F9**. Release the hotkey keys before typing. **Controlling** means local mouse and keyboard input is being sent to the target.

No Python, Tailscale, VPN, inbound PC port, or config-file editing is needed. Both computers make outbound TLS connections on port 443 to the developer's relay.

## Everyday controls

| Action | How |
|---|---|
| Toggle control | Ctrl+Alt+F9 on the controller |
| Stop control | Ctrl+Alt+F10 on the controller |
| Change target password | Type P and Enter in the target console; the ID stays the same |
| Forget saved target | While paused, type F and Enter in the controller, or run Forget Target |
| See connection measurements | While paused, type M and Enter in the controller |
| Measure this PC's route to the relay | Run Measure Relay on either PC |
| Exit | While paused, type Q and Enter or close the console |

The target's own mouse and keyboard remain available. Start Target does not hide itself, create a background service, or install itself at login. Launching it from an existing terminal reuses that terminal. Double-clicking a launcher opens the ordinary console. Only one target instance and one controller instance per user/relay can run at a time.

Your controller remembers its last target in OS-protected storage. Both clients reconnect automatically after network interruptions. Reconnection never resumes control automatically: press Ctrl+Alt+F9 again. If a password was changed, rejected saved credentials are removed; restart Start Controller and enter the new password.

## Windows to Mac keys

| Windows controller key | Mac target key |
|---|---|
| Left Ctrl | Command; use for copy/paste/save/find |
| Right Ctrl | Control; use for terminal Control shortcuts |
| Windows key | Control |
| Alt | Option |
| Backspace / Delete | Delete backward / Delete forward |

Text follows the target's keyboard layout. Use matching layouts for predictable punctuation and letters. IME composition, arbitrary Unicode text transfer, media keys, protected desktops, and application-specific shortcuts are not certified. Ctrl+Alt+F9/F10 are reserved locally.

## If something needs attention

- **Setup-pending:** ask the developer for the configured build described in DEPLOY.md.
- **Target offline:** keep Start Target open on an unlocked computer and check its connection.
- **Target busy:** close the other controller; only one controller may access a target.
- **Rate limited:** wait a minute before retrying credentials.
- **Paused after lag:** release your physical keys/buttons, check connectivity, then reactivate. Old events are discarded with an explicit release/reset.
- **macOS permission changed:** restore Accessibility and restart Start Target if macOS requires it. Do not grant Screen Recording for this app.
- **Windows elevated app or UAC:** interact with secure prompts locally. The default app has ordinary user privileges and cannot control applications at a higher integrity level. An intentional administrator launch may control elevated ordinary apps but does not bypass UAC or sign-in screens.
- **Unsigned build:** Windows or macOS may show the normal publisher/security approval. Public distribution should use the signing/notarization steps in DEPLOY.md; never disable the OS security system.

Keep the target console private: it intentionally displays the pairing password. RemoteInput does not write it or typed input to logs.
