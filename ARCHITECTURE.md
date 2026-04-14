# s&box Blender Bridge — Architecture & Developer Guide

Reference for contributors, AI agents, and anyone reading the codebase.

---

## How It Works

HTTP-based bidirectional sync between Blender (Python addon) and s&box (C# editor library). Blender polls s&box every 100ms for changes. s&box polls its own scene every 200ms for changes. Both sides use sequence-based echo suppression to prevent infinite loops.

```
Blender (Python)                    s&box (C#)
  panel.py  ←→  sync.py  ←HTTP→  BlenderBridgeServer.cs
                   ↕                       ↕
            connection.py          BlenderBridgeDispatcher.cs
                                   BridgePersistence.cs
                                   BridgeSceneHelper.cs
                                   BlenderBridgeWindow.cs
```

**Blender addon** lives in `sbox_bridge/` (4 Python files).
**s&box library** lives in `Editor/BlenderBridge/` (5 C# files, compiled by the s&box editor).

---

## File Map

### Blender Addon (`sbox_bridge/`)

| File | Role |
|---|---|
| `__init__.py` | Addon registration, `SboxBridgeSettings` property group, class list |
| `connection.py` | HTTP client — connect, disconnect, send, poll, auto-reconnect |
| `sync.py` | Core sync engine — all message handlers, depsgraph watcher, mesh extraction, material generation |
| `panel.py` | N-panel UI operators (Send to Scene, Force Resync, etc.) and the panel draw function |
| `blender_manifest.toml` | Extension metadata for Blender's extension repository |

### s&box Library (`Editor/BlenderBridge/`)

| File | Role |
|---|---|
| `BlenderBridgeServer.cs` | HTTP listener on port 8099, message queue, session management |
| `BlenderBridgeDispatcher.cs` | All message handlers, scene polling, mesh extraction/application, payload builders |
| `BridgePersistence.cs` | Binary mesh cache + JSON manifest for surviving play mode and hot reload |
| `BridgeSceneHelper.cs` | Scene resolution and tree walking utilities |
| `BlenderBridgeWindow.cs` | Editor UI panel (status, sync buttons, materials list, activity log) |

### Extension Index (`docs/`)

| File | Role |
|---|---|
| `index.json` | Blender extension repository index — version, archive URL, hash |
| `sbox_bridge-X.Y.Z.zip` | Packaged addon for auto-install via Blender preferences |

---

## Protocol

All messages are JSON over HTTP. Blender sends via `POST /message`, receives via `GET /poll`.

### Coordinate System

```
Blender: X-right, Y-forward, Z-up
s&box:   X-forward, Y-left, Z-up

Blender → s&box: (bY, -bX, bZ)
s&box → Blender: (-sY, sX, sZ)
```

### Echo Suppression

Every outgoing message includes `seq` (sender's counter) and `ack` (last received seq from the other side). When a side sends a change, it records `_last_write_seq[bridgeId] = seq`. When the echo bounces back, it gets dropped.

Blender also uses time-based suppression (`_remote_update_times`) — after receiving a transform from s&box, Blender suppresses sending that object's transform back for 200ms to prevent depsgraph echo loops.

### Message Types

#### Blender → s&box

| Type | Purpose | Key Fields |
|---|---|---|
| `create` | New mesh object | `name`, `meshData`, `idempotencyKey`, `hierarchy` |
| `update_transform` | Position/rotation only (20Hz) | `bridgeId`, `position`, `rotation` |
| `update_mesh` | Full geometry update | `bridgeId`, `meshData`, `geometryHash` |
| `mesh_begin` | Start chunked transfer (>20k verts) | `bridgeId`, `totalVertices`, `chunkCount` |
| `mesh_chunk` | Vertex chunk | `bridgeId`, `chunkIndex`, `vertices` |
| `mesh_end` | Finalize chunked transfer | `bridgeId`, `faces`, `materials` |
| `delete` | Remove object | `bridgeId` |
| `sync` | Request full scene state | `knownObjects` |
| `create_light` | New light | `name`, `lightType`, `properties` |
| `update_light` | Light properties + transform | `bridgeId`, `properties` |
| `update_scene_transform` | Move s&box-native object | `sceneId`, `position`, `rotation` |

#### s&box → Blender

| Type | Purpose | Key Fields |
|---|---|---|
| `updated` | Object moved in s&box | `bridgeId`, `position`, `rotation` |
| `mesh_updated` | Geometry edited in s&box | `bridgeId`, `meshData` |
| `object_created` | Native MeshComponent auto-adopted | `bridgeId`, `name`, `meshData` |
| `deleted` | Object removed in s&box | `bridgeId` |
| `sync_response` | Full scene state | `objects[]` (bridge objects, lights, models) |
| `scene_updated` | Scene object moved | `sceneId`, `position`, `rotation` |
| `light_updated` | Light changed | `sceneId`, `properties` |
| `play_mode` | Play mode toggled | `state` ("started"/"stopped") |

### Mesh Data Format

```json
{
  "vertices": [x0, y0, z0, x1, y1, z1, ...],
  "faces": [vertCount, idx0, idx1, idx2, vertCount, idx0, ...],
  "faceMaterials": [matIdx0, matIdx1, ...],
  "materials": [{ "name": "...", "baseColor": [r,g,b,a], ... }]
}
```

Vertices are flat float arrays in the sender's coordinate system (converted on receive). Faces use a variable-length encoding: vertex count followed by that many indices.

---

## Object Tracking

### Custom Properties on Blender Objects

| Property | Type | Meaning |
|---|---|---|
| `sbox_bridge_id` | string | Bridge ID assigned by s&box (e.g. `b_a1b2c3d4`) |
| `sbox_bridge_name` | string | Original name at creation |
| `sbox_bridge_hash` | string | Last-sent geometry hash (12-char hex) |
| `sbox_bridge_status` | string | `unsent`, `synced`, `modified`, `received` |
| `sbox_bridge_last_sync` | float | `time.time()` of last successful sync |
| `sbox_scene_id` | string | For s&box-native objects (models/lights) |
| `sbox_type` | string | `model` or `light` (s&box-native objects only) |
| `sbox_model_path` | string | Model resource path (s&box-native models only) |

### Tags on s&box GameObjects

| Tag | Meaning |
|---|---|
| `bridge_{bridgeId}` | This object is tracked by the bridge |
| `bridge_group` | The "Blender Bridge" parent container |

---

## Key Flows

### Creating an object from Blender

1. Blender extracts mesh (world-space vertices minus origin, coordinate-converted)
2. Sends `create` with `idempotencyKey` to prevent duplicates on retry
3. s&box creates GameObject + MeshComponent, assigns bridgeId, adds tag
4. Returns `{"bridgeId": "b_..."}` — Blender stores as custom property

### Auto-adopting native s&box objects

When s&box's poll loop finds a MeshComponent without a `bridge_*` tag:
1. Generates a bridgeId, tags the object
2. Extracts mesh data
3. Broadcasts `object_created` to Blender
4. Blender's `_handle_object_created` creates the mesh in Blender

This also happens during `HandleSync` for first-connect reconciliation.

### Hiding objects in Blender

`_check_hidden()` runs every poll cycle (100ms):
- If a bridge object becomes hidden → sends `delete` to s&box
- If it becomes visible again → strips bridge props and re-creates it

### Deleting objects

5-second grace period via `_pending_deletes`. Auto-confirms after timeout. User can cancel from the panel.

### Session recovery

When s&box restarts (new `sessionId` detected in poll response):
1. Blender clears all echo suppression state
2. Sends `sync` with list of known objects
3. s&box responds with full scene state
4. Both sides reconcile (create missing, remove stale)

---

## Materials

Blender extracts PBR values from Principled BSDF nodes and generates `.vmat` files in `Assets/materials/blender_bridge/`. Textures are copied alongside. Material content is hashed (including texture file mtimes) to avoid redundant writes.

---

## Persistence

`BridgePersistence.cs` saves mesh geometry to binary cache files (`.meshcache`) and a JSON manifest (`.bridge.json`). This survives:
- s&box play mode (meshes restored on exit)
- s&box hot reload
- Editor restart (meshes rebuilt from cache on server start)

---

## Rate Limiting & Debouncing

| What | Limit | Where |
|---|---|---|
| Transform sends | 50ms per object (20Hz) | `sync.py` `TRANSFORM_SEND_INTERVAL` |
| Mesh update sends | 150ms debounce | `sync.py` `MESH_DEBOUNCE_INTERVAL` |
| Blender poll interval | 100ms (10Hz) | `sync.py` `_poll_and_process` |
| s&box poll interval | 200ms (5Hz) | `BlenderBridgeServer.cs` PollLoop |
| Delete confirmation | 5s grace period | `sync.py` `PENDING_DELETE_TIMEOUT` |
| Chunked mesh chunks | 10ms between chunks | `sync.py` `_send_chunked_mesh` |
| Chunk size | 20,000 vertices | `sync.py` `CHUNK_VERTEX_LIMIT` |

---

## Settings Reference

### Blender Addon (N-panel)

| Setting | Default | Description |
|---|---|---|
| Host | `localhost` | s&box server hostname |
| Port | `8099` | s&box server port |
| Auto Sync | On | Auto-send changes via depsgraph |
| Scale Factor | `1.0` | Blender-to-s&box unit multiplier |
| Assets Path | — | Path to s&box `Assets/` folder (for materials) |
| Auto Reconnect | On | Reconnect on connection loss (max 5 attempts) |
| Sync Mode | Bidirectional | `BIDIRECTIONAL` / `EXPORT_ONLY` / `MANUAL` |

### s&box Editor (ConVars)

| ConVar | Default | Description |
|---|---|---|
| `bridge_port` | `8099` | HTTP server port |
| `bridge_autostart` | `true` | Start bridge on editor load |

---

## Releasing a New Version

1. Bump version in `sbox_bridge/blender_manifest.toml` and `sbox_bridge/__init__.py` (`bl_info["version"]`)
2. Build zip: `python -c "import zipfile, os; ..."` (exclude `__pycache__`)
3. Compute hash: `sha256` of the zip file
4. Update `docs/index.json` with new version, `archive_url`, `archive_size`, `archive_hash`
5. Commit zip + index + source, push to `main`
6. GitHub Pages serves the updated index — Blender auto-updates

---

## Common Pitfalls

- **Depsgraph echo**: Setting `obj.location` in a timer triggers a depsgraph update *after* the timer returns. Must suppress via `_remote_update_times` (200ms window), not just `_suppress_depsgraph` (only blocks during synchronous processing).
- **Mesh hash collisions**: Old hash was `vertCount * 1000 + faceCount` — moving vertices without changing topology was invisible. Now uses `ComputeMeshGeometryHash` which includes vertex positions.
- **Anonymous object serialization (C#)**: Can't add fields to anonymous objects after creation. Use dedicated builder methods (e.g. `BuildObjectCreatedMessage`) for messages that need a `type` field.
- **Blender pycache**: After editing addon files, delete `__pycache__/` or Blender loads stale bytecode.
- **Two addon locations**: Blender can load from `scripts/addons/` (legacy) and `extensions/blender4_com/` (extension repo). If both are active, conflicts occur. Disable one.
