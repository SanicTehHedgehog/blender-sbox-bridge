"""
Bidirectional sync logic for the s&box Bridge v2.

Key changes from v1:
  - Sequence-based echo prevention (no time-based cooldowns)
  - Direct object creation (no deferred timers)
  - Bidirectional light sync
  - Chunked mesh transfer for large objects
  - Feature filtering for unsupported types
  - Material hash caching
  - Pending delete confirmation
  - Session-based reconnection

Protocol: Blender -> s&box messages include seq/ack for echo suppression.
Coordinate conversion: Blender (X-right, Y-forward, Z-up) <-> s&box (X-forward, Y-left, Z-up).
All conversion happens here. s&box does direct passthrough.
"""

import bpy
from bpy.app.handlers import persistent
import bmesh
import hashlib
import json
import math
import os
import random
import shutil
import time
from mathutils import Euler, Matrix
from . import connection


# ── Module State ──────────────────────────────────────────────────────────

_suppress_depsgraph = False
_timer_running = False
_remote_update_times = {}       # bridgeId -> time.time() when last updated from s&box

# Sequence-based echo prevention
_blender_seq = 0
_last_sbox_seq_processed = 0
_current_session_id = None
_last_write_seq = {}            # bridgeId -> our seq when we last wrote

# Object tracking
_last_known_bridge_ids = set()
_last_transform_send = {}       # bridgeId -> time.time() of last transform send
_mesh_debounce_obj = {}         # bridgeId -> obj reference
_mesh_debounce_scheduled = set()
_last_scale = {}                # bridgeId -> (sx, sy, sz)
_hidden_bridge_ids = set()      # bridgeIds currently hidden in Blender (disabled in s&box)

# Material caching
_material_hash_cache = {}       # material_name -> (content_hash, vmat_rel_path)

# Warnings & pending deletes
_warnings = []                  # [(timestamp, message)]
_pending_deletes = []           # [(bridgeId, timestamp)]

# Deferred outbound (dirty-set). The depsgraph handler only MARKS objects
# here; the poll timer drains them via _flush_dirty_sends(). Doing network
# I/O inside the handler was a viewport-freeze hazard (synchronous POST with
# a 5s timeout, per object, at up to 20Hz during drags) and a re-entrancy
# amplifier. Coalescing is inherent: we store the object, not a transform
# snapshot, and read its freshest state once per flush — a fast drag
# collapses to a single send instead of queuing stale transforms.
_dirty_sends = {}               # key -> (kind, obj); kind in
                                # {transform, light, scene, create, create_light}
_first_dirty_time = None        # when the dirty-set went non-empty; canary for
                                # a stalled flush (see get_degraded_reason)

# Scene IDs the engine has rejected as gone (deleted engine-side, different
# scene/tab, or a previous session). Quarantined so we stop hammering
# update_scene_transform on every move; cleared on sync_response / session
# change so a refreshed link resumes automatically.
_dead_scene_ids = set()

# Chunked mesh state
_chunked_streams = {}           # bridgeId -> stream state dict

# Play mode
_play_mode_active = False

# Set when we detect an engine session change, consumed by the next
# sync_response: a fresh engine session that reports zero bridge objects
# means "engine lost everything" — repopulate it from Blender instead of
# leaving every local object holding a dead ID.
_expect_fresh_engine = False

# Per-Blender-session nonce for idempotency keys. Blender's session_uid
# counter restarts every launch, so "name_uid" alone collides with keys the
# engine remembers from a previous Blender session — HandleCreate would then
# bind the new object to a stale engine object instead of creating one.
_session_nonce = "%08x" % random.getrandbits(32)

# Constants
CHUNK_VERTEX_LIMIT = 20000
TRANSFORM_SEND_INTERVAL = 0.05  # 20Hz max
MESH_DEBOUNCE_INTERVAL = 0.15
# 30s, not 5: five seconds is shorter than the time it takes to notice the
# countdown exists — during undo storms objects were destroyed engine-side
# before the user ever saw the pending queue.
PENDING_DELETE_TIMEOUT = 30.0
# More than this many simultaneous pending deletes never auto-confirm — a
# mass delete is either intentional (one click is cheap) or an accident
# (auto-confirm would be catastrophic).
PENDING_DELETE_BULK_GATE = 5

# Feature filtering
SYNCABLE_TYPES = {"MESH", "LIGHT"}
CONVERTIBLE_TYPES = {"CURVE", "SURFACE", "META", "FONT"}
UNSUPPORTED_TYPES = {"ARMATURE", "LATTICE", "GPENCIL", "GREASEPENCIL", "SPEAKER", "CAMERA"}
UNSUPPORTED_LIGHT_TYPES = {"AREA"}


# ── Coordinate Conversion ────────────────────────────────────────────────

def blender_to_sbox_pos(bx, by, bz):
    """Blender -> s&box: sbox = (blender.Y, -blender.X, blender.Z)"""
    return (by, -bx, bz)


def sbox_to_blender_pos(sx, sy, sz):
    """s&box -> Blender: blender = (-sbox.Y, sbox.X, sbox.Z)"""
    return (-sy, sx, sz)


# The position maps above are a +90° rotation about Z (and its inverse).
# Rotations must ride through the SAME change of basis: R_blender =
# AXIS_S2B @ R_sbox @ AXIS_B2S. Copying euler components across axes
# (the old scheme) is only correct for pure-yaw rotations — a Blender
# X=90° wall got pitch +90 where -90 is correct and stood upside down
# in s&box, offset to the wrong side of its origin.
_AXIS_S2B = Matrix.Rotation(math.radians(90.0), 3, 'Z')
_AXIS_B2S = _AXIS_S2B.transposed()


def _sbox_rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    """Wire pitch/yaw/roll -> Blender-frame 3x3 rotation.

    s&box composes Rz(yaw) @ Ry(pitch) @ Rx(roll), which is exactly
    Blender's 'XYZ' euler order with (x=roll, y=pitch, z=yaw). Building
    the matrix first (instead of writing euler components) also makes the
    result immune to the engine re-canonicalizing angle triples on echo:
    equivalent triples produce the same matrix.
    """
    r_s = Euler((
        math.radians(roll_deg),
        math.radians(pitch_deg),
        math.radians(yaw_deg),
    ), 'XYZ').to_matrix()
    return _AXIS_S2B @ r_s @ _AXIS_B2S


# ── Bridge ID Helpers ────────────────────────────────────────────────────

# Bridge ID is mirrored on obj.data so it survives undo/redo: Blender's undo
# stack restores mesh data more reliably than object custom-prop edits, and the
# undo_post handler reseeds the obj-side ID from the data side.

def get_bridge_id(obj):
    bid = obj.get("sbox_bridge_id")
    if bid:
        return bid
    # Data-side fallback is only trustworthy when the datablock has a single
    # user. With linked duplicates (Alt+D) the mesh is shared, so the mirror
    # can only name ONE of the objects — falling back here made every linked
    # copy resolve to the original's bridge ID (one s&box object driven by
    # two Blender objects, delete spam, no new object on Alt+D).
    data = obj.data
    if data is not None and data.users <= 1:
        return data.get("sbox_bridge_id")
    return None


def set_bridge_id(obj, bridge_id):
    obj["sbox_bridge_id"] = bridge_id
    data = obj.data
    if data is not None:
        # Never overwrite the mirror on shared data — it anchors the first
        # owner's undo recovery, and linked duplicates track their own IDs
        # purely on the object side.
        if data.users <= 1 or "sbox_bridge_id" not in data:
            data["sbox_bridge_id"] = bridge_id


def clear_bridge_id(obj):
    """Strip the bridge ID from obj and (if not shared) obj.data."""
    if "sbox_bridge_id" in obj:
        del obj["sbox_bridge_id"]
    # Only clear from data if no other object shares it — otherwise we'd
    # nuke another bridged linked-duplicate's recovery anchor.
    data = obj.data
    if data is not None and "sbox_bridge_id" in data and data.users <= 1:
        del data["sbox_bridge_id"]


def _mint_bridge_id():
    """Blender mints bridge IDs; the engine honors them on create (and
    upserts when it already has the id). The .blend is the durable side of
    the bridge — engine-minted IDs died with every unsaved engine scene,
    reissuing identity on each reconnect and orphaning mesh caches."""
    return "b_%08x" % random.getrandbits(32)


def find_by_bridge_id(bridge_id):
    for obj in bpy.data.objects:
        if get_bridge_id(obj) == bridge_id:
            return obj
    return None


@persistent
def on_undo_post(scene):
    """After undo/redo, re-seed obj-side bridge IDs from mesh-data side.

    Custom props on Object are not reliably restored by Blender's undo stack,
    but mesh-data props are. If we find an object whose mesh data still carries
    a bridge ID but the obj does not, restore it. This is what kills the
    ghost-duplicate failure mode (lost ID → looks like a new object → s&box
    creates a duplicate or the hash false-matches an unrelated mesh).
    """
    for obj in bpy.data.objects:
        if obj.get("sbox_bridge_id"):
            continue
        data = obj.data
        if data is None or data.users > 1:
            # Shared data (linked duplicates): the mirror can't say which
            # object it belongs to — reseeding would clone one ID onto all
            # copies. Skip; only single-user data is unambiguous.
            continue
        mesh_id = data.get("sbox_bridge_id")
        if mesh_id:
            obj["sbox_bridge_id"] = mesh_id

    # Undo resurrected a deleted object? Cancel its pending engine-side
    # delete — otherwise the fuse burns down and destroys the engine object
    # while its Blender counterpart is alive again (the undo-orphan bug).
    if _pending_deletes:
        alive = set()
        for obj in bpy.data.objects:
            bid = get_bridge_id(obj)
            if bid:
                alive.add(bid)
        resurrected = [(b, t) for b, t in _pending_deletes if b in alive]
        if resurrected:
            _pending_deletes[:] = [(b, t) for b, t in _pending_deletes
                                   if b not in alive]
            for bid, _ in resurrected:
                _last_known_bridge_ids.add(bid)
            log_activity(
                f"Undo restored {len(resurrected)} object(s) — "
                "pending delete(s) cancelled"
            )
            connection.notify_ui()


def _get_scale_factor():
    try:
        return bpy.context.scene.sbox_bridge.scale_factor
    except Exception:
        return 1.0


def _get_sync_on_connect():
    try:
        return bpy.context.scene.sbox_bridge.sync_on_connect
    except Exception:
        return False


def _should_skip_object(obj):
    """Return True if this object should NOT be synced to s&box.
    Filters out hidden objects, cutters, and other non-visual objects."""
    # Hidden in viewport. hide_get() needs a valid view-layer context — from
    # a timer during file load/render it can raise, and an exception here
    # unwinds the depsgraph handler (traceback on every move) or kills the
    # poll timer. Treat "can't tell" as visible.
    try:
        if obj.hide_viewport or obj.hide_get():
            return True
    except Exception:
        pass
    # Explicitly-ignored helpers (e.g. the player-scale reference box)
    if obj.get("sbox_bridge_ignore"):
        return True
    # Cutter/boolean objects (common in KitOps, HardOps, etc.)
    name_lower = obj.name.lower()
    if "cutter" in name_lower or "boolean" in name_lower:
        return True
    # Objects in hidden collections
    try:
        if not obj.visible_get():
            return True
    except Exception:
        pass
    return False


# ── Geometry Hash & Sync Status ──────────────────────────────────────────

def geometry_hash(obj):
    """Hash of everything we send on the wire, so the dispatcher's hash gate
    invalidates when any of it changes. Returns a 12-char hex string, or
    empty string if no mesh.

    Includes:
      - vertex positions
      - face vertex indices
      - object scale (baked into world-space vertices)
      - material slot names (so applying a material in Blender re-syncs)
      - active UV layer coords (so editing UVs in the UV editor re-syncs)
    """
    import struct
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        if mesh is None:
            return ""
        verts = [c for v in mesh.vertices for c in v.co]
        faces = [vi for p in mesh.polygons for vi in p.vertices]
        scale = [round(s, 4) for s in obj.scale]
        # Project scale_factor is baked into the vertices we send. Changing it
        # must invalidate the hash so Sync All / next sync detects it as a
        # change worth pushing.
        sf = round(_get_scale_factor(), 4)
        mat_names = "|".join(m.name if m else "" for m in mesh.materials)
        uv_layer = mesh.uv_layers.active
        uv_floats = []
        if uv_layer:
            for d in uv_layer.data:
                uv_floats.append(d.uv.x)
                uv_floats.append(d.uv.y)
        data = (struct.pack(f'{len(verts)}f', *verts)
                + struct.pack(f'{len(faces)}i', *faces)
                + struct.pack('3f', *scale)
                + struct.pack('f', sf)
                + mat_names.encode('utf-8')
                + struct.pack(f'{len(uv_floats)}f', *uv_floats))
        eval_obj.to_mesh_clear()
        return hashlib.md5(data).hexdigest()[:12]
    except Exception:
        return ""


def get_sync_status(obj):
    """Get the sync status of a bridge object."""
    return obj.get("sbox_bridge_status", "unsent")


def set_sync_status(obj, status):
    """Set sync status: unsent, synced, modified, received."""
    obj["sbox_bridge_status"] = status
    if status in ("synced", "received"):
        obj["sbox_bridge_last_sync"] = time.time()


def get_stored_hash(obj):
    """Get the last-sent geometry hash."""
    return obj.get("sbox_bridge_hash", "")


def set_stored_hash(obj, h):
    """Store the geometry hash after a successful send."""
    obj["sbox_bridge_hash"] = h


def get_sync_mode():
    """Get the current sync mode from settings."""
    try:
        return bpy.context.scene.sbox_bridge.sync_mode
    except Exception:
        return 'BIDIRECTIONAL'


def get_collection_path(obj):
    """Get the collection hierarchy path for an object.
    Returns a list like ["World", "Environment", "Town"]."""
    for col in bpy.data.collections:
        if obj.name in col.objects:
            path = []
            if _build_collection_path(col, bpy.context.scene.collection, path):
                return path
            return [col.name]
    return []


def _build_collection_path(target, current, path):
    """Recursively find target collection and build path."""
    for child in current.children:
        if child == target:
            path.append(child.name)
            return True
        if _build_collection_path(target, child, path):
            path.insert(len(path) - 1, child.name)
            return True
    return False


def get_or_create_collection_path(path_list):
    """Get or create a nested collection path under 's&box Scene'.
    Returns the deepest collection."""
    parent = get_or_create_sbox_collection()
    for name in path_list:
        child = None
        for c in parent.children:
            if c.name == name:
                child = c
                break
        if child is None:
            child = bpy.data.collections.new(name)
            parent.children.link(child)
        parent = child
    return parent


# ── Activity Log ─────────────────────────────────────────────────────────

_activity_log = []              # [(timestamp, message)]
MAX_LOG_ENTRIES = 50


def log_activity(message):
    """Add an entry to the activity log visible in the Blender panel."""
    _activity_log.append((time.time(), message))
    if len(_activity_log) > MAX_LOG_ENTRIES:
        _activity_log.pop(0)
    print(f"[Bridge] {message}")


def get_activity_log():
    return list(_activity_log)


def clear_activity_log():
    _activity_log.clear()


# ── Warnings & Status ────────────────────────────────────────────────────

def add_warning(message):
    _warnings.append((time.time(), message))
    if len(_warnings) > 10:
        _warnings.pop(0)
    log_activity(f"WARNING: {message}")
    connection.notify_ui()


def get_warnings():
    return list(_warnings)


def clear_warnings():
    _warnings.clear()
    connection.notify_ui()


def get_pending_deletes():
    return list(_pending_deletes)


def cancel_pending_deletes():
    for bid, _ in _pending_deletes:
        _last_known_bridge_ids.add(bid)
    _pending_deletes.clear()


def is_play_mode():
    return _play_mode_active


# ── Sync-Health State (single source of truth for the panel) ─────────────

def get_mute_reason():
    """Why live outbound is muted right now, or None if it would flow.
    MUST mirror on_depsgraph_update's gates exactly — the panel renders
    this and the handler obeys the same conditions, so the UI can never
    disagree with actual behavior."""
    if _play_mode_active:
        return "play mode"
    try:
        s = bpy.context.scene.sbox_bridge
        if not s.auto_sync:
            return "Auto Sync off"
        if s.sync_mode == 'MANUAL':
            return "Manual mode"
    except Exception:
        pass
    return None


def get_degraded_reason():
    """Liveness assertion on the outbound pipeline's organs. This is the
    ONLY check that catches a stripped depsgraph handler — heartbeats and
    echo probes inject BELOW the handler, so a dead handler shows fresh
    round-trips and an empty queue on every other indicator. That failure
    class once took days to find; this makes it one red line."""
    try:
        if on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            return "depsgraph handler missing"
        if connection.is_connected():
            if not bpy.app.timers.is_registered(_poll_and_process):
                return "poll timer dead"
            if (_first_dirty_time is not None
                    and time.time() - _first_dirty_time > 5.0):
                return "outbound flush stalled"
    except Exception:
        pass
    return None


# ── Outgoing: Blender -> s&box ──────────────────────────────────────────

def send_create(obj):
    """Send a create message. Blender mints the bridge ID and the engine
    honors it (upserting if it already has that id), so identity survives
    engine-session loss. Detects paste duplicates and uses idempotency keys."""
    global _blender_seq

    # Detect paste duplicate: another object has the same bridgeId
    bid = get_bridge_id(obj)
    if bid:
        for other in bpy.data.objects:
            if other != obj and get_bridge_id(other) == bid:
                # This is a pasted copy — strip the stale ID
                clear_bridge_id(obj)
                bid = None
                break

    # Engine already has this object — nothing to create. An id the engine
    # DOESN'T know is kept and re-sent under the same identity (fresh engine
    # session, restored from Bridge Trash, "not found" recovery).
    if bid and bid in _last_known_bridge_ids:
        return

    # Skip hidden/cutter objects
    if _should_skip_object(obj):
        return

    # Strip stale props
    if "_remote_update_time" in obj:
        del obj["_remote_update_time"]

    try:
        sf = _get_scale_factor()
        mesh_data = _extract_mesh_data(obj, sf)

        if mesh_data is None:
            print(f"[Bridge] Create skipped '{obj.name}': no mesh data")
            return

        wp = obj.matrix_world.to_translation()
        px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)
        idem_key = f"{obj.name}_{getattr(obj, 'session_uid', id(obj))}_{_session_nonce}"
        geo_hash = geometry_hash(obj)
        hierarchy = get_collection_path(obj)

        # Reuse the object's existing id (re-create under the same identity)
        # or mint a fresh one. The engine honors it.
        if not bid:
            bid = _mint_bridge_id()

        _blender_seq += 1
        msg = {
            "type": "create",
            "seq": _blender_seq,
            "ack": _last_sbox_seq_processed,
            "name": obj.name,
            "bridgeId": bid,
            "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
            "rotation": _rotation_to_sbox(obj),
            "meshData": mesh_data,
            "idempotencyKey": idem_key,
            "geometryHash": geo_hash,
            "hierarchy": hierarchy,
        }

        response = connection.send_and_receive(msg)
        if response and "bridgeId" in response:
            rbid = response["bridgeId"]
            if any(other is not obj and get_bridge_id(other) == rbid
                   for other in bpy.data.objects):
                # Idempotency echo returned an id ANOTHER object already
                # holds. Happens when a duplicate stole this object's id and
                # this is the re-create: same key -> same old id -> the pair
                # collides again next tick, forever ("Created: X -> b_..."
                # spam). Salt the key and retry once to force a new object.
                _blender_seq += 1
                msg["seq"] = _blender_seq
                msg["idempotencyKey"] = f"{idem_key}_r{random.getrandbits(16):04x}"
                response = connection.send_and_receive(msg)
        if response and "bridgeId" in response:
            set_bridge_id(obj, response["bridgeId"])
            obj["sbox_bridge_name"] = obj.name
            set_stored_hash(obj, geo_hash)
            set_sync_status(obj, "synced")
            _last_write_seq[response["bridgeId"]] = _blender_seq
            _last_known_bridge_ids.add(response["bridgeId"])
            log_activity(f"Created: {obj.name} -> {response['bridgeId']}")
        else:
            log_activity(f"Create failed '{obj.name}': {response}")
    except Exception as e:
        print(f"[Bridge] Create error '{obj.name}': {e}")


def send_update_transform(obj, force=False):
    """Send a transform-only update with 20Hz rate limiting.

    force=True (dirty-set drain) skips the limiter: the drain cadence already
    bounds the rate, and dropping here would LOSE the move — the dirty mark
    was consumed, so a skipped final drag position never gets re-sent."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    # 20Hz debounce per object
    now = time.time()
    if not force and now - _last_transform_send.get(bridge_id, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[bridge_id] = now

    _blender_seq += 1
    _last_write_seq[bridge_id] = _blender_seq

    sf = _get_scale_factor()
    wp = obj.matrix_world.to_translation()
    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)

    msg = {
        "type": "update_transform",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
    }
    connection.send(msg)


def send_update_mesh(obj):
    """Send a full mesh update. Uses chunked transfer for large meshes.
    Skips if geometry hash hasn't changed (no-op optimization)."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    # Hash check — skip the heavy mesh payload if geometry hasn't changed.
    # But NOT the transform: moving an object with generative modifiers
    # (Array, Mirror, booleans) fires is_updated_geometry, so the move gets
    # routed down THIS path — a bare return here swallows it. That was why
    # arrayed walls ignored moves/rotations until a count change forced a
    # real geometry resync, dupes of arrays "never appeared" (they spawned
    # inside the original and their move was eaten), and long sessions
    # drifted out of alignment.
    geo_hash = geometry_hash(obj)
    stored = get_stored_hash(obj)
    if geo_hash and stored and geo_hash == stored:
        send_update_transform(obj, force=True)
        return

    sf = _get_scale_factor()
    mesh_data = _extract_mesh_data(obj, sf)
    if mesh_data is None:
        return

    # Check if chunked transfer needed
    vert_count = len(mesh_data.get("vertices", [])) // 3
    if vert_count > CHUNK_VERTEX_LIMIT:
        _send_chunked_mesh(obj, bridge_id, mesh_data)
        set_stored_hash(obj, geo_hash)
        set_sync_status(obj, "synced")
        return

    wp = obj.matrix_world.to_translation()
    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)

    _blender_seq += 1
    _last_write_seq[bridge_id] = _blender_seq

    msg = {
        "type": "update_mesh",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
        "name": obj.name,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
        "meshData": mesh_data,
        "geometryHash": geo_hash,
    }
    connection.send(msg)
    set_stored_hash(obj, geo_hash)
    set_sync_status(obj, "synced")


def _send_chunked_mesh(obj, bridge_id, mesh_data):
    """Send large mesh via chunked protocol using Blender timers."""
    global _blender_seq

    vertices = mesh_data.get("vertices", [])
    faces = mesh_data.get("faces", [])
    face_materials = mesh_data.get("faceMaterials", [])
    materials = mesh_data.get("materials", [])
    face_uvs = mesh_data.get("faceUVs")

    total_verts = len(vertices) // 3
    chunk_float_size = CHUNK_VERTEX_LIMIT * 3
    chunk_count = (len(vertices) + chunk_float_size - 1) // chunk_float_size

    # Cancel any in-flight stream for this object
    if bridge_id in _chunked_streams:
        _chunked_streams[bridge_id]["cancelled"] = True

    _blender_seq += 1
    _last_write_seq[bridge_id] = _blender_seq

    begin_msg = {
        "type": "mesh_begin",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
        "totalVertices": total_verts,
        "totalFaces": len(faces),
        "chunkCount": chunk_count,
    }
    connection.send(begin_msg)

    stream = {
        "bridge_id": bridge_id,
        "vertices": vertices,
        "faces": faces,
        "face_materials": face_materials,
        "materials": materials,
        "face_uvs": face_uvs,
        "chunk_count": chunk_count,
        "chunks_sent": 0,
        "cancelled": False,
        "obj_name": obj.name,
    }
    _chunked_streams[bridge_id] = stream

    def send_next_chunk():
        global _blender_seq
        s = _chunked_streams.get(bridge_id)
        if s is None or s.get("cancelled"):
            _chunked_streams.pop(bridge_id, None)
            return None

        idx = s["chunks_sent"]
        if idx >= s["chunk_count"]:
            # All vertex chunks sent — send mesh_end with faces + materials
            _blender_seq += 1
            sf = _get_scale_factor()
            try:
                o = bpy.data.objects.get(s["obj_name"])
                if o:
                    wp = o.matrix_world.to_translation()
                    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)
                    pos = {"x": px * sf, "y": py * sf, "z": pz * sf}
                    rot = _rotation_to_sbox(o)
                else:
                    pos = {"x": 0, "y": 0, "z": 0}
                    rot = {"pitch": 0, "yaw": 0, "roll": 0}
            except Exception:
                pos = {"x": 0, "y": 0, "z": 0}
                rot = {"pitch": 0, "yaw": 0, "roll": 0}

            end_msg = {
                "type": "mesh_end",
                "seq": _blender_seq,
                "ack": _last_sbox_seq_processed,
                "bridgeId": bridge_id,
                "faces": s["faces"],
                "faceMaterials": s["face_materials"],
                "materials": s["materials"],
                "position": pos,
                "rotation": rot,
            }
            # Only attach UVs when present — a JSON null would trip the
            # dispatcher's array enumeration.
            if s["face_uvs"]:
                end_msg["faceUVs"] = s["face_uvs"]
            connection.send(end_msg)
            _chunked_streams.pop(bridge_id, None)
            return None

        # Send next vertex chunk
        offset = idx * CHUNK_VERTEX_LIMIT * 3
        chunk_verts = s["vertices"][offset:offset + CHUNK_VERTEX_LIMIT * 3]

        _blender_seq += 1
        chunk_msg = {
            "type": "mesh_chunk",
            "seq": _blender_seq,
            "ack": _last_sbox_seq_processed,
            "bridgeId": bridge_id,
            "chunkIndex": idx,
            "vertices": chunk_verts,
            "vertexOffset": idx * CHUNK_VERTEX_LIMIT,
        }
        connection.send(chunk_msg)
        s["chunks_sent"] += 1
        return 0.01  # Next chunk in 10ms

    bpy.app.timers.register(send_next_chunk, first_interval=0.01)


def send_delete(bridge_id):
    """Send a delete message to s&box."""
    global _blender_seq
    _blender_seq += 1
    msg = {
        "type": "delete",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
    }
    connection.send(msg)
    _last_write_seq.pop(bridge_id, None)
    log_activity(f"Sent delete: {bridge_id}")


def send_visibility(bridge_id, visible):
    """Toggle the engine-side GameObject's Enabled state. Used for Blender
    hide/unhide — the object keeps its bridge ID, mesh, and materials, unlike
    the old delete/re-create cycle which lost engine-side texture work."""
    global _blender_seq
    _blender_seq += 1
    msg = {
        "type": "set_visibility",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
        "visible": bool(visible),
    }
    connection.send(msg)


def send_sync():
    """Request full sync from s&box, including our known object list."""
    global _blender_seq
    _blender_seq += 1

    known = []
    for obj in bpy.data.objects:
        bid = get_bridge_id(obj)
        if bid:
            known.append({"bridgeId": bid, "name": obj.name})

    msg = {
        "type": "sync",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "knownObjects": known,
    }
    connection.send(msg)
    print("[Bridge] Requested sync.")


# Grid spacing the bridge last broadcast — short-circuits re-broadcast when
# an incoming grid_updated bounces back through the property update callback.
_last_grid_sent = None


def send_grid_changed(grid_size):
    """Push bridge grid_size to s&box. s&box mirrors it into Gizmo.Settings.GridSpacing."""
    global _blender_seq, _last_grid_sent
    if not connection.is_connected():
        return
    if _last_grid_sent == grid_size:
        return
    _last_grid_sent = grid_size
    _blender_seq += 1
    msg = {
        "type": "grid_changed",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "gridSize": int(grid_size),
    }
    connection.send(msg)


def send_create_light(obj):
    """Create a light in s&box from a Blender light object."""
    global _blender_seq

    if obj.type != "LIGHT" or not obj.data:
        return
    if obj.data.type in UNSUPPORTED_LIGHT_TYPES:
        add_warning(f"Skipped '{obj.name}': {obj.data.type} lights not supported")
        return

    light_type_map = {"POINT": "point", "SPOT": "spot", "SUN": "directional"}
    sbox_light_type = light_type_map.get(obj.data.type, "point")

    # Same ID policy as send_create: reuse or mint; skip only when the
    # engine is known to already have it.
    bid = get_bridge_id(obj)
    if bid and bid in _last_known_bridge_ids:
        return
    if not bid:
        bid = _mint_bridge_id()

    sf = _get_scale_factor()
    wp = obj.matrix_world.to_translation()
    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)
    props = _extract_light_properties(obj)
    idem_key = f"light_{obj.name}_{getattr(obj, 'session_uid', id(obj))}_{_session_nonce}"

    _blender_seq += 1
    msg = {
        "type": "create_light",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "name": obj.name,
        "bridgeId": bid,
        "lightType": sbox_light_type,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
        "properties": props,
        "idempotencyKey": idem_key,
    }

    response = connection.send_and_receive(msg)
    if response and "bridgeId" in response:
        set_bridge_id(obj, response["bridgeId"])
        obj["sbox_bridge_name"] = obj.name
        _last_write_seq[response["bridgeId"]] = _blender_seq
        _last_known_bridge_ids.add(response["bridgeId"])
        log_activity(f"Created light: {obj.name} -> {response['bridgeId']}")
    else:
        print(f"[Bridge] Light create failed '{obj.name}': {response}")


def send_update_light(obj, force=False):
    """Send light property + transform update to s&box."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    now = time.time()
    if not force and now - _last_transform_send.get(bridge_id, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[bridge_id] = now

    sf = _get_scale_factor()
    wp = obj.matrix_world.to_translation()
    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)
    props = _extract_light_properties(obj)

    _blender_seq += 1
    _last_write_seq[bridge_id] = _blender_seq

    msg = {
        "type": "update_light",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "bridgeId": bridge_id,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
        "properties": props,
    }
    connection.send(msg)


def quarantine_scene_id(scene_id):
    """Stop syncing a scene object whose engine counterpart is gone. Called
    from connection when the engine rejects update_scene_transform as
    not-found. Names the orphan in the panel so the user knows what to fix."""
    if scene_id in _dead_scene_ids:
        return
    _dead_scene_ids.add(scene_id)
    name = "?"
    try:
        for obj in bpy.data.objects:
            if obj.get("sbox_scene_id") == scene_id:
                name = obj.name
                obj["sbox_bridge_status"] = "quarantined"
                break
    except Exception:
        pass
    add_warning(
        f"'{name}' points at an s&box object that no longer exists "
        f"(stale scene link {str(scene_id)[:8]}…) — pausing its sync. "
        f"Delete it in Blender, or Sync All to refresh."
    )


def send_scene_transform(obj, force=False):
    """Send position update for a scene object (model/light from s&box)."""
    scene_id = obj.get("sbox_scene_id")
    if not scene_id:
        return
    if scene_id in _dead_scene_ids:
        return

    key = f"scene_{scene_id}"
    now = time.time()
    if not force and now - _last_transform_send.get(key, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[key] = now

    global _blender_seq
    sf = _get_scale_factor()
    wp = obj.matrix_world.to_translation()
    px, py, pz = blender_to_sbox_pos(wp.x, wp.y, wp.z)

    _blender_seq += 1
    msg = {
        "type": "update_scene_transform",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "sceneId": scene_id,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
    }
    connection.send(msg)


# ── Incoming: s&box -> Blender ──────────────────────────────────────────

def process_incoming(msg):
    global _suppress_depsgraph

    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except json.JSONDecodeError:
            return

    msg_type = msg.get("type")
    _suppress_depsgraph = True

    try:
        if msg_type == "updated":
            _handle_updated(msg)
        elif msg_type == "deleted":
            _handle_deleted(msg)
        elif msg_type == "sync_response":
            _handle_sync_response(msg)
        elif msg_type == "mesh_updated":
            _handle_mesh_updated(msg)
        elif msg_type == "object_created":
            _handle_object_created(msg)
        elif msg_type == "scene_updated":
            _handle_scene_updated(msg)
        elif msg_type == "light_updated":
            _handle_light_updated(msg)
        elif msg_type == "play_mode":
            _handle_play_mode(msg)
        elif msg_type == "grid_updated":
            _handle_grid_updated(msg)
    finally:
        _suppress_depsgraph = False


def _handle_grid_updated(msg):
    """s&box's Gizmo.Settings.GridSpacing changed. Mirror it into bridge grid_size.
    Stamp _last_grid_sent first so the property update callback's send_grid_changed
    short-circuits (no echo back to s&box)."""
    global _last_grid_sent
    gs = msg.get("gridSize")
    if gs is None:
        return
    try:
        gs = int(gs)
        if gs < 1 or gs > 256:
            return
        _last_grid_sent = gs
        bpy.context.scene.sbox_bridge.grid_size = gs
    except Exception as e:
        print(f"[Bridge] grid_updated handler error: {e}")


def _handle_updated(msg):
    """s&box moved a bridge object. Suppress if it's our own echo."""
    bridge_id = msg.get("bridgeId")
    if not bridge_id:
        return

    # Sequence-based echo suppression (one-shot)
    if bridge_id in _last_write_seq:
        del _last_write_seq[bridge_id]
        return  # This is our own echo bouncing back

    obj = find_by_bridge_id(bridge_id)
    if not obj:
        return

    _remote_update_times[bridge_id] = time.time()
    _apply_sbox_transform(obj, msg)


def _handle_mesh_updated(msg):
    """s&box edited the mesh geometry."""
    bridge_id = msg.get("bridgeId")
    if not bridge_id:
        return

    if bridge_id in _last_write_seq:
        del _last_write_seq[bridge_id]
        return

    obj = find_by_bridge_id(bridge_id)
    if not obj:
        return

    _remote_update_times[bridge_id] = time.time()

    # Export Only mode — never overwrite Blender mesh with s&box data
    if get_sync_mode() == 'EXPORT_ONLY':
        _apply_sbox_transform(obj, msg)
        return

    mesh_data = msg.get("meshData")
    if mesh_data and mesh_data.get("vertices"):
        # Rebuild before applying the transform: the rebuild resets scale to 1
        # (incoming verts are world-scale-baked), so the transform goes through
        # the plain positive-determinant path.
        _rebuild_mesh(obj, mesh_data)
        _apply_sbox_transform(obj, msg)
        # Restamp hash + scale tracking so the depsgraph fire caused by this
        # write doesn't bounce an identical mesh straight back to s&box.
        set_stored_hash(obj, geometry_hash(obj))
        _last_scale[bridge_id] = tuple(round(s, 4) for s in obj.scale)
        set_sync_status(obj, "received")
    else:
        _apply_sbox_transform(obj, msg)


def _handle_object_created(msg):
    """s&box created a new object (native MeshComponent auto-adopted by the bridge)."""
    bridge_id = msg.get("bridgeId")
    if not bridge_id:
        return

    # Don't create if we already have it
    if find_by_bridge_id(bridge_id):
        return

    _create_from_sbox(msg)


def _handle_deleted(msg):
    """s&box deleted a bridge object. NEVER hard-delete the Blender
    counterpart — quarantine it in Bridge Trash and wait for explicit
    Confirm/Restore in the panel. Blender is the source of truth; one bad
    engine-side action must not be able to eat modeling work."""
    bridge_id = msg.get("bridgeId")
    if not bridge_id:
        return
    if not _quarantine_inbound_delete(bridge_id, "live delete"):
        log_activity(f"Deleted from s&box: {bridge_id} (no local object)")


# ── Inbound Delete Quarantine (Bridge Trash) ────────────────────────────

TRASH_COLLECTION_NAME = "Bridge Trash (s&box deletes)"

# bridgeId -> {"name": str, "time": float, "collections": [str]}
_inbound_pending_deletes = {}


def get_inbound_pending_deletes():
    return dict(_inbound_pending_deletes)


def _get_or_create_trash_collection():
    col = bpy.data.collections.get(TRASH_COLLECTION_NAME)
    if col is None:
        col = bpy.data.collections.new(TRASH_COLLECTION_NAME)
    try:
        if col.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(col)
    except Exception:
        pass
    col.hide_viewport = True
    col.hide_render = True
    return col


def _quarantine_inbound_delete(bridge_id, source):
    """Move the object to the hidden Bridge Trash collection instead of
    deleting it. Returns True if a local object was quarantined."""
    _last_known_bridge_ids.discard(bridge_id)
    _last_write_seq.pop(bridge_id, None)
    obj = find_by_bridge_id(bridge_id)
    if obj is None:
        return False
    if bridge_id in _inbound_pending_deletes:
        return True

    original_cols = [c.name for c in obj.users_collection
                     if c.name != TRASH_COLLECTION_NAME]
    trash = _get_or_create_trash_collection()
    for c in list(obj.users_collection):
        try:
            c.objects.unlink(obj)
        except Exception:
            pass
    if obj.name not in trash.objects:
        trash.objects.link(obj)

    _inbound_pending_deletes[bridge_id] = {
        "name": obj.name,
        "time": time.time(),
        "collections": original_cols,
    }
    log_activity(f"s&box deleted '{obj.name}' ({source}) — held in Bridge Trash")
    add_warning(
        f"s&box deleted '{obj.name}' — held in Bridge Trash. "
        "Confirm or Restore in the panel."
    )
    return True


def confirm_inbound_deletes():
    """Permanently delete everything held in Bridge Trash."""
    count = 0
    for bid in list(_inbound_pending_deletes.keys()):
        obj = find_by_bridge_id(bid)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        _inbound_pending_deletes.pop(bid, None)
        count += 1
    _remove_trash_if_empty()
    log_activity(f"Confirmed {count} s&box delete(s)")
    connection.notify_ui()
    return count


def restore_inbound_deletes():
    """Bring trashed objects back into their original collections and queue
    re-creates. IDs are kept — the engine honors Blender-minted ids — so
    each object comes back to s&box under its original identity."""
    count = 0
    for bid in list(_inbound_pending_deletes.keys()):
        info = _inbound_pending_deletes.pop(bid)
        obj = find_by_bridge_id(bid)
        if obj is None:
            continue
        trash = bpy.data.collections.get(TRASH_COLLECTION_NAME)
        if trash is not None and obj.name in trash.objects:
            try:
                trash.objects.unlink(obj)
            except Exception:
                pass
        linked = False
        for cname in info.get("collections", []):
            col = bpy.data.collections.get(cname)
            if col is not None:
                try:
                    col.objects.link(obj)
                    linked = True
                except Exception:
                    pass
        if not linked:
            try:
                bpy.context.scene.collection.objects.link(obj)
            except Exception:
                pass
        uid = getattr(obj, "session_uid", id(obj))
        if obj.type == "MESH":
            _mark_dirty("create", f"create_{uid}", obj)
        elif obj.type == "LIGHT":
            _mark_dirty("create_light", f"create_{uid}", obj)
        count += 1
    _remove_trash_if_empty()
    log_activity(f"Restored {count} object(s) from Bridge Trash — re-sending to s&box")
    connection.notify_ui()
    return count


def _remove_trash_if_empty():
    col = bpy.data.collections.get(TRASH_COLLECTION_NAME)
    if col is not None and not col.objects and not _inbound_pending_deletes:
        try:
            bpy.data.collections.remove(col)
        except Exception:
            pass


def _handle_scene_updated(msg):
    """s&box moved a scene object."""
    scene_id = msg.get("sceneId")
    if not scene_id:
        return

    for obj in bpy.data.objects:
        if obj.get("sbox_scene_id") == scene_id:
            _apply_sbox_transform(obj, msg)
            return


def _handle_light_updated(msg):
    """s&box updated light properties."""
    scene_id = msg.get("sceneId")
    if not scene_id:
        return

    for obj in bpy.data.objects:
        if obj.get("sbox_scene_id") == scene_id and obj.type == "LIGHT":
            _apply_sbox_transform(obj, msg)
            props = msg.get("properties", {})
            if props and obj.data:
                _apply_light_properties(obj, props)
            return


def _handle_play_mode(msg):
    """s&box entered or exited play mode."""
    global _play_mode_active
    state = msg.get("state", "")
    _play_mode_active = (state == "started")
    if _play_mode_active:
        add_warning("s&box entered Play Mode")
    else:
        add_warning("s&box exited Play Mode")


def _handle_sync_response(msg):
    """Full sync from s&box. Reconcile Blender state.
    Only creates Blender objects for s&box-originated objects that Blender doesn't have.
    Never re-creates objects that Blender itself sent — those already exist locally."""
    global _last_known_bridge_ids
    objects = msg.get("objects", [])
    received_ids = set()

    # Fresh reconciliation — retry any quarantined scene links; if they're
    # still gone, the next rejection re-quarantines them.
    _dead_scene_ids.clear()

    # Collect all bridge IDs Blender currently has BEFORE processing
    local_ids = set()
    for obj in bpy.data.objects:
        bid = get_bridge_id(obj)
        if bid:
            local_ids.add(bid)

    inline_delete_bids = []
    for obj_data in objects:
        # Inline deletes from reconciliation: collect only — handled after the
        # loop, once received_ids tells us whether the engine actually has
        # state or is a fresh session that lost everything.
        if obj_data.get("type") == "deleted":
            bid = obj_data.get("bridgeId")
            if bid:
                inline_delete_bids.append(bid)
            continue

        bridge_id = obj_data.get("bridgeId")
        obj_type = obj_data.get("objectType", "")

        # Non-bridge objects (lights, models from s&box)
        if not bridge_id:
            if obj_type == "light":
                _create_light(obj_data)
            elif obj_type == "model":
                _create_model_placeholder(obj_data)
            continue

        received_ids.add(bridge_id)

        existing = find_by_bridge_id(bridge_id)
        if existing:
            # Object exists in Blender — just update transform, don't touch mesh/materials
            _apply_sbox_transform(existing, obj_data)
            name = obj_data.get("name")
            if name and existing.name != name:
                existing.name = name
        elif bridge_id not in local_ids:
            # Object exists in s&box but NOT in Blender — create it
            # (Skip if Blender had this ID before processing, meaning we just sent it)
            _create_from_sbox(obj_data)

    # Engine-reported-stale handling. NEVER hard-delete Blender objects here:
    # a fresh engine session (restart, unsaved engine scene) legitimately
    # knows nothing — hard-deleting ate user scenes on every engine restart.
    #
    # Two distinct cases:
    #  - Fresh engine session with zero bridge objects: repopulate it from
    #    Blender. IDs are KEPT — the engine honors Blender-minted ids on
    #    create — so the scene comes back under the same identities (mesh
    #    caches and references stay valid).
    #  - Engine HAS state and explicitly reports ids deleted: route through
    #    the same Bridge Trash quarantine as live 'deleted' messages.
    global _expect_fresh_engine
    expect_fresh = _expect_fresh_engine
    _expect_fresh_engine = False
    engine_is_empty = not received_ids

    if engine_is_empty and expect_fresh:
        requeued = 0
        for obj in list(bpy.data.objects):
            bid = get_bridge_id(obj)
            if not bid or bid in _inbound_pending_deletes:
                continue
            _last_known_bridge_ids.discard(bid)
            if _should_skip_object(obj):
                continue
            uid = getattr(obj, "session_uid", id(obj))
            if obj.type == "MESH":
                _mark_dirty("create", f"create_{uid}", obj)
                requeued += 1
            elif obj.type == "LIGHT":
                if obj.data and obj.data.type not in UNSUPPORTED_LIGHT_TYPES:
                    _mark_dirty("create_light", f"create_{uid}", obj)
                    requeued += 1
        if requeued:
            log_activity(
                f"Engine session is empty — re-sending {requeued} object(s) "
                "under their existing IDs"
            )
    else:
        for bid in inline_delete_bids:
            _quarantine_inbound_delete(bid, "sync reconcile")

    # Ids we thought the engine had but this sync didn't report (partial
    # loss, scene-tab switch). KEEP the Blender-side ids — the engine honors
    # them on re-create — just drop them from the engine-known set and tell
    # the user; Sync All re-sends them under the same identity.
    stale = _last_known_bridge_ids - received_ids
    stale -= set(_inbound_pending_deletes)
    if stale and not engine_is_empty:
        add_warning(
            f"{len(stale)} object(s) not reported by s&box — kept in Blender. "
            "Sync All re-sends them under the same IDs."
        )

    _last_known_bridge_ids = received_ids.copy()

    # Do NOT auto-send creates here — Sync All and Force Resync handle that
    # before calling send_sync(). Auto-creating here causes duplicate feedback loops.

    log_activity(f"Sync complete: {len(received_ids)} bridge objects from s&box")


# ── Object Creation from s&box ──────────────────────────────────────────

def _create_from_sbox(msg):
    """Create a Blender object from s&box data with actual mesh geometry."""
    bridge_id = msg.get("bridgeId")
    name = msg.get("name", "sbox Object")
    mesh_data = msg.get("meshData")

    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0

    # Engine-side duplicate (shift-drag/Ctrl+D re-tagged by the scan). The
    # wire meshData is geometry-only — building from it makes a BARE copy
    # that loses materials/UVs, and the bare copy's first edit then echoed a
    # material-less mesh back and wiped the ENGINE object's textures too.
    # Clone our own copy of the source object instead: identical geometry
    # plus full materials, UVs, and per-face assignments.
    src_bid = msg.get("sourceBridgeId")
    if src_bid:
        src = find_by_bridge_id(src_bid)
        if src is not None and src.type == 'MESH' and src.data is not None:
            obj = bpy.data.objects.new(name, src.data.copy())
            _finish_create_from_sbox(obj, msg, bridge_id, name)
            log_activity(f"Created from s&box dup of {src_bid}: {name} ({bridge_id})")
            return

    if mesh_data and mesh_data.get("vertices") and len(mesh_data["vertices"]) >= 9:
        raw_verts = mesh_data["vertices"]
        raw_faces = mesh_data.get("faces", [])

        blender_verts = []
        for i in range(len(raw_verts) // 3):
            sx = raw_verts[i * 3] * inv_sf
            sy = raw_verts[i * 3 + 1] * inv_sf
            sz = raw_verts[i * 3 + 2] * inv_sf
            blender_verts.append(sbox_to_blender_pos(sx, sy, sz))

        blender_faces = []
        idx = 0
        while idx < len(raw_faces):
            count = raw_faces[idx]
            idx += 1
            if idx + count > len(raw_faces):
                break
            blender_faces.append([raw_faces[idx + j] for j in range(count)])
            idx += count

        mesh = bpy.data.meshes.new(f"{name}_mesh")
        bm = bmesh.new()
        bm_verts = [bm.verts.new(v) for v in blender_verts]
        bm.verts.ensure_lookup_table()
        for fi in blender_faces:
            try:
                fv = [bm_verts[i] for i in fi if i < len(bm_verts)]
                if len(fv) >= 3:
                    bm.faces.new(fv)
            except (IndexError, ValueError):
                continue
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        obj = bpy.data.objects.new(name, mesh)
    else:
        mesh = bpy.data.meshes.new(f"{name}_mesh")
        obj = bpy.data.objects.new(name, mesh)

    _finish_create_from_sbox(obj, msg, bridge_id, name)
    log_activity(f"Created from s&box: {name} ({bridge_id})")


def _finish_create_from_sbox(obj, msg, bridge_id, name):
    """Shared tail of _create_from_sbox: link, identify, place, and baseline."""
    # Link to collection based on hierarchy (if provided) or default
    hierarchy = msg.get("hierarchy", [])
    if hierarchy:
        col = get_or_create_collection_path(hierarchy)
    else:
        col = get_or_create_sbox_collection()
    col.objects.link(obj)

    if bridge_id:
        set_bridge_id(obj, bridge_id)
        obj["sbox_bridge_name"] = name
        set_sync_status(obj, "received")
        _last_known_bridge_ids.add(bridge_id)

    _apply_sbox_transform(obj, msg)

    # Baseline the change-detection state AFTER the transform (the hash
    # includes scale). Without this, the depsgraph event fired by our own
    # creation read as a user edit and immediately re-sent the object's
    # mesh — material-less — back to the engine, stripping its textures.
    try:
        set_stored_hash(obj, geometry_hash(obj))
        if bridge_id:
            _last_scale[bridge_id] = tuple(round(s, 4) for s in obj.scale)
    except Exception:
        pass


def _rebuild_mesh(obj, mesh_data):
    """Replace an existing Blender object's mesh with new data from s&box.

    Incoming vertices arrive with world scale baked in (that is how
    _extract_mesh_data sent them), so the object's scale is reset to 1 after
    the swap — keeping the old scale would apply it a second time on top of
    the baked data, visibly inflating or shrinking the object after every
    s&box-side edit. The reset also normalizes reflected (negative-scale)
    matrices: any mirror parity is already present in the baked vertex data,
    and with a positive-determinant matrix the face winding from s&box is
    correct as-is.
    """
    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0

    raw_verts = mesh_data.get("vertices", [])
    raw_faces = mesh_data.get("faces", [])
    if len(raw_verts) < 9:
        return

    blender_verts = []
    for i in range(len(raw_verts) // 3):
        sx = raw_verts[i * 3] * inv_sf
        sy = raw_verts[i * 3 + 1] * inv_sf
        sz = raw_verts[i * 3 + 2] * inv_sf
        blender_verts.append(sbox_to_blender_pos(sx, sy, sz))

    blender_faces = []
    idx = 0
    while idx < len(raw_faces):
        count = raw_faces[idx]
        idx += 1
        if idx + count > len(raw_faces):
            break
        blender_faces.append([raw_faces[idx + j] for j in range(count)])
        idx += count

    mesh = bpy.data.meshes.new(f"{obj.name}_mesh")
    bm = bmesh.new()
    bm_verts = [bm.verts.new(v) for v in blender_verts]
    bm.verts.ensure_lookup_table()
    for fi in blender_faces:
        try:
            fv = [bm_verts[i] for i in fi if i < len(bm_verts)]
            if len(fv) >= 3:
                bm.faces.new(fv)
        except (IndexError, ValueError):
            continue
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    # Reseed the data-side bridge ID mirror on the fresh mesh — undo recovery
    # (on_undo_post) restores the object-side ID from mesh data, and the swap
    # would otherwise leave the new datablock without one.
    bid = obj.get("sbox_bridge_id")
    if bid:
        mesh["sbox_bridge_id"] = bid

    old_mesh = obj.data
    # Preserve material slots across the rebuild — the wire payload from
    # s&box carries only geometry, and a bare new datablock silently drops
    # every material (object goes untextured in Blender after any
    # s&box-side edit). Per-face assignment can't survive (topology may
    # have changed), so faces land on slot 0.
    if old_mesh is not None:
        for mat in old_mesh.materials:
            mesh.materials.append(mat)
    obj.data = mesh
    if old_mesh and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)

    obj.scale = (1.0, 1.0, 1.0)


def _create_light(msg):
    """Create a Blender light from s&box scene data."""
    name = msg.get("name", "Light")
    scene_id = msg.get("sceneId", "")
    bridge_id = msg.get("bridgeId", "")

    # Skip if already exists
    for obj in bpy.data.objects:
        if bridge_id and obj.get("sbox_bridge_id") == bridge_id:
            return
        if scene_id and obj.get("sbox_scene_id") == scene_id:
            return

    light_type_map = {"point": "POINT", "spot": "SPOT", "directional": "SUN"}
    blender_type = light_type_map.get(msg.get("lightType", "point"), "POINT")

    light_data = bpy.data.lights.new(name=f"{name}_light", type=blender_type)
    obj = bpy.data.objects.new(name, light_data)

    # Apply properties
    props = msg.get("properties", {})
    if props:
        _apply_light_properties(obj, props)

    col = get_or_create_sbox_collection()
    col.objects.link(obj)

    if bridge_id:
        obj["sbox_bridge_id"] = bridge_id
        obj["sbox_bridge_name"] = obj.name
        _last_known_bridge_ids.add(bridge_id)
    if scene_id:
        obj["sbox_scene_id"] = scene_id
    obj["sbox_type"] = "light"

    _apply_sbox_transform(obj, msg)
    log_activity(f"Light: {name} ({bridge_id or scene_id})")


def _apply_light_properties(obj, props):
    """Apply s&box light properties to a Blender light."""
    if not obj.data:
        return
    light = obj.data

    color = props.get("color", {})
    if color:
        light.color = (color.get("r", 1.0), color.get("g", 1.0), color.get("b", 1.0))

    radius = props.get("radius", 500)
    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0
    light.energy = radius * 10  # Approximate conversion

    if hasattr(light, "shadow_soft_size"):
        light.shadow_soft_size = radius * inv_sf * 0.1

    if light.type == "SPOT":
        cone_outer = props.get("coneOuter", 45)
        cone_inner = props.get("coneInner", 30)
        light.spot_size = math.radians(min(cone_outer * 2, 175))
        if cone_outer > 0:
            light.spot_blend = max(0.0, min(1.0, 1.0 - (cone_inner / cone_outer)))


def _create_model_placeholder(msg):
    """Create a Blender representation for an s&box model."""
    name = msg.get("name", "Model")
    scene_id = msg.get("sceneId", "")
    fbx_path = msg.get("fbxSourcePath")
    model_path = msg.get("modelPath", "unknown")

    for obj in bpy.data.objects:
        if obj.get("sbox_scene_id") == scene_id:
            return

    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0
    imported_obj = None

    if fbx_path:
        imported_obj = _import_fbx_as_reference(fbx_path, name, scene_id)

    if imported_obj:
        obj = imported_obj
        bounds = msg.get("bounds")
        if bounds and obj.type == 'MESH' and obj.data:
            _scale_to_sbox_bounds(obj, bounds, inv_sf)
    else:
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = 'CUBE'
        col = get_or_create_sbox_collection()
        col.objects.link(obj)

        bounds = msg.get("bounds")
        if bounds:
            mins = bounds.get("mins", {})
            maxs = bounds.get("maxs", {})
            sx_dim = abs(maxs.get("y", 0) - mins.get("y", 0)) * inv_sf
            sy_dim = abs(maxs.get("x", 0) - mins.get("x", 0)) * inv_sf
            sz_dim = abs(maxs.get("z", 0) - mins.get("z", 0)) * inv_sf
            obj.empty_display_size = 0.5
            obj.scale = (max(sx_dim, 0.01), max(sy_dim, 0.01), max(sz_dim, 0.01))
        else:
            obj.empty_display_size = 25 * inv_sf

    obj["sbox_scene_id"] = scene_id
    obj["sbox_type"] = "model"
    obj["sbox_model_path"] = model_path
    _apply_sbox_transform(obj, msg)


def _scale_to_sbox_bounds(obj, bounds, inv_sf):
    """Scale an imported FBX mesh to match the s&box model bounds."""
    mins = bounds.get("mins", {})
    maxs = bounds.get("maxs", {})

    sbox_dx = abs(maxs.get("y", 0) - mins.get("y", 0)) * inv_sf
    sbox_dy = abs(maxs.get("x", 0) - mins.get("x", 0)) * inv_sf
    sbox_dz = abs(maxs.get("z", 0) - mins.get("z", 0)) * inv_sf

    if sbox_dx < 0.001 or sbox_dy < 0.001 or sbox_dz < 0.001:
        return

    blender_dx = obj.dimensions.x if obj.dimensions.x > 0.001 else 1.0
    blender_dy = obj.dimensions.y if obj.dimensions.y > 0.001 else 1.0
    blender_dz = obj.dimensions.z if obj.dimensions.z > 0.001 else 1.0

    scale_x = sbox_dx / blender_dx
    scale_y = sbox_dy / blender_dy
    scale_z = sbox_dz / blender_dz
    uniform = (scale_x + scale_y + scale_z) / 3.0

    obj.scale = (uniform, uniform, uniform)


def _import_fbx_as_reference(fbx_path, name, scene_id=""):
    """Import an FBX file as a visual reference mesh."""
    import tempfile

    if not os.path.exists(fbx_path):
        return None

    try:
        temp_dir = os.path.join(tempfile.gettempdir(), "sbox_bridge_models")
        os.makedirs(temp_dir, exist_ok=True)
        base_name = os.path.basename(fbx_path)
        temp_path = os.path.join(temp_dir, f"ref_{base_name}")
        shutil.copy2(fbx_path, temp_path)

        existing = set(bpy.data.objects.keys())

        bpy.ops.import_scene.fbx(
            filepath=temp_path,
            use_custom_normals=True,
            use_image_search=False,
            ignore_leaf_bones=True,
            automatic_bone_orientation=False,
        )

        new_objs = [obj for obj in bpy.data.objects if obj.name not in existing]
        if not new_objs:
            return None

        for obj in new_objs:
            obj["sbox_scene_id"] = scene_id
            obj["sbox_type"] = "model"

        mesh_objs = [obj for obj in new_objs if obj.type == 'MESH']
        non_mesh = [obj for obj in new_objs if obj.type != 'MESH']

        if len(mesh_objs) > 1:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in mesh_objs:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = mesh_objs[0]
            bpy.ops.object.join()
            result = bpy.context.active_object
        elif mesh_objs:
            result = mesh_objs[0]
        else:
            result = new_objs[0]

        for obj in non_mesh:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)

        result.name = name
        result["sbox_scene_id"] = scene_id
        result["sbox_type"] = "model"
        result.location = (0, 0, 0)
        result.rotation_euler = (0, 0, 0)

        return result

    except Exception as e:
        print(f"[Bridge] FBX import failed for {fbx_path}: {e}")
        return None


# ── Mesh Extraction ─────────────────────────────────────────────────────

def _extract_mesh_data(obj, sf):
    """Extract mesh vertices, faces, and material data from a Blender object."""
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        if mesh is None:
            return None

        # Bake only world-cumulative scale into vertices. World rotation and
        # world position are sent separately and applied by s&box. Baking
        # rotation here would double-rotate (s&box re-applies WorldRotation);
        # baking translation would mis-place parented children.
        #
        # Use Matrix.decompose() rather than to_scale(): decompose returns
        # SIGNED scale and the matching proper rotation, so a reflected world
        # matrix (negative-scale objects, "S X -1" mirrors) is faithfully split
        # into rotation + signed scale. to_scale() returns absolute magnitudes
        # and to_euler() loses the sign, which silently drops the reflection
        # on the wire — s&box then shows un-reflected geometry and the user
        # sees inverted normals on that side.
        _, _, ws = obj.matrix_world.decompose()

        vertices = []
        for v in mesh.vertices:
            bx = v.co.x * ws.x * sf
            by = v.co.y * ws.y * sf
            bz = v.co.z * ws.z * sf
            cvt = blender_to_sbox_pos(bx, by, bz)
            vertices.extend(0.0 if (math.isnan(c) or math.isinf(c)) else c for c in cvt)

        # When the world matrix is reflected (negative determinant — happens
        # with negative scale, "S X -1" mirrors, some FBX/glTF imports),
        # baking the signed scale into verts above reflects the geometry into
        # the wire payload. But CCW-from-outside winding now reads as
        # CW-from-outside in s&box, because the reflection flipped the
        # apparent face orientation. Reverse winding (and matching UV order)
        # to restore CCW-from-outside under s&box's positive-det world frame.
        flip_winding = obj.matrix_world.determinant() < 0

        # Per-loop UVs from the active UV layer. Flat [u, v, u, v, ...] aligned
        # with face vertex order: face N's UVs immediately follow face N-1's.
        # V is flipped (1 - v) because Blender uses V=0 at bottom while Source
        # 2 / s&box conventionally treats V=0 at top — without the flip,
        # textures appear vertically inverted.
        uv_layer = mesh.uv_layers.active.data if mesh.uv_layers.active else None

        faces = []
        face_materials = []
        face_uvs = [] if uv_layer else None
        for poly in mesh.polygons:
            poly_verts = list(poly.vertices)
            if uv_layer:
                poly_uvs = [
                    (uv_layer[li].uv.x, 1.0 - uv_layer[li].uv.y)
                    for li in range(poly.loop_start, poly.loop_start + poly.loop_total)
                ]
            else:
                poly_uvs = None
            if flip_winding:
                poly_verts.reverse()
                if poly_uvs is not None:
                    poly_uvs.reverse()
            faces.append(len(poly_verts))
            faces.extend(poly_verts)
            face_materials.append(poly.material_index)
            if poly_uvs is not None:
                for u, v in poly_uvs:
                    face_uvs.append(u)
                    face_uvs.append(v)

        materials = _extract_materials(obj)
        eval_obj.to_mesh_clear()

        result = {"vertices": vertices, "faces": faces}
        if materials:
            result["materials"] = materials
            result["faceMaterials"] = face_materials
        if face_uvs is not None:
            result["faceUVs"] = face_uvs
        return result
    except Exception as e:
        print(f"[Bridge] Mesh extraction error: {e}")
        return None


def _extract_materials(obj):
    """Extract material data with hash-based caching."""
    if not obj.data or not obj.data.materials:
        return None

    materials = []
    for mat in obj.data.materials:
        mat_data = _extract_principled_bsdf(mat)
        content_hash = _hash_material(mat_data)
        mat_name = mat.name if mat else "default"

        cached = _material_hash_cache.get(mat_name)
        if cached and cached[0] == content_hash:
            mat_data["vmatPath"] = cached[1]
        else:
            vmat_path = _generate_vmat_and_copy_textures(mat_data)
            if vmat_path:
                mat_data["vmatPath"] = vmat_path
                _material_hash_cache[mat_name] = (content_hash, vmat_path)

        materials.append(mat_data)

    return materials if materials else None


def _hash_material(mat_data):
    """Content hash of material properties + texture file mtimes."""
    h = hashlib.md5()
    for key in sorted(mat_data.keys()):
        val = mat_data[key]
        if key.endswith("Texture") and val and isinstance(val, str) and os.path.isfile(val):
            try:
                h.update(f"{key}:{os.path.getmtime(val)}".encode())
            except Exception:
                h.update(f"{key}:{val}".encode())
        else:
            h.update(f"{key}:{val}".encode())
    return h.hexdigest()


def _extract_principled_bsdf(material):
    """Extract PBR values from a Principled BSDF material."""
    result = {
        "name": material.name if material else "default",
        "baseColor": [0.8, 0.8, 0.8, 1.0],
        "metallic": 0.0,
        "roughness": 0.5,
        "baseColorTexture": None,
        "roughnessTexture": None,
        "metallicTexture": None,
        "normalTexture": None,
        "normalStrength": 1.0,
        "emissionColor": [0.0, 0.0, 0.0],
        "emissionStrength": 0.0,
    }

    if not material or not material.node_tree:
        return result

    principled = None
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            principled = node
            break

    if not principled:
        return result

    bc = principled.inputs.get('Base Color')
    if bc:
        if bc.links:
            tex_path = _get_texture_path(bc.links[0].from_node)
            if tex_path:
                result["baseColorTexture"] = tex_path
        result["baseColor"] = list(bc.default_value[:4])

    met = principled.inputs.get('Metallic')
    if met:
        if met.links:
            tex_path = _get_texture_path(met.links[0].from_node)
            if tex_path:
                result["metallicTexture"] = tex_path
        result["metallic"] = met.default_value

    rough = principled.inputs.get('Roughness')
    if rough:
        if rough.links:
            tex_path = _get_texture_path(rough.links[0].from_node)
            if tex_path:
                result["roughnessTexture"] = tex_path
        result["roughness"] = rough.default_value

    norm = principled.inputs.get('Normal')
    if norm and norm.links:
        from_node = norm.links[0].from_node
        if from_node.type == 'NORMAL_MAP':
            result["normalStrength"] = from_node.inputs['Strength'].default_value
            color_in = from_node.inputs.get('Color')
            if color_in and color_in.links:
                tex_path = _get_texture_path(color_in.links[0].from_node)
                if tex_path:
                    result["normalTexture"] = tex_path
        elif from_node.type == 'TEX_IMAGE':
            tex_path = _get_texture_path(from_node)
            if tex_path:
                result["normalTexture"] = tex_path

    em_color = principled.inputs.get('Emission Color')
    if em_color:
        result["emissionColor"] = list(em_color.default_value[:3])
    em_str = principled.inputs.get('Emission Strength')
    if em_str:
        result["emissionStrength"] = em_str.default_value

    return result


def _get_texture_path(node):
    """Get absolute file path from an Image Texture node."""
    if node.type != 'TEX_IMAGE' or not node.image:
        return None
    if node.image.filepath:
        return bpy.path.abspath(node.image.filepath)
    return None


def _get_assets_path():
    """Get the s&box project Assets path from addon settings."""
    try:
        path = bpy.context.scene.sbox_bridge.project_assets_path
        if path and os.path.isdir(bpy.path.abspath(path)):
            return bpy.path.abspath(path)
    except Exception:
        pass
    return None


def _generate_vmat_and_copy_textures(mat_data):
    """Generate a .vmat file in the s&box project and copy textures.
    Returns the relative material path or None."""
    assets_dir = _get_assets_path()
    if not assets_dir:
        return None

    safe_name = "".join(
        c if c.isalnum() or c in "_-" else "_"
        for c in mat_data.get("name", "default")
    ).lower()
    bridge_dir = os.path.join(assets_dir, "materials", "blender_bridge")
    os.makedirs(bridge_dir, exist_ok=True)

    def copy_tex(abs_path, suffix):
        # Bare empty/None means the BSDF input has no texture — silent is correct.
        if not abs_path:
            return None
        # A path was specified but doesn't resolve. Surface this — most common
        # cause of "all surfaces are dev grid": image was loaded then file moved,
        # or image is packed into the .blend with no external file on disk.
        if not os.path.isfile(abs_path):
            add_warning(
                f"Texture missing for material '{safe_name}' ({suffix}): {abs_path}"
            )
            return None
        ext = os.path.splitext(abs_path)[1]
        dest_name = f"{safe_name}_{suffix}{ext}"
        dest_abs = os.path.join(bridge_dir, dest_name)
        try:
            shutil.copy2(abs_path, dest_abs)
            return f"materials/blender_bridge/{dest_name}"
        except Exception as e:
            add_warning(
                f"Texture copy failed for '{safe_name}' ({suffix}): {e}"
            )
            return None

    color_ref = copy_tex(mat_data.get("baseColorTexture"), "color")
    rough_ref = copy_tex(mat_data.get("roughnessTexture"), "rough")
    metal_ref = copy_tex(mat_data.get("metallicTexture"), "metal")
    normal_ref = copy_tex(mat_data.get("normalTexture"), "normal")

    bc = mat_data.get("baseColor", [0.8, 0.8, 0.8, 1.0])
    r = bc[0] if len(bc) > 0 else 0.8
    g = bc[1] if len(bc) > 1 else 0.8
    b = bc[2] if len(bc) > 2 else 0.8

    # When a color texture drives albedo, ignore the BSDF's Base Color value:
    # Blender leaves default_value at whatever it was before the texture was
    # plugged in, and writing it as g_vColorTint multiplies the texture by
    # that stale color (the "still blue after switching to an image" bug).
    if color_ref:
        r = g = b = 1.0

    em_str = mat_data.get("emissionStrength", 0.0)
    has_emission = em_str > 0.001

    lines = [
        "// AUTO-GENERATED BY BLENDER BRIDGE",
        "",
        "Layer0",
        "{",
        '\tshader "shaders/complex.shader"',
        "",
        "\tF_SPECULAR 1",
    ]

    if metal_ref:
        lines.append("\tF_METALNESS_TEXTURE 1")
    if has_emission:
        # Without this feature flag the complex shader silently ignores every
        # g_*SelfIllum* parameter — emission looked like it "did nothing".
        lines.append("\tF_SELF_ILLUM 1")

    lines.append("")
    lines.append(f'\tg_flModelTintAmount "1.000"')
    lines.append(f'\tg_vColorTint "[{r:.6f} {g:.6f} {b:.6f} 0.000000]"')
    if color_ref:
        lines.append(f'\tTextureColor "{color_ref}"')

    lines.append("")
    lines.append(f'\tg_flMetalness "{mat_data.get("metallic", 0.0):.3f}"')
    if metal_ref:
        lines.append(f'\tTextureMetalness "{metal_ref}"')

    lines.append("")
    lines.append(f'\tg_flRoughnessScaleFactor "{mat_data.get("roughness", 0.5):.3f}"')
    if rough_ref:
        lines.append(f'\tTextureRoughness "{rough_ref}"')

    if normal_ref:
        lines.append("")
        lines.append(f'\tTextureNormal "{normal_ref}"')

    if has_emission:
        ec = mat_data.get("emissionColor", [0, 0, 0])
        er = ec[0]
        eg = ec[1] if len(ec) > 1 else 0
        eb = ec[2] if len(ec) > 2 else 0
        # Blender parity: emission is its own color independent of albedo, so
        # AlbedoFactor 0, Tint = Emission Color, Scale = Emission Strength,
        # inline white mask (whole surface glows, like Blender's shader).
        lines.append("")
        lines.append('\tg_flSelfIllumAlbedoFactor "0.000"')
        lines.append('\tg_flSelfIllumBrightness "0.000"')
        lines.append(f'\tg_flSelfIllumScale "{em_str:.3f}"')
        lines.append(f'\tg_vSelfIllumTint "[{er:.6f} {eg:.6f} {eb:.6f} 0.000000]"')
        lines.append('\tTextureSelfIllumMask "[1.000000 1.000000 1.000000 0.000000]"')

    lines.append("")
    lines.append('\tg_vTexCoordScale "[1.000 1.000]"')
    lines.append('\tg_vTexCoordOffset "[0.000 0.000]"')
    lines.append("}")

    vmat_path = os.path.join(bridge_dir, f"{safe_name}.vmat")
    with open(vmat_path, "w") as f:
        f.write("\n".join(lines))

    rel_path = f"materials/blender_bridge/{safe_name}.vmat"
    return rel_path


def _extract_light_properties(obj):
    """Extract Blender light properties for the wire protocol."""
    if not obj.data:
        return {}
    light = obj.data
    sf = _get_scale_factor()

    props = {
        "color": {"r": light.color[0], "g": light.color[1], "b": light.color[2]},
        "shadows": light.use_shadow if hasattr(light, "use_shadow") else True,
    }

    if light.type == "POINT":
        props["radius"] = getattr(light, "shadow_soft_size", 1.0) * sf * 10
    elif light.type == "SPOT":
        props["radius"] = getattr(light, "shadow_soft_size", 1.0) * sf * 10
        props["coneOuter"] = math.degrees(light.spot_size) / 2
        blend = getattr(light, "spot_blend", 0.0)
        props["coneInner"] = props["coneOuter"] * (1.0 - blend)
    elif light.type == "SUN":
        props["radius"] = 10000

    return props


# ── Transform Helpers ────────────────────────────────────────────────────

def _rotation_to_sbox(obj):
    """World rotation for the wire, taken from matrix_world.decompose().

    decompose(), not to_euler(): for a reflected world matrix (negative
    scale, mirrors) decompose returns the proper rotation that pairs with
    the SIGNED scale _extract_mesh_data bakes into the vertices. to_euler()
    reads the un-fixed improper matrix and returns a rotation ~180° away
    from that pairing, which mis-orients every mirrored object in s&box.
    For normal matrices the two are identical.

    Conjugate through the axis change (R_s = AXIS_B2S @ R_b @ AXIS_S2B)
    and read the s&box triple off the 'XYZ' euler of the result — s&box's
    Rz(yaw) @ Ry(pitch) @ Rx(roll) is 'XYZ' order with (roll, pitch, yaw).
    See _sbox_rotation_matrix, the exact inverse of this."""
    _, rq, _ = obj.matrix_world.decompose()
    r_s = _AXIS_B2S @ rq.to_matrix() @ _AXIS_S2B
    e = r_s.to_euler('XYZ')
    return {
        "pitch": math.degrees(e.y),
        "yaw": math.degrees(e.z),
        "roll": math.degrees(e.x),
    }


def _apply_sbox_transform(obj, msg):
    """Apply s&box position/rotation to a Blender object."""
    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0

    new_loc = None
    if "position" in msg:
        p = msg["position"]
        bx, by, bz = sbox_to_blender_pos(
            p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0)
        )
        new_loc = (bx * inv_sf, by * inv_sf, bz * inv_sf)

    new_quat = None
    if "rotation" in msg:
        r = msg["rotation"]
        new_quat = _sbox_rotation_matrix(
            r.get("pitch", 0.0), r.get("yaw", 0.0), r.get("roll", 0.0)
        ).to_quaternion()

    if new_quat is not None and obj.matrix_world.determinant() < 0:
        # Reflected (mirrored) object. The wire rotation pairs with the signed
        # scale that decompose() spreads across all three axes — writing it
        # straight into rotation_euler would pair it with the object's local
        # scale signs instead and mis-orient it by 180°. Rotate the whole
        # world basis by the delta so decompose() lands exactly on the target
        # rotation while the reflection is preserved. Position rides along in
        # the same matrix write: obj.location wouldn't be visible in
        # matrix_world until the next depsgraph evaluation.
        m = obj.matrix_world.copy()
        _, current_q, _ = m.decompose()
        delta = new_quat @ current_q.inverted()
        new_m = (delta.to_matrix() @ m.to_3x3()).to_4x4()
        new_m.translation = new_loc if new_loc is not None else m.to_translation()
        obj.matrix_world = new_m
        return

    if new_loc is not None:
        obj.location = new_loc
    if new_quat is not None:
        if obj.rotation_mode == 'QUATERNION':
            obj.rotation_quaternion = new_quat
        else:
            try:
                obj.rotation_euler = new_quat.to_euler(obj.rotation_mode)
            except ValueError:
                # AXIS_ANGLE mode isn't a euler order — fall back to XYZ,
                # matching the old behavior for that mode.
                obj.rotation_euler = new_quat.to_euler('XYZ')


# ── Depsgraph Handler ───────────────────────────────────────────────────

_last_play_mute_note = 0.0


def _note_play_mute():
    """Surface the play-mode outbound mute instead of dropping moves silently."""
    global _last_play_mute_note
    now = time.time()
    if now - _last_play_mute_note > 10.0:
        _last_play_mute_note = now
        log_activity("Outbound muted: play-mode flag set (self-clears via poll)")


def _mark_dirty(kind, key, obj):
    """Queue an outbound send for the poll timer to flush. Last mark for a
    key wins — the flush reads the object's current state, so intermediate
    drag positions are never sent."""
    global _first_dirty_time
    if not _dirty_sends:
        _first_dirty_time = time.time()
    _dirty_sends[key] = (kind, obj)


# At most this many creates (full mesh extraction + material generation +
# blocking send_and_receive each) go out per flush tick. A fresh-engine
# repopulate queues the ENTIRE scene at once; unthrottled, Blender froze
# for the whole batch ("lots of objects cause issues on connect"). The
# remainder stays queued and trickles out on subsequent ticks — everything
# still arrives, just without the freeze.
HEAVY_SENDS_PER_FLUSH = 3


def _flush_dirty_sends():
    """Drain the dirty-set. Runs from the poll timer, never from the
    depsgraph handler. Returns the number of entries drained."""
    global _first_dirty_time
    if not _dirty_sends:
        _first_dirty_time = None
        return 0
    pending = list(_dirty_sends.items())
    _dirty_sends.clear()
    _first_dirty_time = None
    heavy_budget = HEAVY_SENDS_PER_FLUSH
    deferred = {}
    for key, (kind, obj) in pending:
        if kind in ("create", "create_light") and heavy_budget <= 0:
            deferred[key] = (kind, obj)
            continue
        # Object may have been deleted between mark and flush.
        try:
            if obj is None or not obj.name:
                continue
            # Held in Bridge Trash — nothing goes out for it. (Restore pops
            # the quarantine entry BEFORE queuing its re-create, so restored
            # objects pass this check.)
            _bid = get_bridge_id(obj)
            if _bid and _bid in _inbound_pending_deletes:
                continue
        except ReferenceError:
            continue
        try:
            if kind == "transform":
                send_update_transform(obj, force=True)
            elif kind == "light":
                send_update_light(obj, force=True)
            elif kind == "scene":
                send_scene_transform(obj, force=True)
            elif kind == "create":
                # send_create itself skips ids the engine already has.
                # Objects now KEEP their id across engine sessions, so a
                # bare "has id -> skip" here would block every re-create
                # (restore-from-trash, fresh-engine repopulate, not-found
                # recovery).
                send_create(obj)
                heavy_budget -= 1
            elif kind == "create_light":
                send_create_light(obj)
                heavy_budget -= 1
        except ReferenceError:
            continue
        except Exception as e:
            print(f"[Bridge] Deferred send failed ({kind}, {key}): {e}")
    if deferred:
        # Newer marks made during this flush win over the deferred entries.
        for k, v in deferred.items():
            _dirty_sends.setdefault(k, v)
        # Restart the stall canary — this backlog is intentional pacing,
        # not a dead flush.
        _first_dirty_time = time.time()
    return len(pending) - len(deferred)


@persistent
def on_depsgraph_update(scene, depsgraph):
    """Called after every depsgraph update. Marks changed objects in the
    dirty-set; the poll timer does the actual network I/O. Only the mesh
    debounce path keeps its own timer (it already deferred).

    @persistent is load-bearing: without it, Blender FREES this handler on
    every .blend load (documented bpy.app.handlers behavior). The failure is
    perfectly silent and asymmetric — connection, polling, inbound applies,
    and all operator buttons keep working; only live outbound dies. That was
    the root cause of the long-running 'Blender→engine stops syncing after a
    restart' intermittency."""
    if _suppress_depsgraph:
        return
    if not connection.is_connected():
        return
    # Watchdog: if the poll timer died (an exception in a past tick), nothing
    # else restarts it — and the play-mode flag can then never self-heal from
    # the poll response, muting all outbound while the panel reads Connected.
    # Resurrect it on user activity.
    try:
        if not bpy.app.timers.is_registered(_poll_and_process):
            start_timer()
            log_activity("Poll timer was dead — restarted")
    except Exception:
        pass
    if _play_mode_active:
        _note_play_mute()
        return

    try:
        settings = scene.sbox_bridge
        if not settings.auto_sync:
            return
    except Exception:
        return

    # Manual mode — skip all auto-sends (user must use buttons)
    mode = get_sync_mode()
    if mode == 'MANUAL':
        return

    now = time.time()

    for update in depsgraph.updates:
        # Material datablock updates: editing a material's properties (color
        # slider, texture node link, image swap) doesn't fire an Object update
        # in Blender's depsgraph — only the Material datablock fires. Walk
        # bpy.data.objects, find every mesh that has this material in any
        # slot, and queue a mesh re-sync. Without this, the user has to nudge
        # geometry to push material edits through.
        if isinstance(update.id, bpy.types.Material):
            try:
                mat = update.id.original
            except Exception:
                mat = update.id
            mat_name = mat.name if mat else None
            if not mat_name:
                continue
            # Name-based slot match. Identity comparison (`slot is mat`) is
            # fragile across depsgraph evaluated/original boundaries — name
            # is what Blender actually keys materials on.
            for using_obj in bpy.data.objects:
                if using_obj.type != "MESH" or using_obj.data is None:
                    continue
                if not any(slot is not None and slot.name == mat_name
                           for slot in using_obj.data.materials):
                    continue
                if _should_skip_object(using_obj):
                    continue
                bid = get_bridge_id(using_obj)
                if bid:
                    # Clear stored hash so the debounced send_update_mesh
                    # doesn't gate on a hash that hasn't yet caught up to the
                    # material edit.
                    if "sbox_bridge_hash" in using_obj:
                        del using_obj["sbox_bridge_hash"]
                    set_sync_status(using_obj, "modified")
                    _schedule_mesh_update(bid, using_obj)
            continue

        # Accept Object updates only
        if not isinstance(update.id, bpy.types.Object):
            continue

        # Get the original object (not the evaluated depsgraph copy)
        try:
            obj = update.id.original
        except Exception:
            obj = update.id
        if obj is None:
            continue

        # Skip objects whose transforms were recently set by s&box (echo prevention).
        # 200ms window covers any delayed depsgraph fires.
        bid = get_bridge_id(obj)
        if bid and bid in _remote_update_times:
            if now - _remote_update_times[bid] < 0.2:
                continue
            else:
                del _remote_update_times[bid]

        # Scene objects (models/lights from s&box) — position updates only
        if obj.get("sbox_scene_id") or obj.get("sbox_type"):
            if not update.is_updated_geometry:
                sid = obj.get("sbox_scene_id") or obj.name
                _mark_dirty("scene", f"scene_{sid}", obj)
            continue

        # Skip unsupported and convertible types
        if obj.type in UNSUPPORTED_TYPES or obj.type in CONVERTIBLE_TYPES:
            continue

        # Lights
        if obj.type == "LIGHT":
            if obj.data and obj.data.type in UNSUPPORTED_LIGHT_TYPES:
                continue
            bridge_id = get_bridge_id(obj)
            if bridge_id:
                # Same stolen-ID window as meshes: a duplicated light drives
                # the source's engine light until the sweep re-keys it.
                stamp = obj.get("sbox_bridge_name")
                if stamp is not None and stamp != obj.name:
                    owner = None
                    for other in bpy.data.objects:
                        if other is not obj and get_bridge_id(other) == bridge_id:
                            owner = other
                            break
                    if owner is not None:
                        _strip_bridge_props(obj)
                        uid = getattr(obj, "session_uid", id(obj))
                        _mark_dirty("create_light", f"create_{uid}", obj)
                        _mark_dirty("light", bridge_id, owner)
                        continue
                    obj["sbox_bridge_name"] = obj.name
                _mark_dirty("light", bridge_id, obj)
            elif not obj.get("sbox_scene_id"):
                uid = getattr(obj, "session_uid", id(obj))
                _mark_dirty("create_light", f"create_{uid}", obj)
            continue

        # Non-mesh — skip
        if obj.type != "MESH":
            continue

        # Skip hidden/cutter objects
        if _should_skip_object(obj):
            continue

        # Mesh objects
        bridge_id = get_bridge_id(obj)

        if bridge_id:
            # A fresh duplicate carries the SOURCE's ID until the 100ms
            # duplicate sweep re-keys it — transforms sent in that window move
            # the source's engine object (the original visibly twitches while
            # you drag the copy, then rests slightly off). A copy's
            # sbox_bridge_name stamp doesn't match its own name — that's the
            # cheap tell; confirm with a scan before touching the wire.
            stamp = obj.get("sbox_bridge_name")
            if stamp is not None and stamp != obj.name:
                owner = None
                for other in bpy.data.objects:
                    if other is not obj and get_bridge_id(other) == bridge_id:
                        owner = other
                        break
                if owner is not None:
                    # This is the copy: re-key it now, never send under the
                    # stolen ID, and correct the source in case stray moves
                    # already landed on its engine object.
                    _strip_bridge_props(obj)
                    uid = getattr(obj, "session_uid", id(obj))
                    _mark_dirty("create", f"create_{uid}", obj)
                    _mark_dirty("transform", bridge_id, owner)
                    continue
                # No other holder — the object was just renamed. Adopt the
                # new name so this scan doesn't rerun every event.
                obj["sbox_bridge_name"] = obj.name

            # Read scale from the evaluated copy (update.id) since .original
            # may not have the updated scale during interactive transforms
            eval_scale = tuple(round(s, 4) for s in update.id.scale)
            is_geo = update.is_updated_geometry
            is_scale = _scale_changed_with(bridge_id, eval_scale)
            # is_updated_shading fires on material slot reassignment and on
            # active-UV-layer swaps — both need a full mesh re-sync, not just
            # a transform update.
            is_shading = getattr(update, "is_updated_shading", False)
            if is_geo or is_scale or is_shading:
                set_sync_status(obj, "modified")
                _schedule_mesh_update(bridge_id, obj)
            else:
                _mark_dirty("transform", bridge_id, obj)
        else:
            # Paste-dupe detection stays inline (cheap, no I/O) so a stale
            # inherited ID is stripped before anything reads it. The create
            # itself is deferred: send_create does full mesh extraction plus
            # a blocking send_and_receive — the single worst in-handler stall
            # (fires mid-drag on Ctrl+D duplicates).
            _detect_and_strip_paste_duplicate(obj)
            if not get_bridge_id(obj):
                uid = getattr(obj, "session_uid", id(obj))
                _mark_dirty("create", f"create_{uid}", obj)


# ── Mesh Update Debounce ────────────────────────────────────────────────

def _scale_changed(obj, bridge_id):
    """Check if the object's scale changed since last check."""
    current = tuple(round(s, 4) for s in obj.scale)
    return _scale_changed_with(bridge_id, current)


def _scale_changed_with(bridge_id, current_scale):
    """Check if scale changed, given pre-computed scale tuple."""
    prev = _last_scale.get(bridge_id)
    _last_scale[bridge_id] = current_scale
    if prev is None:
        return False
    return current_scale != prev


def _schedule_mesh_update(bridge_id, obj):
    """Debounce mesh updates to avoid flooding during interactive edits."""
    _mesh_debounce_obj[bridge_id] = obj

    if bridge_id in _mesh_debounce_scheduled:
        return

    _mesh_debounce_scheduled.add(bridge_id)

    def do_update():
        _mesh_debounce_scheduled.discard(bridge_id)
        latest_obj = _mesh_debounce_obj.pop(bridge_id, None)
        if latest_obj is None:
            return None
        try:
            if latest_obj.name:
                send_update_mesh(latest_obj)
        except ReferenceError:
            pass
        return None

    bpy.app.timers.register(do_update, first_interval=MESH_DEBOUNCE_INTERVAL)


def _detect_and_strip_paste_duplicate(obj):
    """Detect if an object was pasted and inherited a stale bridge ID."""
    bid = get_bridge_id(obj)
    if not bid:
        return
    for other in bpy.data.objects:
        if other != obj and get_bridge_id(other) == bid:
            clear_bridge_id(obj)
            if "_remote_update_time" in obj:
                del obj["_remote_update_time"]
            return


# ── Timer: poll for messages + detect deletions ─────────────────────────

def _poll_and_process():
    """Blender timer callback: poll s&box and process messages."""
    global _last_sbox_seq_processed, _current_session_id, _last_grid_sent, \
        _play_mode_active

    if not connection.is_connected():
        try:
            if hasattr(bpy.context, "scene") and hasattr(bpy.context.scene, "sbox_bridge"):
                bpy.context.scene.sbox_bridge.is_connected = False
        except Exception:
            pass
        if connection.is_reconnecting():
            # Watchdog the retry organ itself — an unguarded bpy timer is
            # exactly what silently died once before.
            connection.ensure_reconnect_timer()
            return 0.5  # Keep timer alive during reconnect
        return None  # Stop timer — disconnected

    # Self-heal: anything that strips our handlers (a .blend load before
    # @persistent existed, another addon clearing the list) kills live
    # outbound with zero errors while inbound keeps working. Re-attach here
    # — this timer is the one thing guaranteed to still be running.
    try:
        if on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(on_depsgraph_update)
            log_activity("Depsgraph handler was missing — re-attached (live outbound restored)")
        if on_undo_post not in bpy.app.handlers.undo_post:
            bpy.app.handlers.undo_post.append(on_undo_post)
        if on_undo_post not in bpy.app.handlers.redo_post:
            bpy.app.handlers.redo_post.append(on_undo_post)
    except Exception:
        pass

    try:
        response = connection.poll()
        if response is None:
            return 0.1

        # Level-triggered play-mode sync. The edge-triggered play_mode
        # messages can be missed (disconnect, restart while playing, spurious
        # broadcast around a hot-reload), leaving _play_mode_active stuck True
        # — which silently kills ALL depsgraph outbound (moves/edits) while
        # timer paths (creates, deletes) keep working. The poll response
        # carries ground truth every tick; trust it over the edges.
        playing = response.get("playing")
        if playing is not None and bool(playing) != _play_mode_active:
            _play_mode_active = bool(playing)
            add_warning(
                f"Play mode {'ACTIVE — live sync paused' if _play_mode_active else 'inactive — live sync resumed'}"
                " (synced from poll)"
            )

        # Detect session change (s&box restarted or hot-reloaded)
        session_id = response.get("sessionId")
        if session_id and session_id != _current_session_id:
            global _expect_fresh_engine
            _expect_fresh_engine = True
            _current_session_id = session_id
            _last_write_seq.clear()
            _last_sbox_seq_processed = 0
            _dead_scene_ids.clear()
            # New session knows nothing about our grid — clear the echo guard
            # so the next grid change (either direction) always goes through.
            _last_grid_sent = None
            if _get_sync_on_connect():
                log_activity(f"Session changed to {session_id}, resyncing...")
                send_sync()
            else:
                log_activity(f"Session changed to {session_id}")
                add_warning(
                    "Engine session changed — press Sync All to reconcile "
                    "(auto-sync on connect is off)"
                )
            return 0.1

        msgs = response.get("messages", [])
        for msg in msgs:
            seq = msg.get("seq", 0)
            process_incoming(msg)
            if seq > _last_sbox_seq_processed:
                _last_sbox_seq_processed = seq

    except Exception as e:
        print(f"[Bridge] Poll error: {e}")
        msgs = []

    # Drain deferred outbound marked by the depsgraph handler.
    flushed = 0
    try:
        flushed = _flush_dirty_sends()
    except Exception as e:
        print(f"[Bridge] _flush_dirty_sends error: {e}")

    # An uncaught exception below would kill this timer permanently while the
    # connection still reads CONNECTED — no more polls, no play-state
    # self-heal, panel green, outbound dead. Contain each step so one bad
    # tick can't take the loop down.
    for step in (_check_duplicates, _check_deletions, _check_hidden,
                 _confirm_pending_deletes):
        try:
            step()
        except Exception as e:
            print(f"[Bridge] {step.__name__} error: {e}")
    # Adaptive poll: burst to ~30Hz while s&box is streaming messages, tick
    # at ~20Hz while outbound is flowing (keeps drag latency at the old
    # direct-send feel), idle at 10Hz otherwise.
    return 0.03 if msgs else (0.05 if flushed else 0.1)


def _check_duplicates():
    """Clean up stale bridge properties and detect duplicate IDs."""
    if not connection.is_connected():
        return

    # Strip bridge IDs from non-syncable types (only MESH and LIGHT should have them)
    # Use _strip_bridge_props which also removes from _last_known_bridge_ids
    # so _check_deletions won't send delete messages for these
    for obj in list(bpy.data.objects):
        bid = get_bridge_id(obj)
        if bid and obj.type not in SYNCABLE_TYPES:
            # Silently strip — don't trigger deletes for curves/armatures/etc
            clear_bridge_id(obj)
            if "_remote_update_time" in obj:
                del obj["_remote_update_time"]
            _last_known_bridge_ids.discard(bid)
            _last_write_seq.pop(bid, None)
            # Also remove from pending deletes if it got added
            _pending_deletes[:] = [(b, t) for b, t in _pending_deletes if b != bid]
            continue

    # Detect duplicate IDs on remaining objects
    seen = {}
    for obj in list(bpy.data.objects):
        bid = obj.get("sbox_bridge_id")
        if not bid:
            continue
        # Trashed objects keep their id but are inert — don't let them win
        # (or lose) a duplicate-id fight with a live object.
        if bid in _inbound_pending_deletes:
            continue
        if bid in seen:
            # Two objects claim one id. Decide which is the ORIGINAL owner:
            # iteration order is alphabetical and Blender's gap-filling names
            # can make the copy sort FIRST (dupe of Cube.017 becomes
            # Cube.013), so first-wins would keep the copy and strip the
            # original — whose idempotent re-create returns the SAME id and
            # the pair collides again forever. The create stamps
            # sbox_bridge_name; the object whose stamp matches its own name
            # is the original.
            keep, strip = seen[bid], obj
            keep_owns = keep.get("sbox_bridge_name") == keep.name
            obj_owns = obj.get("sbox_bridge_name") == obj.name
            if obj_owns and not keep_owns:
                keep, strip = obj, seen[bid]
                seen[bid] = keep
            _strip_bridge_props(strip)
            if strip.type == "MESH":
                send_create(strip)
            elif strip.type == "LIGHT":
                send_create_light(strip)
            # The keeper's engine object may have eaten stray moves while the
            # copy still carried its ID — re-send its true transform.
            if keep.type == "MESH":
                _mark_dirty("transform", bid, keep)
            elif keep.type == "LIGHT":
                _mark_dirty("light", bid, keep)
        else:
            seen[bid] = obj


def _check_deletions():
    """Detect bridge objects deleted in Blender, add to pending deletes."""
    global _last_known_bridge_ids

    if not connection.is_connected():
        return

    current_ids = set()
    for obj in bpy.data.objects:
        # get_bridge_id, not obj.get: must be the same view of identity that
        # the rest of the addon uses, or an object whose obj-side prop was
        # lost (undo) but is still recoverable via its data mirror gets a
        # spurious delete sent for it.
        bid = get_bridge_id(obj)
        # Quarantined objects keep their bid but must stay OUT of the
        # engine-known set: re-adding them here would (a) fire an outbound
        # delete when the user confirms the trash, and (b) make send_create
        # skip the re-create when the user restores it.
        if bid and bid not in _inbound_pending_deletes:
            current_ids.add(bid)

    deleted = _last_known_bridge_ids - current_ids
    added = False
    for bid in deleted:
        # Don't add if already pending
        if not any(b == bid for b, _ in _pending_deletes):
            _pending_deletes.append((bid, time.time()))
            added = True
    if added:
        connection.notify_ui()

    _last_known_bridge_ids = current_ids


def _check_hidden():
    """Detect bridge objects hidden/unhidden in Blender, sync to s&box."""
    if not connection.is_connected():
        return

    for obj in list(bpy.data.objects):
        bid = get_bridge_id(obj)
        if not bid:
            continue
        # Bridge Trash is a hidden collection — without this, quarantine
        # would fire set_visibility at an engine object that no longer exists.
        if bid in _inbound_pending_deletes:
            continue

        is_hidden = _should_skip_object(obj)

        if is_hidden and bid not in _hidden_bridge_ids:
            # Just became hidden — disable in s&box. NOT a delete: destroying
            # the GameObject threw away engine-side material/texture work and
            # forced a full mesh rebuild on unhide.
            _hidden_bridge_ids.add(bid)
            send_visibility(bid, False)
            log_activity(f"Hidden: {obj.name} disabled in s&box")

        elif not is_hidden and bid in _hidden_bridge_ids:
            # Just became visible again — re-enable the same engine object.
            _hidden_bridge_ids.discard(bid)
            send_visibility(bid, True)
            # Catch up edits made while hidden: the depsgraph handler skips
            # hidden objects, so anything changed in between never went out.
            try:
                if obj.type == "MESH":
                    if geometry_hash(obj) != get_stored_hash(obj):
                        set_sync_status(obj, "modified")
                        _schedule_mesh_update(bid, obj)
                    else:
                        _mark_dirty("transform", bid, obj)
                elif obj.type == "LIGHT":
                    _mark_dirty("light", bid, obj)
            except Exception:
                pass
            log_activity(f"Unhidden: {obj.name} re-enabled in s&box")


_bulk_delete_warned = False


def _confirm_pending_deletes():
    """Auto-confirm pending deletes after timeout. Bulk deletions never
    auto-confirm — they wait for the explicit Confirm Deletes button."""
    global _bulk_delete_warned
    if len(_pending_deletes) > PENDING_DELETE_BULK_GATE:
        if not _bulk_delete_warned:
            _bulk_delete_warned = True
            add_warning(
                f"{len(_pending_deletes)} deletes pending — bulk deletions "
                "need Confirm Deletes in the panel (no auto-confirm)"
            )
        return
    _bulk_delete_warned = False
    now = time.time()
    remaining = []
    for bid, timestamp in _pending_deletes:
        if now - timestamp >= PENDING_DELETE_TIMEOUT:
            send_delete(bid)
        else:
            remaining.append((bid, timestamp))
    _pending_deletes.clear()
    _pending_deletes.extend(remaining)


def _strip_bridge_props(obj):
    """Remove all bridge-related custom properties from an object."""
    bid = get_bridge_id(obj)
    clear_bridge_id(obj)
    if "_remote_update_time" in obj:
        del obj["_remote_update_time"]
    if bid:
        _last_known_bridge_ids.discard(bid)
        _last_write_seq.pop(bid, None)


# ── Collection Management ───────────────────────────────────────────────

def get_or_create_sbox_collection():
    """Get or create the 's&box Scene' collection for s&box-originated objects."""
    scene_col = bpy.context.scene.collection
    for col in scene_col.children:
        if col.name == "s&box Scene":
            return col
    col = bpy.data.collections.new("s&box Scene")
    scene_col.children.link(col)
    return col


# ── Timer Management ────────────────────────────────────────────────────

def start_timer():
    global _timer_running
    # _timer_running can lie: if _poll_and_process ever raised, Blender
    # unregistered the timer but the flag stayed True, making start_timer a
    # permanent no-op. Ask the timer registry for ground truth.
    try:
        alive = bpy.app.timers.is_registered(_poll_and_process)
    except Exception:
        alive = _timer_running
    if not alive:
        bpy.app.timers.register(_poll_and_process, first_interval=0.1)
    _timer_running = True


def stop_timer():
    global _timer_running
    if _timer_running:
        try:
            bpy.app.timers.unregister(_poll_and_process)
        except Exception:
            pass
        _timer_running = False
