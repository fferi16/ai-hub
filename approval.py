"""Feri jóváhagyása `danger` szinthez: Windows-értesítés Igen/Nem gombbal.
Időtúllépés, bezárás vagy hiba → Nem. Tartalék: időkorlátos felugró ablak."""
import ctypes
import logging
import threading

log = logging.getLogger("hub.approval")

MB_YESNO, MB_ICONWARNING, MB_TOPMOST, MB_SETFOREGROUND = 0x4, 0x30, 0x40000, 0x10000
IDYES = 6


def _toast(title: str, message: str, timeout_s: int) -> bool:
    from windows_toasts import InteractableWindowsToaster, Toast, ToastButton, ToastDuration, ToastScenario

    done = threading.Event()
    answer = {"yes": False}

    def on_activated(args):
        arg = getattr(args, "arguments", None)
        log.info("Értesítés válasz: %r", arg)
        answer["yes"] = arg == "yes"
        done.set()

    toaster = InteractableWindowsToaster("Hub")
    toast = Toast([title, message], duration=ToastDuration.Long, scenario=ToastScenario.Reminder)
    toast.AddAction(ToastButton("Igen", "yes"))
    toast.AddAction(ToastButton("Nem", "no"))
    toast.on_activated = on_activated
    toast.on_dismissed = lambda a: (log.info("Értesítés bezárva: %r", getattr(a, "reason", a)), done.set())
    toast.on_failed = lambda _: done.set()
    toaster.show_toast(toast)
    if not done.wait(timeout_s):
        log.info("Jóváhagyás időtúllépés → Nem")
    try:
        toaster.remove_toast(toast)
    except Exception:
        pass
    return answer["yes"]


def _messagebox(title: str, message: str, timeout_s: int) -> bool:
    fn = ctypes.windll.user32.MessageBoxTimeoutW
    flags = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
    return fn(None, message, title, flags, 0, timeout_s * 1000) == IDYES


def ask(title: str, message: str, timeout_s: int = 120) -> bool:
    try:
        return _toast(title, message, timeout_s)
    except Exception:
        log.exception("Értesítés nem sikerült, tartalék ablak")
        try:
            return _messagebox(title, message, timeout_s)
        except Exception:
            log.exception("Jóváhagyás nem kérhető → Nem")
            return False
