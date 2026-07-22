"""
HTTP connection layer for the s&box Bridge v2.
State machine with auto-reconnect, session tracking, and latency monitoring.
Uses Python stdlib only (http.client, json).
"""

import http.client
import json
import time
import traceback

import bpy


# ── Connection States ─────────────────────────────────────────────────────

DISCONNECTED = 0
CONNECTED = 1
RECONNECTING = 2

# ── Module State ──────────────────────────────────────────────────────────

_state = DISCONNECTED
_host = "localhost"
_port = 8099
_session_id = None
_consecutive_failures = 0
_reconnect_attempt = 0
_reconnect_timer_registered = False
_last_poll_latency_ms = 0.0

# Heartbeat timestamps for the panel's trust line. Stamped on SUCCESS only —
# the panel computes ages at draw time, so a dead timer shows a growing
# "recv Ns ago" number instead of a lying green dot.
_last_send_ok_time = 0.0
_last_recv_ok_time = 0.0

_MAX_FAILURES = 3
# Reconnect retries forever (backoff capped at 10s). The old 5-attempt
# give-up rendered as plain "Disconnected" — indistinguishable from
# never-tried — and engine hot-compiles routinely outlast the ~75s budget.
_RECONNECT_MAX_DELAY = 10.0


def notify_ui():
    """Tag every 3D viewport for redraw. Call ONLY on state transitions —
    the N-panel cannot repaint itself from timers otherwise, which is the
    mechanical root of 'panel says Connected long after the engine died'."""
    try:
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def get_heartbeats():
    """(last_send_ok, last_recv_ok) unix timestamps, 0.0 = never."""
    return (_last_send_ok_time, _last_recv_ok_time)


# ── Public Accessors ──────────────────────────────────────────────────────

def is_connected():
    return _state == CONNECTED


def is_reconnecting():
    return _state == RECONNECTING


def get_state():
    return _state


def get_session_id():
    return _session_id


def get_latency_ms():
    return _last_poll_latency_ms


def get_reconnect_attempt():
    return _reconnect_attempt


# ── Connect / Disconnect ─────────────────────────────────────────────────

def connect(host="localhost", port=8099):
    """Test connection to the s&box bridge server.
    Returns (success: bool, session_id: str or None)."""
    global _state, _host, _port, _session_id, _consecutive_failures, _reconnect_attempt

    if _state != DISCONNECTED:
        disconnect()

    _host = host
    _port = port
    _consecutive_failures = 0
    _reconnect_attempt = 0

    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/status")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()

        if resp.status != 200:
            print(f"[s&box Bridge] Server returned status {resp.status}")
            return (False, None)

        data = json.loads(body)
        _session_id = data.get("sessionId")
        print(f"[s&box Bridge] Connected! Session: {_session_id}")
        _state = CONNECTED
        notify_ui()
        return (True, _session_id)

    except Exception as e:
        print(f"[s&box Bridge] Connection failed: {e}")
        traceback.print_exc()
        return (False, None)


def disconnect():
    """Cleanly disconnect and stop any reconnect timers."""
    global _state, _consecutive_failures, _reconnect_attempt
    _stop_reconnect_timer()
    _state = DISCONNECTED
    _consecutive_failures = 0
    _reconnect_attempt = 0
    print("[s&box Bridge] Disconnected.")
    notify_ui()


# ── Send (Blender → s&box via POST /message) ─────────────────────────────

_last_engine_error = (None, 0.0)    # (text, time) — throttles repeat rejections


def _surface_engine_error(sent_json, body):
    """The server answers dispatch failures with HTTP 200 + {"error": ...}.
    Without reading the body, a rejected message (stale bridge id, dispatcher
    exception) is indistinguishable from success on the Blender side — moves
    silently stop arriving. Surface it in the console and panel warnings."""
    global _last_engine_error
    try:
        data = json.loads(body)
        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            return
        try:
            mtype = json.loads(sent_json).get("type", "?")
        except Exception:
            mtype = "?"
        # Stale scene link: the engine no longer has this GameObject
        # (deleted, other scene/tab, previous session). Quarantine the id so
        # we stop hammering it on every move — sync.py clears the quarantine
        # on the next sync_response / session change.
        if mtype == "update_scene_transform" and (
                "not found" in err or "invalid sceneId" in err):
            try:
                sid = json.loads(sent_json).get("sceneId")
                if sid:
                    from . import sync
                    sync.quarantine_scene_id(sid)
            except Exception:
                pass
        else:
            # Mark the rejected object 'failed' so the panel's problems-first
            # list names it, instead of the error living only in warnings.
            try:
                bid = json.loads(sent_json).get("bridgeId")
                if bid:
                    from . import sync
                    obj = sync.find_by_bridge_id(bid)
                    if obj is not None:
                        sync.set_sync_status(obj, "failed")
            except Exception:
                pass

        text = f"engine rejected '{mtype}': {err}"
        now = time.time()
        if _last_engine_error[0] != text or now - _last_engine_error[1] > 5.0:
            _last_engine_error = (text, now)
            print(f"[s&box Bridge] {text}")
            try:
                from . import sync
                sync.add_warning(text)
            except Exception:
                pass
    except Exception:
        pass


def _encode_message(message):
    """dict -> JSON string, or None if it contains NaN/inf. Raising here used
    to unwind the depsgraph handler (traceback on every move with a degenerate
    transform) — drop the one message loudly instead."""
    if not isinstance(message, dict):
        return message
    try:
        return json.dumps(message, allow_nan=False, default=str)
    except ValueError as e:
        print(f"[s&box Bridge] Dropped '{message.get('type', '?')}' message "
              f"with non-finite floats: {e}")
        return None


def send(message):
    """Send a JSON message to s&box. Returns True on success.
    The message dict should already contain seq/ack fields."""
    global _consecutive_failures

    if _state != CONNECTED:
        return False

    message = _encode_message(message)
    if message is None:
        return False

    try:
        conn = http.client.HTTPConnection(_host, _port, timeout=5)
        conn.request(
            "POST", "/message",
            body=message,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()

        if resp.status != 200:
            print(f"[s&box Bridge] Send rejected with HTTP {resp.status}")
            _consecutive_failures += 1
            _check_auto_reconnect()
            return False

        _consecutive_failures = 0
        global _last_send_ok_time
        _last_send_ok_time = time.time()
        _surface_engine_error(message, body)
        return True

    except Exception as e:
        print(f"[s&box Bridge] Send error: {e}")
        _consecutive_failures += 1
        _check_auto_reconnect()
        return False


def send_and_receive(message):
    """Send a JSON message and return the parsed response body.
    Used for create messages where s&box returns the assigned bridge ID."""
    global _consecutive_failures

    if _state != CONNECTED:
        return None

    message = _encode_message(message)
    if message is None:
        return None

    try:
        conn = http.client.HTTPConnection(_host, _port, timeout=5)
        conn.request(
            "POST", "/message",
            body=message,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()

        if resp.status != 200:
            _consecutive_failures += 1
            _check_auto_reconnect()
            return None

        _consecutive_failures = 0
        global _last_send_ok_time
        _last_send_ok_time = time.time()
        return json.loads(body)

    except Exception as e:
        print(f"[s&box Bridge] send_and_receive error: {e}")
        _consecutive_failures += 1
        _check_auto_reconnect()
        return None


# ── Poll (s&box → Blender via GET /poll) ──────────────────────────────────

def poll():
    """Poll for messages from s&box.
    Returns the full response dict: {sessionId, sboxSeq, messages} or None on error."""
    global _consecutive_failures, _last_poll_latency_ms

    if _state != CONNECTED:
        return None

    try:
        start = time.time()
        conn = http.client.HTTPConnection(_host, _port, timeout=2)
        conn.request("GET", "/poll")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        _last_poll_latency_ms = (time.time() - start) * 1000.0

        if resp.status != 200:
            _consecutive_failures += 1
            _check_auto_reconnect()
            return None

        _consecutive_failures = 0
        global _last_recv_ok_time
        _last_recv_ok_time = time.time()
        data = json.loads(body)
        if isinstance(data, dict):
            return data
        # Legacy format: bare array → wrap it
        if isinstance(data, list):
            return {"sessionId": _session_id, "sboxSeq": 0, "messages": data}
        return None

    except Exception as e:
        print(f"[s&box Bridge] Poll error: {e}")
        _consecutive_failures += 1
        _check_auto_reconnect()
        return None


# ── Auto-Reconnect ────────────────────────────────────────────────────────

def _check_auto_reconnect():
    """Transition to RECONNECTING after too many consecutive failures."""
    global _state

    if _consecutive_failures < _MAX_FAILURES:
        return

    if _state == RECONNECTING:
        return  # Already reconnecting

    # Check if auto-reconnect is enabled
    try:
        settings = bpy.context.scene.sbox_bridge
        if not settings.auto_reconnect:
            print(f"[s&box Bridge] Lost connection (auto-reconnect disabled).")
            _state = DISCONNECTED
            return
    except Exception:
        pass

    print(f"[s&box Bridge] Lost connection — attempting auto-reconnect...")
    _state = RECONNECTING
    _start_reconnect_timer()
    notify_ui()


def _start_reconnect_timer():
    """Register a Blender timer for reconnection attempts."""
    global _reconnect_timer_registered, _reconnect_attempt
    _reconnect_attempt = 0
    if not _reconnect_timer_registered:
        bpy.app.timers.register(_attempt_reconnect, first_interval=1.0)
        _reconnect_timer_registered = True


def _stop_reconnect_timer():
    """Unregister the reconnect timer."""
    global _reconnect_timer_registered
    if _reconnect_timer_registered:
        try:
            bpy.app.timers.unregister(_attempt_reconnect)
        except Exception:
            pass
        _reconnect_timer_registered = False


def ensure_reconnect_timer():
    """Watchdog hook: resurrect the reconnect timer if it died. The retry
    organ is an unguarded bpy timer — the same organ class that silently
    died in the poll loop once. Called from the poll tick while RECONNECTING."""
    global _reconnect_timer_registered
    if _state != RECONNECTING:
        return
    try:
        if not bpy.app.timers.is_registered(_attempt_reconnect):
            _reconnect_timer_registered = False
            _start_reconnect_timer()
            print("[s&box Bridge] Reconnect timer was dead — restarted")
    except Exception:
        pass


def _attempt_reconnect():
    """Timer callback: try to reconnect. Retries FOREVER with backoff capped
    at _RECONNECT_MAX_DELAY — engine hot-compiles and editor restarts
    routinely outlast any finite budget, and a silent give-up is
    indistinguishable from never-having-tried. Manual Disconnect is the only
    way to stop. Body is fully contained so an exception can't kill the
    retry organ itself."""
    global _state, _reconnect_attempt, _consecutive_failures, _session_id, \
        _reconnect_timer_registered

    try:
        if _state != RECONNECTING:
            _reconnect_timer_registered = False
            return None  # Stop timer

        _reconnect_attempt += 1
        print(f"[s&box Bridge] Reconnect attempt {_reconnect_attempt}...")
        notify_ui()

        try:
            conn = http.client.HTTPConnection(_host, _port, timeout=3)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            conn.close()

            if resp.status == 200:
                data = json.loads(body)
                _session_id = data.get("sessionId")
                _state = CONNECTED
                _consecutive_failures = 0
                _reconnect_attempt = 0
                print(f"[s&box Bridge] Reconnected! Session: {_session_id}")
                _reconnect_timer_registered = False
                notify_ui()

                # Restart the sync timer and trigger reconciliation
                try:
                    from . import sync
                    sync.start_timer()
                    sync.send_sync()
                except Exception:
                    pass

                return None  # Stop timer

        except Exception as e:
            print(f"[s&box Bridge] Reconnect failed: {e}")

        # Exponential backoff capped low — this loops forever, so the cap is
        # the steady-state retry period, not a countdown to giving up.
        try:
            base = bpy.context.scene.sbox_bridge.reconnect_interval
        except Exception:
            base = 3.0
        return min(base * (2 ** (_reconnect_attempt - 1)), _RECONNECT_MAX_DELAY)

    except Exception as e:
        print(f"[s&box Bridge] Reconnect tick error (contained): {e}")
        return _RECONNECT_MAX_DELAY
