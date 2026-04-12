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

_MAX_FAILURES = 3
_MAX_RECONNECT_ATTEMPTS = 5


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


# ── Send (Blender → s&box via POST /message) ─────────────────────────────

def send(message):
    """Send a JSON message to s&box. Returns True on success.
    The message dict should already contain seq/ack fields."""
    global _consecutive_failures

    if _state != CONNECTED:
        return False

    if isinstance(message, dict):
        message = json.dumps(message, allow_nan=False, default=str)

    try:
        conn = http.client.HTTPConnection(_host, _port, timeout=5)
        conn.request(
            "POST", "/message",
            body=message,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()

        if resp.status != 200:
            _consecutive_failures += 1
            _check_auto_reconnect()
            return False

        _consecutive_failures = 0
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

    if isinstance(message, dict):
        message = json.dumps(message, allow_nan=False, default=str)

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


def _attempt_reconnect():
    """Timer callback: try to reconnect with exponential backoff."""
    global _state, _reconnect_attempt, _consecutive_failures, _session_id, _reconnect_timer_registered

    if _state != RECONNECTING:
        _reconnect_timer_registered = False
        return None  # Stop timer

    _reconnect_attempt += 1

    if _reconnect_attempt > _MAX_RECONNECT_ATTEMPTS:
        print(f"[s&box Bridge] Gave up after {_MAX_RECONNECT_ATTEMPTS} reconnect attempts.")
        _state = DISCONNECTED
        _reconnect_timer_registered = False
        return None  # Stop timer

    print(f"[s&box Bridge] Reconnect attempt {_reconnect_attempt}/{_MAX_RECONNECT_ATTEMPTS}...")

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

    # Exponential backoff: base_interval * 2^attempt, max 30s
    try:
        base = bpy.context.scene.sbox_bridge.reconnect_interval
    except Exception:
        base = 3.0
    delay = min(base * (2 ** (_reconnect_attempt - 1)), 30.0)
    return delay  # Schedule next attempt
