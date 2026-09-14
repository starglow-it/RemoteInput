import sys


def target_injector():
    if sys.platform == "win32":
        from .windows import WindowsInjector
        return WindowsInjector()
    if sys.platform == "darwin":
        from .macos import MacInjector
        return MacInjector()
    raise RuntimeError("Start Target supports Windows 11 and macOS only.")


def controller_capture(emit, toggle, stop):
    if sys.platform != "win32":
        raise RuntimeError("Start Controller requires Windows 11.")
    from .windows import WindowsCapture
    return WindowsCapture(emit, toggle, stop)

