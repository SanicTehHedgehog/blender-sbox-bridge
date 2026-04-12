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
import bmesh
import hashlib
import json
import math
import os
import shutil
import time
from . import connection


# ── Module State ──────────────────────────────────────────────────────────

_suppress_depsgraph = False
_timer_running = False

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

# Material caching
_material_hash_cache = {}       # material_name -> (content_hash, vmat_rel_path)

# Warnings & pending deletes
_warnings = []                  # [(timestamp, message)]
_pending_deletes = []           # [(bridgeId, timestamp)]

# Chunked mesh state
_chunked_streams = {}           # bridgeId -> stream state dict

# Play mode
_play_mode_active = False

# Constants
CHUNK_VERTEX_LIMIT = 20000
TRANSFORM_SEND_INTERVAL = 0.05  # 20Hz max
MESH_DEBOUNCE_INTERVAL = 0.15
PENDING_DELETE_TIMEOUT = 5.0

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


# ── Bridge ID Helpers ────────────────────────────────────────────────────

def get_bridge_id(obj):
    return obj.get("sbox_bridge_id")


def set_bridge_id(obj, bridge_id):
    obj["sbox_bridge_id"] = bridge_id


def find_by_bridge_id(bridge_id):
    for obj in bpy.data.objects:
        if obj.get("sbox_bridge_id") == bridge_id:
            return obj
    return None


def _get_scale_factor():
    try:
        return bpy.context.scene.sbox_bridge.scale_factor
    except Exception:
        return 1.0


# ── Warnings & Status ────────────────────────────────────────────────────

def add_warning(message):
    _warnings.append((time.time(), message))
    if len(_warnings) > 10:
        _warnings.pop(0)
    print(f"[Bridge] WARNING: {message}")


def get_warnings():
    return list(_warnings)


def get_pending_deletes():
    return list(_pending_deletes)


def cancel_pending_deletes():
    for bid, _ in _pending_deletes:
        _last_known_bridge_ids.add(bid)
    _pending_deletes.clear()


def is_play_mode():
    return _play_mode_active


# ── Outgoing: Blender -> s&box ──────────────────────────────────────────

def send_create(obj):
    """Send a create message. s&box assigns the bridge ID synchronously.
    Detects paste duplicates and uses idempotency keys."""
    global _blender_seq

    # Detect paste duplicate: another object has the same bridgeId
    bid = obj.get("sbox_bridge_id")
    if bid:
        for other in bpy.data.objects:
            if other != obj and other.get("sbox_bridge_id") == bid:
                # This is a pasted copy — strip the stale ID
                if "sbox_bridge_id" in obj:
                    del obj["sbox_bridge_id"]
                bid = None
                break

    # If it already has a unique ID, don't create again
    if get_bridge_id(obj):
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

        px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)
        idem_key = f"{obj.name}_{getattr(obj, 'session_uid', id(obj))}"

        _blender_seq += 1
        msg = {
            "type": "create",
            "seq": _blender_seq,
            "ack": _last_sbox_seq_processed,
            "name": obj.name,
            "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
            "rotation": _rotation_to_sbox(obj),
            "meshData": mesh_data,
            "idempotencyKey": idem_key,
        }

        response = connection.send_and_receive(msg)
        if response and "bridgeId" in response:
            set_bridge_id(obj, response["bridgeId"])
            _last_write_seq[response["bridgeId"]] = _blender_seq
            _last_known_bridge_ids.add(response["bridgeId"])
            print(f"[Bridge] Created: {obj.name} -> {response['bridgeId']}")
        else:
            print(f"[Bridge] Create failed '{obj.name}': {response}")
    except Exception as e:
        print(f"[Bridge] Create error '{obj.name}': {e}")


def send_update_transform(obj):
    """Send a transform-only update with 20Hz rate limiting."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    # 20Hz debounce per object
    now = time.time()
    if now - _last_transform_send.get(bridge_id, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[bridge_id] = now

    _blender_seq += 1
    _last_write_seq[bridge_id] = _blender_seq

    sf = _get_scale_factor()
    px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)

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
    """Send a full mesh update. Uses chunked transfer for large meshes."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    sf = _get_scale_factor()
    mesh_data = _extract_mesh_data(obj, sf)
    if mesh_data is None:
        return

    # Check if chunked transfer needed
    vert_count = len(mesh_data.get("vertices", [])) // 3
    if vert_count > CHUNK_VERTEX_LIMIT:
        _send_chunked_mesh(obj, bridge_id, mesh_data)
        return

    px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)

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
    }
    connection.send(msg)


def _send_chunked_mesh(obj, bridge_id, mesh_data):
    """Send large mesh via chunked protocol using Blender timers."""
    global _blender_seq

    vertices = mesh_data.get("vertices", [])
    faces = mesh_data.get("faces", [])
    face_materials = mesh_data.get("faceMaterials", [])
    materials = mesh_data.get("materials", [])

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
                    px, py, pz = blender_to_sbox_pos(o.location.x, o.location.y, o.location.z)
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
    print(f"[Bridge] Sent delete: {bridge_id}")


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

    sf = _get_scale_factor()
    px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)
    props = _extract_light_properties(obj)
    idem_key = f"light_{obj.name}_{getattr(obj, 'session_uid', id(obj))}"

    _blender_seq += 1
    msg = {
        "type": "create_light",
        "seq": _blender_seq,
        "ack": _last_sbox_seq_processed,
        "name": obj.name,
        "lightType": sbox_light_type,
        "position": {"x": px * sf, "y": py * sf, "z": pz * sf},
        "rotation": _rotation_to_sbox(obj),
        "properties": props,
        "idempotencyKey": idem_key,
    }

    response = connection.send_and_receive(msg)
    if response and "bridgeId" in response:
        set_bridge_id(obj, response["bridgeId"])
        _last_write_seq[response["bridgeId"]] = _blender_seq
        _last_known_bridge_ids.add(response["bridgeId"])
        print(f"[Bridge] Created light: {obj.name} -> {response['bridgeId']}")
    else:
        print(f"[Bridge] Light create failed '{obj.name}': {response}")


def send_update_light(obj):
    """Send light property + transform update to s&box."""
    global _blender_seq

    bridge_id = get_bridge_id(obj)
    if not bridge_id:
        return

    now = time.time()
    if now - _last_transform_send.get(bridge_id, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[bridge_id] = now

    sf = _get_scale_factor()
    px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)
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


def send_scene_transform(obj):
    """Send position update for a scene object (model/light from s&box)."""
    scene_id = obj.get("sbox_scene_id")
    if not scene_id:
        return

    key = f"scene_{scene_id}"
    now = time.time()
    if now - _last_transform_send.get(key, 0) < TRANSFORM_SEND_INTERVAL:
        return
    _last_transform_send[key] = now

    global _blender_seq
    sf = _get_scale_factor()
    px, py, pz = blender_to_sbox_pos(obj.location.x, obj.location.y, obj.location.z)

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
        elif msg_type == "scene_updated":
            _handle_scene_updated(msg)
        elif msg_type == "light_updated":
            _handle_light_updated(msg)
        elif msg_type == "play_mode":
            _handle_play_mode(msg)
    finally:
        _suppress_depsgraph = False


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

    _apply_sbox_transform(obj, msg)
    mesh_data = msg.get("meshData")
    if mesh_data and mesh_data.get("vertices"):
        _rebuild_mesh(obj, mesh_data)


def _handle_deleted(msg):
    """s&box deleted a bridge object."""
    bridge_id = msg.get("bridgeId")
    if not bridge_id:
        return

    obj = find_by_bridge_id(bridge_id)
    if obj:
        bpy.data.objects.remove(obj, do_unlink=True)
    _last_known_bridge_ids.discard(bridge_id)
    _last_write_seq.pop(bridge_id, None)
    print(f"[Bridge] Deleted from s&box: {bridge_id}")


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
    """Full sync from s&box. Reconcile Blender state."""
    objects = msg.get("objects", [])
    received_ids = set()

    for obj_data in objects:
        # Handle inline deletes from reconciliation
        if obj_data.get("type") == "deleted":
            bid = obj_data.get("bridgeId")
            if bid:
                existing = find_by_bridge_id(bid)
                if existing:
                    bpy.data.objects.remove(existing, do_unlink=True)
                _last_known_bridge_ids.discard(bid)
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
            _apply_sbox_transform(existing, obj_data)
            # Update name if changed in s&box
            name = obj_data.get("name")
            if name and existing.name != name:
                existing.name = name
            mesh_data = obj_data.get("meshData")
            if mesh_data and mesh_data.get("vertices"):
                _rebuild_mesh(existing, mesh_data)
        else:
            _create_from_sbox(obj_data)

    # Reconcile: remove stale objects that s&box no longer has
    stale = _last_known_bridge_ids - received_ids
    for bid in stale:
        obj = find_by_bridge_id(bid)
        if obj:
            _strip_bridge_props(obj)

    _last_known_bridge_ids = received_ids.copy()

    # Send create for Blender-only objects (immediate, not deferred)
    for obj in list(bpy.data.objects):
        if obj.get("sbox_scene_id") or obj.get("sbox_type"):
            continue
        if obj.type == "MESH" and not get_bridge_id(obj):
            send_create(obj)
        elif obj.type == "LIGHT" and not get_bridge_id(obj):
            if obj.data and obj.data.type not in UNSUPPORTED_LIGHT_TYPES:
                send_create_light(obj)

    print(f"[Bridge] Sync complete: {len(received_ids)} bridge objects from s&box")


# ── Object Creation from s&box ──────────────────────────────────────────

def _create_from_sbox(msg):
    """Create a Blender object from s&box data with actual mesh geometry."""
    bridge_id = msg.get("bridgeId")
    name = msg.get("name", "sbox Object")
    mesh_data = msg.get("meshData")

    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0

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

    col = get_or_create_sbox_collection()
    col.objects.link(obj)

    if bridge_id:
        set_bridge_id(obj, bridge_id)
        _last_known_bridge_ids.add(bridge_id)

    _apply_sbox_transform(obj, msg)
    print(f"[Bridge] Created from s&box: {name} ({bridge_id})")


def _rebuild_mesh(obj, mesh_data):
    """Replace an existing Blender object's mesh with new data from s&box."""
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

    old_mesh = obj.data
    obj.data = mesh
    if old_mesh and old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)


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
        _last_known_bridge_ids.add(bridge_id)
    if scene_id:
        obj["sbox_scene_id"] = scene_id
    obj["sbox_type"] = "light"

    _apply_sbox_transform(obj, msg)
    print(f"[Bridge] Light: {name} ({bridge_id or scene_id})")


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

        sx, sy, sz = obj.scale
        vertices = []
        for v in mesh.vertices:
            bx = v.co.x * sx * sf
            by = v.co.y * sy * sf
            bz = v.co.z * sz * sf
            cvt = blender_to_sbox_pos(bx, by, bz)
            vertices.extend(0.0 if (math.isnan(c) or math.isinf(c)) else c for c in cvt)

        faces = []
        face_materials = []
        for poly in mesh.polygons:
            faces.append(len(poly.vertices))
            for vi in poly.vertices:
                faces.append(vi)
            face_materials.append(poly.material_index)

        materials = _extract_materials(obj)
        eval_obj.to_mesh_clear()

        result = {"vertices": vertices, "faces": faces}
        if materials:
            result["materials"] = materials
            result["faceMaterials"] = face_materials
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
        if not abs_path or not os.path.isfile(abs_path):
            return None
        ext = os.path.splitext(abs_path)[1]
        dest_name = f"{safe_name}_{suffix}{ext}"
        dest_abs = os.path.join(bridge_dir, dest_name)
        try:
            shutil.copy2(abs_path, dest_abs)
            return f"materials/blender_bridge/{dest_name}"
        except Exception as e:
            print(f"[Bridge] Texture copy failed: {e}")
            return None

    color_ref = copy_tex(mat_data.get("baseColorTexture"), "color")
    rough_ref = copy_tex(mat_data.get("roughnessTexture"), "rough")
    metal_ref = copy_tex(mat_data.get("metallicTexture"), "metal")
    normal_ref = copy_tex(mat_data.get("normalTexture"), "normal")

    bc = mat_data.get("baseColor", [0.8, 0.8, 0.8, 1.0])
    r = bc[0] if len(bc) > 0 else 0.8
    g = bc[1] if len(bc) > 1 else 0.8
    b = bc[2] if len(bc) > 2 else 0.8

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

    em_str = mat_data.get("emissionStrength", 0.0)
    if em_str > 0.001:
        ec = mat_data.get("emissionColor", [0, 0, 0])
        er = ec[0]
        eg = ec[1] if len(ec) > 1 else 0
        eb = ec[2] if len(ec) > 2 else 0
        lines.append("")
        lines.append(f'\tg_vSelfIllumTint "[{er:.6f} {eg:.6f} {eb:.6f} 0.000000]"')
        lines.append(f'\tg_flSelfIllumScale "{em_str:.3f}"')

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
    return {
        "pitch": math.degrees(obj.rotation_euler.x),
        "yaw": math.degrees(obj.rotation_euler.z),
        "roll": math.degrees(obj.rotation_euler.y),
    }


def _apply_sbox_transform(obj, msg):
    """Apply s&box position/rotation to a Blender object."""
    sf = _get_scale_factor()
    inv_sf = 1.0 / sf if sf else 1.0

    if "position" in msg:
        p = msg["position"]
        bx, by, bz = sbox_to_blender_pos(
            p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0)
        )
        obj.location = (bx * inv_sf, by * inv_sf, bz * inv_sf)

    if "rotation" in msg:
        r = msg["rotation"]
        obj.rotation_euler = (
            math.radians(r.get("pitch", 0.0)),
            math.radians(r.get("roll", 0.0)),
            math.radians(r.get("yaw", 0.0)),
        )


# ── Depsgraph Handler ───────────────────────────────────────────────────

def on_depsgraph_update(scene, depsgraph):
    """Called after every depsgraph update. Routes changes to s&box."""
    if _suppress_depsgraph:
        return
    if not connection.is_connected():
        return
    if _play_mode_active:
        return

    try:
        settings = scene.sbox_bridge
        if not settings.auto_sync:
            return
    except Exception:
        return

    for update in depsgraph.updates:
        if not isinstance(update.id, bpy.types.Object):
            continue

        obj = update.id

        # Scene objects (models/lights from s&box) — position updates only
        if obj.get("sbox_scene_id") or obj.get("sbox_type"):
            if not update.is_updated_geometry:
                send_scene_transform(obj)
            continue

        # Unsupported types — skip silently
        if obj.type in UNSUPPORTED_TYPES:
            continue

        # Lights
        if obj.type == "LIGHT":
            if obj.data and obj.data.type in UNSUPPORTED_LIGHT_TYPES:
                add_warning(f"'{obj.name}': AREA lights not supported in s&box")
                continue
            bridge_id = get_bridge_id(obj)
            if bridge_id:
                send_update_light(obj)
            elif not obj.get("sbox_scene_id"):
                send_create_light(obj)
            continue

        # Convertible types (curves, surfaces, etc.)
        if obj.type in CONVERTIBLE_TYPES:
            if not get_bridge_id(obj):
                try:
                    send_create(obj)
                except Exception:
                    pass
            continue

        # Non-mesh — skip
        if obj.type != "MESH":
            continue

        # Mesh objects
        bridge_id = get_bridge_id(obj)

        if bridge_id:
            if update.is_updated_geometry or _scale_changed(obj, bridge_id):
                _schedule_mesh_update(bridge_id, obj)
            else:
                send_update_transform(obj)
        else:
            # New object — detect paste duplicate, then create directly (no timer)
            _detect_and_strip_paste_duplicate(obj)
            if not get_bridge_id(obj):
                send_create(obj)


# ── Mesh Update Debounce ────────────────────────────────────────────────

def _scale_changed(obj, bridge_id):
    """Check if the object's scale changed since last check."""
    current = tuple(round(s, 4) for s in obj.scale)
    prev = _last_scale.get(bridge_id)
    _last_scale[bridge_id] = current
    if prev is None:
        return False
    return current != prev


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
    bid = obj.get("sbox_bridge_id")
    if not bid:
        return
    for other in bpy.data.objects:
        if other != obj and other.get("sbox_bridge_id") == bid:
            if "sbox_bridge_id" in obj:
                del obj["sbox_bridge_id"]
            if "_remote_update_time" in obj:
                del obj["_remote_update_time"]
            return


# ── Timer: poll for messages + detect deletions ─────────────────────────

def _poll_and_process():
    """Blender timer callback: poll s&box and process messages."""
    global _last_sbox_seq_processed, _current_session_id

    if not connection.is_connected():
        try:
            if hasattr(bpy.context, "scene") and hasattr(bpy.context.scene, "sbox_bridge"):
                bpy.context.scene.sbox_bridge.is_connected = False
        except Exception:
            pass
        if connection.is_reconnecting():
            return 0.5  # Keep timer alive during reconnect
        return None  # Stop timer — disconnected

    try:
        response = connection.poll()
        if response is None:
            return 0.1

        # Detect session change (s&box restarted or hot-reloaded)
        session_id = response.get("sessionId")
        if session_id and session_id != _current_session_id:
            _current_session_id = session_id
            _last_write_seq.clear()
            _last_sbox_seq_processed = 0
            print(f"[Bridge] Session changed to {session_id}, resyncing...")
            send_sync()
            return 0.1

        for msg in response.get("messages", []):
            seq = msg.get("seq", 0)
            process_incoming(msg)
            if seq > _last_sbox_seq_processed:
                _last_sbox_seq_processed = seq

    except Exception as e:
        print(f"[Bridge] Poll error: {e}")

    _check_duplicates()
    _check_deletions()
    _confirm_pending_deletes()
    return 0.1


def _check_duplicates():
    """Clean up stale bridge properties and detect duplicate IDs."""
    if not connection.is_connected():
        return

    # Strip bridge IDs from non-syncable types
    for obj in list(bpy.data.objects):
        bid = obj.get("sbox_bridge_id")
        if bid and obj.type not in SYNCABLE_TYPES and obj.type not in CONVERTIBLE_TYPES:
            _strip_bridge_props(obj)
            continue

    # Detect duplicate IDs on remaining objects
    seen = {}
    for obj in list(bpy.data.objects):
        bid = obj.get("sbox_bridge_id")
        if not bid:
            continue
        if bid in seen:
            _strip_bridge_props(obj)
            if obj.type == "MESH":
                send_create(obj)
            elif obj.type == "LIGHT":
                send_create_light(obj)
        else:
            seen[bid] = obj.name


def _check_deletions():
    """Detect bridge objects deleted in Blender, add to pending deletes."""
    global _last_known_bridge_ids

    if not connection.is_connected():
        return

    current_ids = set()
    for obj in bpy.data.objects:
        bid = obj.get("sbox_bridge_id")
        if bid:
            current_ids.add(bid)

    deleted = _last_known_bridge_ids - current_ids
    for bid in deleted:
        # Don't add if already pending
        if not any(b == bid for b, _ in _pending_deletes):
            _pending_deletes.append((bid, time.time()))

    _last_known_bridge_ids = current_ids


def _confirm_pending_deletes():
    """Auto-confirm pending deletes after timeout."""
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
    bid = obj.get("sbox_bridge_id")
    for key in ["sbox_bridge_id", "_remote_update_time"]:
        if key in obj:
            del obj[key]
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
    if not _timer_running:
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
