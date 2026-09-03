"""Making VarAC transmit.  VarAC has no API, so this drives its Win32 GUI:
HamLink's broadcast automation, re-verified against VarAC V15.0.18 on
2026-09-02, plus an EXPERIMENTAL one-time-beacon trigger.

Verified facts for V15.0.18 (see `dump_windows` / `rehearse_broadcast`):
* The main-window BROADCAST button does NOT react to BM_CLICK; a posted
  WM_LBUTTONDOWN/WM_LBUTTONUP pair opens the dialog.  Dialog buttons accept
  either, so every click here posts mouse messages first and falls back to
  BM_CLICK.
* Dialog title 'Broadcast message'.  Controls: label 'TO:' beside a ComboBox
  (there are several ComboBoxes; pick the one on the TO: row), a WPF
  HwndWrapper message box that needs a real focus click + WM_CHAR, a byte
  counter label 'N/150 Bytes', buttons CLOSE / BROADCAST / BROADCAST AND CLOSE.
* Limit is 150 BYTES (non-ASCII may count 2).  HamLink's 81 was an older build.

Known costs: sending steals focus and moves the mouse pointer briefly; captions
are matched in English, so VarAC's InterfaceLanguage must be English.

Never call this without the interlocks in services/beacon.py.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("varmap.tx")

VARAC_WINDOW_RE = r"VarAC.*V\d+"
DIALOG_TITLE_RE = r"^Broadcast message"
BROADCAST_MAX_BYTES = 150

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_CHAR = 0x0102
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
BM_CLICK = 0x00F5
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002


def is_varac_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq VarAC.exe", "/NH"],
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "varac.exe" in r.stdout.lower()
    except Exception:
        return False


def _win32():
    try:
        import win32gui  # type: ignore
        import ctypes
        return win32gui, ctypes.windll.user32
    except ImportError:
        return None, None


def find_window_by_title(pattern: str):
    win32gui, _ = _win32()
    if not win32gui:
        return None
    found: List[int] = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and re.search(pattern, win32gui.GetWindowText(h), re.I):
                found.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def _children(win32gui, hwnd) -> List[int]:
    out: List[int] = []

    def cb(h, _):
        out.append(h)
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def _text(win32gui, h) -> str:
    try:
        return win32gui.GetWindowText(h)
    except Exception:
        return ""


def _child_by_text(win32gui, parent, text: str):
    for h in _children(win32gui, parent):
        if _text(win32gui, h) == text and win32gui.IsWindowVisible(h):
            return h
    return None


def _post_click(win32gui, user32, hwnd, right: bool = False) -> None:
    """Click a control by posting mouse messages at its centre (client coords)."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    lparam = ((max(b - t, 2) // 2) << 16) | (max(r - l, 2) // 2)
    if right:
        user32.PostMessageW(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, lparam)
        user32.PostMessageW(hwnd, WM_RBUTTONUP, 0, lparam)
    else:
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)


def _wait_window(pattern: str, seconds: float):
    deadline = time.time() + seconds
    while time.time() < deadline:
        h = find_window_by_title(pattern)
        if h:
            return h
        time.sleep(0.1)
    return None


def _wait_gone(hwnd, seconds: float) -> bool:
    win32gui, _ = _win32()
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return True
        except Exception:
            return True
        time.sleep(0.1)
    return False


def find_varac_window():
    return find_window_by_title(VARAC_WINDOW_RE)


BM_GETCHECK = 0x00F0


def varac_activity() -> Dict[str, Any]:
    """What VarAC is doing right now, read from its main window (read-only).

    VarAC disables BROADCAST, PING and CALL CQ while it is connected to a
    station (QSO, VMail exchange, ping, file transfer) and while the Broadcast
    window is open.  `busy` therefore means: do not hand VarAC anything now.
    """
    out: Dict[str, Any] = {"running": False, "broadcast_enabled": None, "ping_enabled": None,
                           "cq_enabled": None, "dialog_open": False, "busy": None, "reason": None}
    win32gui, user32 = _win32()
    if not win32gui:
        return out
    main = find_varac_window()
    if not main:
        out["reason"] = "VarAC window not found"
        return out
    out["running"] = True
    for h in _children(win32gui, main):
        try:
            if "BUTTON" not in win32gui.GetClassName(h).upper():
                continue
            t = _text(win32gui, h)
            if t == "BROADCAST":
                out["broadcast_enabled"] = bool(user32.IsWindowEnabled(h))
            elif t == "PING":
                out["ping_enabled"] = bool(user32.IsWindowEnabled(h))
            elif t == "CALL CQ":
                out["cq_enabled"] = bool(user32.IsWindowEnabled(h))
        except Exception:
            pass
    out["dialog_open"] = find_window_by_title(DIALOG_TITLE_RE) is not None
    if out["dialog_open"]:
        out["busy"], out["reason"] = True, "VarAC's Broadcast window is already open (operator composing a message?)"
    elif out["broadcast_enabled"] is False:
        out["busy"], out["reason"] = True, "VarAC is connected to a station (QSO, VMail, ping or file transfer in progress)"
    elif out["broadcast_enabled"] is True:
        out["busy"] = False
    else:
        out["reason"] = "BROADCAST button not found (non-English UI?)"
    return out


def ignore_dcd_state() -> Optional[bool]:
    """Read VarAC's main-window 'Ignore DCD' checkbox.

    True = ticked (VarAC will transmit even when the channel is busy),
    False = not ticked, None = cannot tell (VarAC not running, control not found).
    The checkbox is the BUTTON control on the same row, immediately left of the
    'Ignore DCD' label (verified on V15.0.18).  Read-only: BM_GETCHECK changes nothing.
    """
    win32gui, _ = _win32()
    if not win32gui:
        return None
    main = find_varac_window()
    if not main:
        return None
    kids = _children(win32gui, main)
    label = None
    for h in kids:
        if _text(win32gui, h).strip().lower() == "ignore dcd":
            label = h
            break
    if not label:
        return None
    ll, lt, lr, lb = win32gui.GetWindowRect(label)
    ly = (lt + lb) // 2
    best = None
    for h in kids:
        try:
            if "BUTTON" not in win32gui.GetClassName(h).upper():
                continue
            l, t, r, b = win32gui.GetWindowRect(h)
            if abs((t + b) // 2 - ly) <= 10 and r <= ll + 4 and (best is None or l > best[0]):
                best = (l, h)
        except Exception:
            pass
    if not best:
        return None
    try:
        return bool(win32gui.SendMessage(best[1], BM_GETCHECK, 0, 0) & 1)
    except Exception:
        return None


def dump_windows() -> Dict[str, Any]:
    """Diagnostics: every child of the VarAC main window with class, text and rect."""
    win32gui, _ = _win32()
    if not win32gui:
        return {"ok": False, "error": "pywin32 not installed (pip install pywin32)"}
    main = find_varac_window()
    if not main:
        return {"ok": False, "error": "VarAC window not found (is VarAC running?)"}
    rows = []
    for h in _children(win32gui, main):
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            rows.append({"hwnd": h, "class": win32gui.GetClassName(h), "text": _text(win32gui, h),
                         "visible": bool(win32gui.IsWindowVisible(h)), "rect": [l, t, r, b]})
        except Exception:
            pass
    return {"ok": True, "main_hwnd": main, "title": _text(win32gui, main), "children": rows}


# -- the broadcast dialog -----------------------------------------------------

def _open_dialog(win32gui, user32) -> Tuple[Optional[int], str]:
    main = find_varac_window()
    if not main:
        return None, "VarAC window not found (is VarAC running?)"
    if find_window_by_title(DIALOG_TITLE_RE):
        # Never type into a window the operator opened themselves.
        return None, "VarAC's Broadcast window is already open; not touching it"
    btn = _child_by_text(win32gui, main, "BROADCAST")
    if not btn:
        return None, "BROADCAST button not found on VarAC main window (English UI required)"
    if not user32.IsWindowEnabled(btn):
        return None, "VarAC is connected to a station (BROADCAST disabled); will retry when free"
    _post_click(win32gui, user32, btn)                       # works on V15.0.18
    d = _wait_window(DIALOG_TITLE_RE, 4.0)
    if not d:
        win32gui.SendMessage(btn, BM_CLICK, 0, 0)            # older builds
        d = _wait_window(DIALOG_TITLE_RE, 4.0)
    if not d:
        return None, "Broadcast dialog did not open"
    time.sleep(0.3)  # let the dialog finish creating its child controls
    return d, ""


def _dialog_controls(win32gui, dialog) -> Dict[str, Any]:
    """Locate the TO edit, the WPF message box, the byte counter and the buttons."""
    kids = _children(win32gui, dialog)
    info: Dict[str, Any] = {"to_edit": None, "msg": None, "counter": None,
                            "send_close": None, "send": None, "close": None}
    to_label = None
    for h in kids:
        t = _text(win32gui, h)
        if t == "TO:":
            to_label = h
        elif t == "BROADCAST AND CLOSE":
            info["send_close"] = h
        elif t == "BROADCAST":
            info["send"] = h
        elif t == "CLOSE":
            info["close"] = h
        elif re.match(r"^\d+/\d+ Bytes", t):
            info["counter"] = h
    # TO: the ComboBox on the same row as the 'TO:' label (there are several ComboBoxes)
    if to_label:
        ly = sum(win32gui.GetWindowRect(to_label)[1::2]) // 2
        best = None
        for h in kids:
            try:
                if "COMBOBOX" not in win32gui.GetClassName(h).upper():
                    continue
                l, t, r, b = win32gui.GetWindowRect(h)
                if abs((t + b) // 2 - ly) <= 15 and (best is None or l < best[0]):
                    best = (l, h)
            except Exception:
                pass
        if best:
            for sh in _children(win32gui, best[1]):
                if "Edit" in win32gui.GetClassName(sh):
                    info["to_edit"] = sh
                    break
    # MESSAGE: the WPF host
    for h in kids:
        try:
            if "HwndWrapper" in win32gui.GetClassName(h) and win32gui.IsWindowVisible(h):
                info["msg"] = h
                break
        except Exception:
            pass
    return info


def _counter_bytes(win32gui, counter) -> Optional[int]:
    m = re.match(r"^(\d+)/(\d+) Bytes", _text(win32gui, counter) if counter else "")
    return int(m.group(1)) if m else None


def _type_message(win32gui, user32, dialog, msg_hwnd, text: str) -> None:
    user32.SetForegroundWindow(dialog)
    time.sleep(0.2)
    l, t, r, b = win32gui.GetWindowRect(msg_hwnd)
    user32.SetCursorPos((l + r) // 2, (t + b) // 2)
    time.sleep(0.1)
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.2)
    for ch in text:
        user32.SendMessageW(msg_hwnd, WM_CHAR, ord(ch), 0)
    time.sleep(0.3)


def _drive_broadcast(message: str, to: str, send: bool) -> Dict[str, Any]:
    """Shared by send_broadcast (send=True) and rehearse_broadcast (send=False)."""
    result: Dict[str, Any] = {"ok": False, "error": "", "typed_bytes": None, "sent": False}
    if sys.platform != "win32":
        result["error"] = "VarAC GUI automation is Windows-only"
        return result
    win32gui, user32 = _win32()
    if not win32gui:
        result["error"] = "pywin32 not installed (pip install pywin32)"
        return result
    msg_text = (message or "").strip()
    if not msg_text:
        result["error"] = "empty message"
        return result
    if len(msg_text.encode("utf-8")) > BROADCAST_MAX_BYTES:
        msg_text = msg_text.encode("utf-8")[:BROADCAST_MAX_BYTES].decode("utf-8", errors="ignore")

    dialog, err = _open_dialog(win32gui, user32)
    if not dialog:
        result["error"] = err
        return result

    def close_dialog() -> None:
        c = _child_by_text(win32gui, dialog, "CLOSE")
        if c:
            _post_click(win32gui, user32, c)
            if not _wait_gone(dialog, 2.0):
                win32gui.SendMessage(c, BM_CLICK, 0, 0)

    try:
        ctl = _dialog_controls(win32gui, dialog)
        result["controls"] = {k: bool(v) for k, v in ctl.items()}
        if ctl["to_edit"]:
            user32.SendMessageW(ctl["to_edit"], WM_SETTEXT, 0, to)
        else:
            log.warning("Broadcast: TO field not found; VarAC's default recipient will be used")
        if not ctl["msg"]:
            close_dialog()
            result["error"] = "MESSAGE field not found in broadcast dialog"
            return result

        _type_message(win32gui, user32, dialog, ctl["msg"], msg_text)
        typed = _counter_bytes(win32gui, ctl["counter"])
        if typed == 0:  # focus click missed: try once more
            _type_message(win32gui, user32, dialog, ctl["msg"], msg_text)
            typed = _counter_bytes(win32gui, ctl["counter"])
        result["typed_bytes"] = typed
        expected = len(msg_text.encode("utf-8"))
        if typed is not None and typed != expected:
            close_dialog()
            result["error"] = f"message did not land correctly (dialog counts {typed} bytes, expected {expected})"
            return result

        if not send:
            close_dialog()
            result["ok"] = True
            return result

        btn = ctl["send_close"] or ctl["send"]
        if not btn:
            close_dialog()
            result["error"] = "BROADCAST button not found in dialog"
            return result
        _post_click(win32gui, user32, btn)
        if not _wait_gone(dialog, 3.0):
            win32gui.SendMessage(btn, BM_CLICK, 0, 0)
            if not _wait_gone(dialog, 3.0):
                # A 'too long' or similar message box may be up; do not leave the dialog open.
                close_dialog()
                result["error"] = "dialog did not close after BROADCAST AND CLOSE (VarAC may have refused the message)"
                return result
        result["ok"] = True
        result["sent"] = True
        log.info("VarAC broadcast sent: TO=%s MSG=%s", to, msg_text)
        return result
    except Exception as e:
        close_dialog()
        result["error"] = f"automation failed: {e}"
        return result


def send_broadcast(message: str, to: str = "ALL") -> Tuple[bool, str]:
    """Open VarAC's Broadcast dialog, fill it, click BROADCAST AND CLOSE.  Returns (ok, error)."""
    r = _drive_broadcast(message, to, send=True)
    return bool(r["ok"]), r.get("error", "")


def rehearse_broadcast(message: str, to: str = "ALL") -> Dict[str, Any]:
    """Everything except the final click: opens the dialog, fills TO and MESSAGE,
    verifies the byte counter, then CLOSES it.  Nothing is transmitted."""
    return _drive_broadcast(message, to, send=False)


def send_one_time_beacon() -> Tuple[bool, str]:
    """EXPERIMENTAL.  VarAC: 'Right-click the Beacon button to send a single,
    one-time beacon.'  Posts a right-click on the SEND BEACONS button.
    Verify via VarAC.log ('Sending one-time beacon')."""
    if sys.platform != "win32":
        return False, "Windows-only"
    win32gui, user32 = _win32()
    if not win32gui:
        return False, "pywin32 not installed"
    main = find_varac_window()
    if not main:
        return False, "VarAC window not found"
    target = None
    for h in _children(win32gui, main):
        try:
            txt = _text(win32gui, h)
            if (win32gui.IsWindowVisible(h) and "BUTTON" in win32gui.GetClassName(h).upper()
                    and re.search(r"BEACON", txt, re.I) and "BROADCAST" not in txt.upper()):
                target = h
                break
        except Exception:
            pass
    if not target:
        return False, "Beacon button not found (try --dump-windows)"
    _post_click(win32gui, user32, target, right=True)
    time.sleep(1.0)
    busy = find_window_by_title(r"^Frequency is busy")
    if busy:
        no = _child_by_text(win32gui, busy, "No") or _child_by_text(win32gui, busy, "&No")
        if no:
            _post_click(win32gui, user32, no)
        return False, "frequency busy; VarAC asked to confirm and we declined"
    return True, ""
