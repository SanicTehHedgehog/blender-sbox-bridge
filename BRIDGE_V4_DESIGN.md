# Blender-s&box Bridge v4 Design Document

> **Status**: Design phase (no code changes yet)  
> **Last updated**: 2026-04-15  
> **Authors**: AI-assisted design with multi-agent review  
> **Predecessor**: v3.2.0 (current production)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problems With v3](#2-problems-with-v3)
3. [Architecture Overview](#3-architecture-overview)
4. [Ownership Model (Two-Tier Hybrid)](#4-ownership-model-two-tier-hybrid)
5. [VMDL Proxy System](#5-vmdl-proxy-system)
6. [Material Translation Pipeline](#6-material-translation-pipeline)
7. [Identity & Desync Prevention](#7-identity--desync-prevention)
8. [Transport Layer](#8-transport-layer)
9. [UX & UI Design](#9-ux--ui-design)
10. [Safeguards & Corruption Prevention](#10-safeguards--corruption-prevention)
11. [s&box Engine API Reference](#11-sbox-engine-api-reference)
12. [Anvil & SourceIO Integration](#12-anvil--sourceio-integration)
13. [Migration Guide (v3 to v4)](#13-migration-guide-v3-to-v4)
14. [Known Limitations & Trade-offs](#14-known-limitations--trade-offs)
15. [Open Questions](#15-open-questions)

---

## 1. Executive Summary

Bridge v4 is a ground-up redesign of the sync architecture, addressing three critical failures in v3:

1. **Desync and data loss** (Grillus complaint: "sync grabbed engine version and wiped my progress")
2. **No ownership semantics** (no way to control what syncs, when, or in which direction)
3. **Performance bottlenecks** (O(n) lookups, full geometry hashing every frame, HTTP polling overhead)

### Core Design Principles

- **Never lose user work.** Every sync operation shows a preview before executing.
- **Explicit over implicit.** No automatic overwrites. User confirms direction of data flow.
- **Objects have different needs.** Mesh geometry needs strict ownership. Transforms can flow freely.
- **vmdls are sacred.** Read-only proxies only. Never modify original game assets from Blender.

---

## 2. Problems With v3

### 2.1 Architectural Issues

| Issue | Location | Impact |
|-------|----------|--------|
| O(n) bridge ID lookup | `sync.py:98-102` `find_by_bridge_id()` | Linear scan of all objects per incoming message |
| Full geometry hash every depsgraph update | `sync.py:133-154` `geometry_hash()` | MD5 of all vertices on every change, requires full depsgraph eval |
| One-shot echo suppression | `sync.py:711-715` | Deletes `_last_write_seq` on first echo; second rapid update applies as false positive |
| HTTP polling (100ms) | `connection.py` + `BlenderBridgeServer.cs` | Latency floor, no push capability, wasted cycles |
| God module | `sync.py` (1877 lines) | 15+ module-level globals, untestable, state leaks between operations |
| TextureAlignToGrid destroys Blender UVs | `BlenderBridgeDispatcher.cs:~977` | All artist UV work lost on mesh receive |

### 2.2 User Complaints (Grillus Feedback)

> "I don't see practicality of bridge being real time... opens up possibilities for sync issues."

> "Need to be able to choose what is being exported and imported."

> "I wanted to re-sync mesh so it would appear in engine, when sync grabbed engine version and brought it into blender, wiping my progress."

> "Workflow would be: block out in s&box, connect blender, select objects to send via widget, keep track visually which sent/received."

### 2.3 Missing Features

- No vmdl support (only PolygonMesh)
- No ownership control per object
- No material preview in Blender
- No sync plan preview (fire-and-forget)
- No batch operations on collections
- No pause/resume toggle
- No UV preservation on mesh sync

---

## 3. Architecture Overview

### 3.1 Module Split (replacing sync.py god module)

```
sbox_bridge/
  __init__.py          # Registration, settings PropertyGroup
  connection.py        # WebSocket client (replaces HTTP)
  panel.py             # UI panels, operators
  
  core/
    identity.py        # Stable ID generation, triple-storage, recovery
    ownership.py       # Two-tier ownership state machine
    journal.py         # Transform/material edit journaling
    
  sync/
    mesh.py            # Geometry extraction, hashing, dirty-bit tracking
    transforms.py      # Position/rotation/scale sync with echo suppression
    materials.py       # Material extraction, baking, .vmat generation
    lights.py          # Light property sync
    
  vmdl/
    proxy.py           # VMDL proxy import, watchdog, edit prevention
    cache.py           # Session cache management, cleanup
    source_io.py       # Source IO integration wrapper
    
  ui/
    sync_plan.py       # Pre-flight sync preview dialog
    material_preview.py # Material thumbnail generation
    status_overlay.py  # Viewport overlay colors for sync state
```

### 3.2 Data Flow

```
Blender (Python)                          s&box (C#)
┌────────────────┐                        ┌────────────────┐
│ Depsgraph      │                        │ Scene Poll     │
│ Handler        │                        │ Loop (200ms)   │
│ (change detect)│                        │                │
└──────┬─────────┘                        └──────┬─────────┘
       │ dirty bit                               │ dirty bit
       v                                         v
┌────────────────┐    WebSocket (binary)  ┌────────────────┐
│ Sync Engine    │◄══════════════════════►│ Bridge Server  │
│ (per-module)   │    framed messages     │ (dispatcher)   │
└──────┬─────────┘                        └──────┬─────────┘
       │                                         │
       v                                         v
┌────────────────┐                        ┌────────────────┐
│ Ownership      │                        │ Ownership      │
│ State Machine  │                        │ State Machine  │
│ (per object)   │                        │ (per object)   │
└────────────────┘                        └────────────────┘
```

### 3.3 Complete Object Creation Flow ("Drop a Mesh")

End-to-end flow for creating an object in Blender and syncing it to s&box:

```
1. User adds object in Blender (Add → Mesh → Cube, or import, or duplicate)
2. Depsgraph fires on_depsgraph_update
3. Unified handler detects new object (no sbox_bridge_stable property)
4. Generate stable_id (sha256 of scene_path + data_name + timestamp)
5. Write stable_id to triple-storage (custom prop, mesh data, scene registry)
6. Write stable_id to external WAL registry
7. Set ownership based on session authority_default:
   - BLENDER: shape_owner=BLENDER, channels=BLENDER
   - SBOX: shape_owner=SBOX, channels=SBOX  
   - UNLOCKED: shape_owner=UNLOCKED, channels=BIDIRECTIONAL
8. IF connected:
   a. Object appears in "pending send" state (yellow outline)
   b. Automatic behavior depends on sync mode:
      - Manual mode: object waits for user to click "Send Selected"
      - Auto mode: 500ms debounce, then auto-send (geometry + transform + material)
   c. Send 'create' message with: stable_id, name, ownership, geometry, transform
   d. Server creates GameObject:
      - Scene target: ModelBuilder mesh → ModelRenderer component
      - Hammer target: PolygonMesh → MeshComponent
      - Always: BoxCollider auto-generated from mesh bounds (user can delete)
   e. Server responds with create_ack: { stable_id, sbox_object_id }
   f. Object transitions to "synced" state (green outline)
9. IF disconnected:
   a. Object added to _outbound_queue
   b. Object shows "QUEUED" badge (yellow with clock icon)
   c. On reconnect: queue presented to user for review before sending
```

---

## 4. Ownership Model (Two-Tier Hybrid)

### 4.1 Why Not Simple Object-Level Ownership

The original proposal (BLENDER/SBOX/UNLOCKED/LOCKED per object) was attacked and found to have three critical failures:

1. **Iterative refinement impossible**: Once Blender owns geometry, the engineer can't reposition in s&box without a full ownership transfer that risks losing mesh work.
2. **vmdl scope confusion**: Ownership for compiled models vs source geometry is fundamentally different.
3. **Collection sends hide mismatches**: Mixed ownership in hierarchies causes silent data loss.

### 4.2 Why Not Field-Level Ownership

The counter-proposal (per-field ownership with time-window rejection) was also attacked:

1. **Combinatorial explosion**: 4 fields x 4 states = 256 configurations per object. Unmanageable UI.
2. **Time windows are indefensible**: Any N minutes is wrong. Lunch break = ownership expires.
3. **Geometry merge is a fantasy**: Meshes aren't line-addressable. "Three-way merge" for vertices doesn't exist.
4. **Cross-field dependencies**: Geometry edits can dirty transforms (origin shift), rigging (weight invalidation), materials (UV changes).

### 4.3 The Hybrid: Two Tiers

```
Tier 1 — SHAPE (object-level, governs geometry + rigging):
    States: BLENDER | SBOX | UNLOCKED
    Semantics: "Who owns the SHAPE of this thing?"
    Rule: Non-mergeable. One side writes, other side reads.
    
Tier 2 — CHANNELS (per-channel, governs transforms + materials):
    States: BLENDER | SBOX | BIDIRECTIONAL
    Semantics: "Who can adjust placement and look?"
    Rule: Value-typed, reconcilable. Can flow both ways.
```

### 4.4 State Transitions

```python
class ObjectOwnership:
    shape_owner: Literal["BLENDER", "SBOX", "UNLOCKED"]
    transform_channel: Literal["BLENDER", "SBOX", "BIDIRECTIONAL"]
    material_channel: Literal["BLENDER", "SBOX", "BIDIRECTIONAL"]
    transform_journal: list[JournalEntry]
    material_journal: list[JournalEntry]

def on_sync(obj, incoming):
    # --- Tier 1: geometry ---
    if incoming.has_geometry:
        if obj.shape_owner == "UNLOCKED":
            # ATOMIC CLAIM: server-side lock prevents race
            # If two clients send geometry for same UNLOCKED object,
            # first message processed wins. Second gets REJECT.
            claimed = server.try_claim(obj.stable_id, incoming.source)
            if claimed:
                apply_geometry(incoming)
                obj.shape_owner = incoming.source
            else:
                REJECT(f"geometry just claimed by {obj.shape_owner}")
        elif obj.shape_owner == incoming.source:
            apply_geometry(incoming)
        else:
            REJECT(f"geometry owned by {obj.shape_owner}")
    
    # --- Tier 2: transforms ---
    if incoming.has_transforms:
        if obj.transform_channel in (incoming.source, "BIDIRECTIONAL"):
            if obj.transform_channel == "BIDIRECTIONAL":
                # Last-write-wins with server timestamp
                # Server stamps each transform with monotonic clock
                # Clients receive authoritative position from server
                apply_transforms(incoming)
                obj.transform_journal.append(JournalEntry(
                    source=incoming.source,
                    timestamp=server.monotonic_time(),
                    value=incoming.transforms
                ))
            else:
                apply_transforms(incoming)
        else:
            REJECT(f"transforms locked to {obj.transform_channel}")
```

### 4.4b Server-Side Ownership Arbitration

```csharp
// Server-side atomic claim for UNLOCKED objects
private readonly ConcurrentDictionary<string, string> _pendingClaims = new();

bool TryClaim(string stableId, string claimer) {
    // AddOrUpdate is atomic on ConcurrentDictionary
    // First caller wins; second caller sees existing value
    var existing = _pendingClaims.GetOrAdd(stableId, claimer);
    return existing == claimer;
}
```

For BIDIRECTIONAL transform conflicts (two users move same object):
- Server uses **last-write-wins with server monotonic timestamp**
- Both transforms are applied in arrival order
- Server broadcasts the final position to all clients
- No user prompt needed — transforms are value-typed and trivially reconcilable
- Journal preserves history for debugging

For geometry conflicts on UNLOCKED:
- First `mesh_update` to arrive claims ownership atomically
- Second sender receives: `{"type": "claim_rejected", "stable_id": "xxx", "claimed_by": "artist_a@ws1"}`
- Second sender's client shows: "Pillar_01 was just claimed by Artist A. Your edit was not applied."

### 4.5 Workflow Examples

**Artist + Engineer Pair (most common):**

| Step | Action | Shape Owner | Transform Channel |
|------|--------|------------|-------------------|
| 1 | Artist models pillar | BLENDER | BLENDER |
| 2 | Artist sends to s&box | BLENDER | BIDIRECTIONAL |
| 3 | Engineer repositions | BLENDER | BIDIRECTIONAL (journal: SBOX) |
| 4 | Sync back to Blender | Artist mesh untouched, position updated |
| 5 | Artist refines mesh | Sends geometry (allowed: shape_owner=BLENDER) |
| 6 | Engineer repositions again | Transform journal appends |
| 7 | Repeat until done | Both sides productive simultaneously |

**Solo Artist:**
- Everything: shape=BLENDER, channels=BLENDER
- Simplest mode: Blender is the single source of truth

**Level Design with VMDLs:**
- VMDLs don't participate in ownership (separate class, see Section 5)
- transform_channel=BIDIRECTIONAL for placement
- Geometry: NEVER writable from Blender

### 4.6 Collection Send Pre-flight

When sending a Blender collection to s&box:

```python
def send_collection(collection):
    report = []
    blocked = []
    
    for obj in collection.all_objects:
        if obj.get("sbox_is_proxy"):
            report.append(f"SKIP {obj.name}: VMDL proxy (read-only)")
            continue
        
        can_send_geo = (obj.shape_owner in ("BLENDER", "UNLOCKED"))
        can_send_tx = (obj.transform_channel in ("BLENDER", "BIDIRECTIONAL"))
        
        if not can_send_geo and has_geo_changes(obj):
            blocked.append(f"BLOCKED {obj.name}: geometry owned by SBOX")
        else:
            report.append(f"SEND {obj.name}: {fields}")
    
    if blocked:
        show_confirmation_dialog(
            title="Some objects cannot be fully sent",
            details=report + blocked,
            actions=["Send what's allowed", "Cancel"]
        )
```

### 4.7 UI: One Icon Per Object

Artists see ONE icon, not a matrix:

| Shape Owner | Icon Color | Meaning |
|-------------|-----------|---------|
| BLENDER | Blue | "I own this" |
| SBOX | Orange | "Engine owns this" |
| UNLOCKED | Green | "Free — first editor claims it" |
| VMDL Proxy | Gray (cyan wireframe) | "Reference only, can't edit geometry" |

A small badge dot appears if transform/material channels differ from shape owner. Details on hover/click only.

### 4.8 Object Deletion Lifecycle

**Blender deletes a BLENDER-owned object:**
1. Depsgraph handler detects object missing from scene
2. Send `delete` message with stable_id
3. s&box removes the GameObject
4. Remove from external WAL registry
5. If disconnected: queue the delete, apply on reconnect

**Blender deletes an SBOX-owned object:**
1. BLOCKED. Show warning: "This object is owned by s&box. Delete in s&box first, or transfer ownership."
2. User can force-delete with Shift+Delete: sends delete, s&box removes it, logs warning

**s&box deletes an object:**
1. Server sends `delete` message to all connected clients
2. Blender receives delete:
   - If SBOX-owned: remove immediately, no prompt
   - If BLENDER-owned: show warning: "s&box deleted Pillar_01 (which you own). [Accept] [Undo in s&box]"
   - If UNLOCKED: remove immediately
3. Remove from bridge ID cache and external registry

**Delete during disconnection:**
- Blender deletes while disconnected → queued, applied on reconnect after user review
- s&box deletes while disconnected → on reconnect hello_ack includes `{"stable_id": "xxx", "deleted": true}`, Blender shows "3 objects were deleted in s&box while you were disconnected. [Accept All] [Review]"

**Undo of delete:**
- User deletes object → Ctrl+Z restores it
- External registry still has the stable_id (delete removed it, undo restores it via undo_post handler)
- If the delete was already sent to s&box: re-send a `create` message to recreate in s&box
- If disconnected when undo happens: the queued delete is removed from queue

### 4.9 Hierarchy & Parenting

**Decision: Hierarchy sync is SUPPORTED for transforms, NOT for gameplay logic.**

When Blender parents object A to object B:
1. The bridge detects the parent change via depsgraph
2. Sends a `reparent` message: `{"stable_id": "A", "parent_id": "B"}` (or `null` for unparent)
3. s&box calls `goA.SetParent(goB)`
4. Since transforms use `matrix_world`, visual positions are preserved

**Limitations (explicitly stated):**
- Blender's parent types (Object, Bone, Vertex) map only to basic GameObject parenting in s&box
- Blender constraints (Copy Location, Track To, etc.) do NOT sync — they are Blender-side only
- If a developer relies on specific parent-child relationships for gameplay Components, they must set those up in s&box directly. The bridge syncs the hierarchy structure, not the semantic intent.

---

## 5. VMDL Proxy System

### 5.1 Core Constraint

**vmdls are READ-ONLY visual proxies.** We import a COPY, never the original. Geometry NEVER flows from Blender back to s&box for vmdl objects. Transform sync (position, rotation, scale) IS bidirectional.

This is the #1 safety rule. The other user's expectation of editing vmdl mesh from the addon is exactly the dangerous assumption we guard against.

### 5.2 Import Pipeline

```python
def import_vmdl_proxy(original_path, session):
    # PHASE 1: Copy (our code, no Source IO)
    copy_path = create_verified_copy(original_path, session)
    # original_path is NEVER passed beyond this point
    
    # PHASE 2: Import via SourceIO (reads from copy_path only)
    # Exact API: bpy.ops.sourceio.vmdl(filepath=str(copy_path))
    # Expects .vmdl_c (compiled), scale=0.01905 (Hammer units → meters)
    proxy = source_io_import(copy_path)
    
    # Tag immediately
    proxy["sbox_vmdl_source"] = str(original_path)
    proxy["sbox_is_proxy"] = True
    proxy["sbox_readonly"] = True
    return proxy
```

### 5.3 Six-Layer Safety System

#### Layer 1: Import Safety
- Original file set read-only during copy (tripwire)
- Atomic copy: write to `.tmp`, verify SHA-256 checksum, then `os.replace`
- Source IO content root overridden to cache directory
- Post-import hash verification on original (detect if anything wrote to it)
- Each session gets its own cache subdirectory (UUID4, no collisions)

#### Layer 2: Edit Prevention
- **Barrier A — Mesh hash watchdog**: Every 2s, hash vertex positions + face indices. If changed, auto-revert from backup.
- **Barrier B — Depsgraph handler**: Detects Edit Mode entry on proxy objects, immediately schedules `force_exit_edit_mode()` on next tick.
- **Barrier C — Visual indicator**: Proxy name prefixed with `[PROXY]`, cyan wireframe color.
- **Modifier guard**: On import, record modifier stack. Any unauthorized modifier added by user is auto-removed within 2s.

#### Layer 3: Transform Sync Safety
- Sequence numbering with `echo_of_seq` field prevents oscillation
- Epsilon tolerance (1e-4) for "same transform" comparison
- Oscillation detector: >6 round-trips/second triggers 500ms pause + authoritative state push
- Scale constraint enforcement (uniform-only for physics bodies)
- Always use `matrix_world`, never `matrix_local` (parenting-safe)

#### Layer 4: Session & Cache Safety
- Session ID: UUID4 (not hash of timestamp)
- Disk space pre-check: require 500MB free before copy
- Cache missing detection: watchdog verifies copy file exists, auto-recreates from original
- Antivirus handling: catch `PermissionError`, show actionable guidance
- Reference-counted retention: 30 days (not 7), never auto-delete without confirmation, prompted when cache >2GB

#### Layer 5: Connection Safety

| Proxy State | Wireframe | Overlay | Transform Sync |
|-------------|----------|---------|----------------|
| Connected | Cyan | (none) | Active |
| Disconnected | Dark gray | "DISCONNECTED" | Queued |
| Stale | Red | "STALE" | Disabled |

- On reconnect: s&box is authority for vmdl state. If both sides moved while disconnected, s&box wins.
- If vmdl deleted in s&box while disconnected: proxy enters STALE (stays visible, sync disabled).

#### Layer 6: Corruption Prevention
- Manifest: atomic writes (temp + rename), `.json.bak` backup, schema validation
- If both manifest and backup corrupt: rebuild from `.meta` sidecar files in cache
- Custom property recovery: if `sbox_is_proxy` etc. missing, restore from manifest
- On-disk checksum verification: on import, on reconnect, on user request (not every tick)

### 5.4 Failure Mode Table

| Failure | Detection | Recovery | Data Loss |
|---------|-----------|----------|-----------|
| Original file modified | Post-import checksum | Alert user | None |
| Copy corrupted | Hash mismatch | Re-copy from original | None |
| Mesh edited by user | Hash watchdog (2s) | Auto-revert | None |
| Edit Mode entered | Depsgraph handler | Force exit + warning | None |
| Transform oscillation | Message rate counter | Pause + authoritative sync | None |
| Cache deleted | File existence check | Re-copy from original | None |
| Manifest corrupted | JSON parse failure | Load backup or rebuild | None |
| Connection lost | Heartbeat timeout | Gray out proxies, queue | None |
| Disk full | Pre-check | Abort cleanly, report | None |
| AV quarantine | PermissionError | Report with instructions | None |

---

## 6. Material Translation Pipeline

### 6.1 Parameter Mapping (Corrected)

The initial proposal claimed "6 inputs map lossless." This is wrong. Here are the actual mappings with corrections:

| Blender Input | s&box Parameter | Conversion Required | Notes |
|---------------|-----------------|-------------------|-------|
| `base_color` (linear sRGB) | `g_vColorTint` (sRGB encoded) | **pow(1/2.2) gamma encode** | Without this, every color appears darker |
| `metallic` (0-1 absolute) | `g_flMetalness` | Direct (lossless) | |
| `roughness` (0-1 absolute) | `TextureRoughness` + `g_flRoughnessScaleFactor` | **Generate 1x1 constant PNG** at roughness value if no texture; set scale=1.0 | Scale param is a multiplier on the texture. 1x1 constant texture is the standard approach. |
| `normal` (OpenGL +Y up) | `TextureNormal` | **No conversion needed** (both use OpenGL +Y up) | Source 2 uses OpenGL normals, NOT DirectX. Confirmed via Valve Dev Community. |
| `emission` (linear color) | `g_vSelfIllumTint` | Gamma encode + `F_SELF_ILLUM 1` flag | |
| `IOR` (1.0-2.5) | **No direct param** | Compute F0: `((IOR-1)/(IOR+1))^2`, store as `g_flSpecularReflectance` | IOR=1.5 (default) = F0=0.04 (already assumed) |
| `alpha` | Depends on mode | See alpha table below | |
| `backface_culling` | `g_bDoubleSided` | **Inverted** (culling OFF = double-sided ON) | |
| `SSS weight` | `g_flSubsurfaceScale` | Select `vr_skin.vfx` shader | NOT dropped — approximated |
| `SSS radius` | `g_vSubsurfaceColor` | Normalize radius vector | |
| Vertex colors | `VertexAttribute.Color` | Pass through as mesh data | NOT dropped |

### 6.2 Alpha Mode Mapping

| Blender Mode | s&box Parameters | Notes |
|-------------|-----------------|-------|
| `ALPHA_BLEND` | `F_TRANSLUCENT 1`, `g_nRenderMode 1` | Order-dependent transparency |
| `ALPHA_CLIP` | `F_ALPHA_TEST 1`, `g_flAlphaTestReference {threshold}` | Binary cutout |
| `ALPHA_HASHED` | `F_ALPHA_TEST 1` | **Approximated** as clip with warning |

### 6.3 Shader Selection (Real Source 2 Names)

The initial proposal used fictional shader names. Real mapping:

| Condition (evaluated in order) | Shader | Feature Flags |
|-------------------------------|--------|---------------|
| SSS weight > 0 | **`skin.shader`** | SSS always-on: `g_flCurvatureScale`, `g_vTransmissionColor`, `g_flTransmissionFalloff` |
| Emission > 0.01 | `complex.vfx` | `F_SELF_ILLUM 1` |
| Metallic > 0.1 (with texture) | `complex.vfx` | `F_METALNESS_TEXTURE 1`, `F_SPECULAR 1` |
| Metallic > 0.1 (uniform) | `complex.vfx` | `F_SPECULAR 1` (use `g_flMetalness`) |
| Has alpha | `complex.vfx` | `F_TRANSLUCENT 1` or `F_ALPHA_TEST 1` |
| Default | `complex.vfx` | `F_SPECULAR 1` |

**CONFIRMED (2026-04-15)**: s&box uses `complex.vfx` NOT `vr_complex.vfx`. The `vr_` prefix was Half-Life Alyx specific. s&box shader names have diverged from Source 2 base. The `F_` feature flag system is confirmed working. Note: `F_SPECULAR 1` AND `F_METALNESS_TEXTURE 1` must BOTH be enabled for metalness textures to work.

**SSS (CONFIRMED)**: s&box uses a dedicated `skin.shader` for SSS. SSS is always-on in this shader (no feature flag). Key params: `g_flCurvatureScale`, `g_vTransmissionColor`, `g_flTransmissionFalloff`, `TextureTransmissiveMask`, `g_vAmbientNormalSoftness` (per-channel normal blur). Map Blender SSS weight > 0 → switch to `skin.shader`.

The shader mapping should live in a **separate JSON registry file** that can be updated without code changes:

```json
{
  "version": 2,
  "engine": "sbox",
  "note": "s&box uses complex.vfx, NOT vr_complex.vfx (HLA naming diverged)",
  "shaders": {
    "default": { "name": "complex.vfx", "flags": { "F_SPECULAR": 1 } },
    "metallic_textured": { "name": "complex.vfx", "flags": { "F_SPECULAR": 1, "F_METALNESS_TEXTURE": 1 } },
    "emissive": { "name": "complex.vfx", "flags": { "F_SPECULAR": 1, "F_SELF_ILLUM": 1 } },
    "translucent": { "name": "complex.vfx", "flags": { "F_TRANSLUCENT": 1 } },
    "alpha_test": { "name": "complex.vfx", "flags": { "F_ALPHA_TEST": 1 } },
    "skin_sss": { "name": "skin.shader", "flags": {},
      "params": { "g_flCurvatureScale": 1.0, "g_flTransmissionFalloff": 2.0 }
    }
  }
}
```

### 6.4 Three-Tier Texture Strategy

| Tier | Detection | Action | Performance |
|------|-----------|--------|-------------|
| **1 (Direct)** | Image Texture node → output | Export image as-is, set vtex `srgb` flag appropriately | Instant |
| **2 (Simple Bake)** | Image + ColorRamp or Mapping node | Bake result, store blend/UV metadata in sidecar | ~1-3s per texture |
| **3 (Procedural Bake)** | Complex node tree, no image source | Bake at configurable resolution (default 2K) with quality warning | ~5-15s per texture |

**Baking workflow:**
- **Never bake on every material change** (destroys real-time editing)
- Generate 256x256 preview proxy on change (sub-second on GPU)
- Full-resolution baking only on export
- Batch bake with progress bar, cancellation support
- UV validation pre-flight: check UVs exist, check for overlap, check UV channel reference

**Resolution control:**
- User sets default in addon preferences (512/1024/2048/4096)
- Per-material override available
- Warning if >2048 (vtex practical limits)
- Warning on non-power-of-2 textures

### 6.4b Bake Trigger Specification

**When does a full bake run?**
1. User clicks "Export Materials" button — bakes ALL materials that need it
2. User clicks "Send Selected" on objects with Tier 2/3 materials — bakes those materials before sending
3. User clicks "Dry Run" — estimates bake time but does NOT bake

**What happens if bake is stale?**
- `blender_material_hash` in sidecar tracks the Blender node tree state (hash of all node connections, values, and image paths). This is SEPARATE from `vmat_content_hash` (which tracks the exported .vmat).
- On Send: if `blender_material_hash` differs from last bake, warn: "Material 'Wood_Floor' changed since last bake. Re-bake before sending? [Bake Now] [Send Stale] [Cancel]"

**Zero-area faces and degenerate geometry:**
- Pre-bake validation checks: UV existence, UV overlap, zero-area faces (`poly.area < 1e-8`), degenerate triangles
- On failure: "Cannot bake 'Wood_Floor' on 'Floor_01': 3 zero-area faces detected. Fix geometry before baking."

**World-space UV Lock + baking incompatibility:**
- **These are fundamentally incompatible.** Anvil-style world-space UV Lock generates UVs from world position at runtime (procedural). Baking requires static UVs stored per-vertex.
- **Resolution:** When a Tier 2/3 material needs baking, the bridge auto-generates a bake-specific UV layer from the current world-space projection, bakes to that layer, then exports both the baked texture and the UV layer. The world-space UV Lock continues to work in Blender's viewport, but the exported version uses the frozen snapshot.
- The sidecar records: `uv_source = "world_space_snapshot"` so reverse sync knows these UVs are derived, not artist-authored.

### 6.5 Metadata: Sidecar Files (Not Embedded)

**NEVER embed metadata in .vmat files.** Reasons:
1. Hand-editing .vmat is common; embedded metadata gets corrupted
2. Source 2 compiler may strip unrecognized keys in future updates
3. Stale metadata causes silent data loss on reverse sync

Instead, use `.vmat.blend_meta` sidecar files:

```ini
# Blender Material Metadata - generated by bridge v4
# Safe to delete; bridge will regenerate on next export
blender_material_name = Chrome_Panel
blender_file_hash = a3f8c901...
vmat_content_hash = 7b2e1f...
original_roughness = 0.35
original_ior = 1.45
baked_channels = base_color, roughness
bake_resolution = 2048
uv_channel = 0
shader_selection_reason = complex_metallic (metallic=0.85)
```

**Staleness detection:** On reverse sync, recompute hash of .vmat parameters. If it differs from `vmat_content_hash`, the material was edited in s&box. Warn: "Material 'Chrome_Panel' was modified in s&box since last export. Reverse sync will overwrite. Proceed?"

### 6.6 What Gets Dropped vs. Approximated

| Feature | Status | Handling |
|---------|--------|----------|
| Base color, metallic, roughness, normal, emission | **Lossless** (with corrections above) | Direct mapping |
| SSS | **Approximated** | Map to `skin.shader` with `g_flCurvatureScale`, `g_vTransmissionColor`, `g_flTransmissionFalloff` |
| Vertex colors | **Passthrough** | Transfer as mesh vertex attribute; optionally bake to texture |
| Anisotropy | **Approximated** | Shader flag set, but rendered isotropically in some fallbacks |
| Displacement | **Metadata only** | s&box can't tessellate. Warn: "Apply SubSurf modifier before export" or bake to normal (lossy) |
| Complex layered materials | **Dropped** | Warning with manual steps |
| Procedural textures | **Baked** (Tier 3) | Quality loss warning, cannot rebuild procedurals on reverse sync |

### 6.6b Material Ownership — What Each State Means

| material_channel | .vmat on disk | Blender material | Sync behavior |
|-----------------|---------------|-----------------|---------------|
| BLENDER | Bridge writes/overwrites .vmat freely | Authoritative source | Blender → s&box only. s&box material changes ignored. |
| SBOX | Bridge does NOT overwrite .vmat | Read-only mirror | s&box → Blender only. Requires SourceIO to reverse-parse .vmat_c into Principled BSDF. |
| BIDIRECTIONAL | Bridge writes .vmat; monitors for external changes | Active both ways | Last-write-wins. Staleness detection via sidecar hash. If both sides changed since last sync, show conflict dialog with field-level diff. |

**When material_channel = SBOX (reverse material sync):**
1. s&box sends `material_sync` message with .vmat path
2. Blender reads the .vmat file (or .vmat_c via SourceIO if only compiled exists)
3. Parses shader type, texture paths, scalar parameters
4. Builds a Principled BSDF node tree:
   - TextureColor → Base Color image texture node
   - TextureNormal → Normal Map node
   - TextureRoughness → Roughness image texture node
   - g_flMetalness → Metallic socket value
   - g_vColorTint → Base Color multiply node
5. Textures are loaded from s&box content directory (NOT copied)
6. Material tagged as `sbox_synced = True` to prevent re-export

**Field-level diff for staleness (answering the open question):**
```python
def compute_material_diff(sidecar_state: dict, current_vmat: dict) -> list[str]:
    diffs = []
    for key in set(sidecar_state.keys()) | set(current_vmat.keys()):
        old = sidecar_state.get(key)
        new = current_vmat.get(key)
        if old != new:
            diffs.append(f"{key}: {old} → {new}")
    return diffs
# Example output: ["g_flRoughnessScaleFactor: 0.3 → 0.7", "TextureColor: brick_a.png → brick_b.png"]
```

### 6.7 Texture Naming Convention (CONFIRMED)

s&box auto-detects texture purpose from filename suffixes. The bridge MUST use these:

| Exported File | Suffix | VMAT Parameter | Feature Flag |
|--------------|--------|---------------|-------------|
| `{mat}_color.png` | `_color` | `TextureColor` | — |
| `{mat}_normal.png` | `_normal` | `TextureNormal` | — (OpenGL, NO flip) |
| `{mat}_rough.png` | `_rough` | `TextureRoughness` | — |
| `{mat}_metal.png` | `_metal` | `TextureMetalness` | `F_METALNESS_TEXTURE 1` |
| `{mat}_selfillum.png` | `_selfillum` | `TextureSelfIllumMask` | `F_SELF_ILLUM 1` |
| `{mat}_trans.png` | `_trans` | `TextureTranslucency` | `F_TRANSLUCENT 1` |
| `{mat}_ao.png` | `_ao` | `TextureAmbientOcclusion` | — |

Using these suffixes means s&box's Material Editor auto-populates texture slots when opened manually.

### 6.8 Hotspot Material Integration — Two Distinct Workflows

#### Workflow A: Custom Hotspot Atlas (Blender → s&box)

1. Artist defines a trim sheet texture atlas in Blender's Image Editor
2. Defines hotspot regions via Anvil-style tool (rectangular UV regions, orientation tags)
3. Saves to `hotspots.json` (Blender-side only, versioned in project):
```json
{
  "atlas": "trim_sheet_metal_01.png",
  "hotspots": [
    {"name": "wall_panel", "uv_min": [0.0, 0.0], "uv_max": [0.5, 0.25], "orientation": "walls"},
    {"name": "floor_tile", "uv_min": [0.5, 0.0], "uv_max": [1.0, 0.25], "orientation": "floor"}
  ]
}
```
4. When face is assigned to a hotspot, Blender computes UVs to fit the face into that UV rectangle
5. Bridge exports: atlas texture as .png → s&box content dir, .vmat referencing it, face UVs in mesh_update
6. s&box receives UVs directly — no hotspot concept needed on s&box side
7. `hotspots.json` lives at `project/assets/blender_bridge/hotspots.json` (Blender writes, s&box ignores)

#### Workflow B: s&box Built-in _hs Materials (s&box → Blender)

1. Bridge C# side enumerates `_hs` materials: `ResourceLibrary.GetAll<Material>()` filtered by `_hs` suffix
2. For each _hs material, extract: atlas texture path, UV layout (from the .vmat's texture coordinates)
3. Send material catalog to Blender: `{"type": "material_catalog", "hotspot_materials": [...]}`
4. Blender-side picker shows thumbnails (via SourceIO VTEX decoder)
5. Artist picks an _hs material and assigns to faces
6. **UV space mapping**: s&box _hs materials use the full [0,0]→[1,1] UV space of their atlas texture. The bridge assigns UVs by matching face aspect ratio to atlas regions — but the atlas region definitions must come FROM the _hs material's metadata (if available) or be inferred from the atlas texture layout
7. Bridge sends: face-material assignment (vmat_path) + computed UVs in mesh_update
8. s&box applies the _hs material reference + UVs. No texture copying needed (material already exists)

**Key distinction:** Workflow A exports new materials. Workflow B references existing materials. Both send UVs from Blender because Blender is where the face-to-region assignment happens.

**Orientation tagging** (walls/floor/ceiling) is **Blender-side preprocessing only**. s&box has no equivalent concept — it receives the final UV assignment, not the orientation logic.

### 6.9 Material Instancing

- 10 objects sharing one Blender material datablock = one .vmat file (correct instancing)
- Per-object material slot overrides = separate .vmat files (derived variants)
- Material name collisions: append content hash suffix

### 6.8 Export Dialog Requirements (Trust Building)

For artists to trust the pipeline, the export dialog must show:
1. Per-material status panel: source value, target parameter, lossless/approximated/dropped
2. Warning panel: missing UVs, non-power-of-2, approximated params, dropped features
3. Bake time estimate before export begins
4. **Dry-run mode**: produces status report without writing any files

---

## 7. Identity & Desync Prevention

### 7.1 Stable ID System

v3 uses `sbox_bridge_id` as a single custom property. This is lost on undo, paste, or reload.

v4 uses **triple-storage** for redundancy:

```python
# Storage 1: Object custom property
obj["sbox_bridge_stable"] = stable_id

# Storage 2: Mesh data property (survives object undo)
obj.data["sbox_bridge_stable"] = stable_id

# Storage 3: Scene-level registry (survives both)
scene["sbox_id_registry"][obj.name] = stable_id
```

**ID generation:**
```python
stable_id = sha256(scene_path + object_data_name + creation_timestamp)[:12]
```

**Recovery priority:** custom property -> mesh data property -> scene registry -> re-identification by geometry hash + name + position.

### 7.2 Version Tracking

Per-object, both sides:

```json
{
  "stable_id": "a1f3c2e90b4d",
  "local_version": 7,
  "remote_version": 5,
  "geometry_hash": "f8c2...",
  "transform_hash": "0a3b..."
}
```

### 7.3 O(1) Bridge ID Lookup

v3: `find_by_bridge_id()` scans ALL objects linearly.

v4: Dict cache, rebuilt on connect:

```python
_bridge_id_cache: dict[str, bpy.types.Object] = {}

def find_by_bridge_id(bridge_id):
    return _bridge_id_cache.get(bridge_id)

def rebuild_cache():
    _bridge_id_cache.clear()
    for obj in bpy.data.objects:
        bid = obj.get("sbox_bridge_stable")
        if bid:
            _bridge_id_cache[bid] = obj
```

### 7.4 Echo Suppression (Replacing One-Shot)

v3's one-shot suppression (`_last_write_seq` deleted on first echo) is fragile. Two rapid updates before echo returns = false positive.

v4 uses **sequence numbering per object with echo_of_seq:**

```python
class SyncState:
    def __init__(self):
        self.local_seq = 0
        self.remote_seq = 0
    
    def send_update(self, obj):
        self.local_seq += 1
        bridge.send({
            "seq": self.local_seq,
            "echo_of": self.remote_seq  # "I've seen your version N"
        })
    
    def receive_update(self, msg):
        if msg.echo_of >= self.local_seq:
            return  # Echo of our own update, drop
        self.remote_seq = msg.seq
        apply(msg)
```

### 7.5 Dirty-Bit Change Detection

v3: Full geometry hash (MD5 of all vertices) on every depsgraph update.

v4: Depsgraph-driven dirty bit:

```python
_dirty_objects: set[str] = set()

def on_depsgraph_update(scene, depsgraph):
    for update in depsgraph.updates:
        obj = update.id
        if hasattr(obj, "get") and obj.get("sbox_bridge_stable"):
            if update.is_updated_geometry:
                _dirty_objects.add(obj["sbox_bridge_stable"])
            elif update.is_updated_transform:
                send_transform(obj)  # Transforms are cheap, send immediately
```

Geometry hash computed ONLY for objects in `_dirty_objects`, and only when actually preparing to send a mesh update (not every frame).

---

## 8. Transport Layer

### 8.1 WebSocket (Replacing HTTP Polling)

v3: HTTP POST /message + GET /poll every 100ms.

v4: Persistent WebSocket connection with binary framing.

**Benefits:**
- Push-based: s&box can send updates immediately, no polling delay
- Binary frames: struct.pack for mesh data instead of JSON float arrays
- Single persistent connection: no TCP handshake per message
- Heartbeat: automatic connection health monitoring

**Message format:**
```
[1 byte: message type]
[4 bytes: payload length]
[N bytes: payload (JSON for control, binary for mesh data)]
```

### 8.1b Connection Lifecycle & Startup Sequence

**1. Startup States**

When the addon loads, the connection state is `DISCONNECTED`. There is no auto-connect behavior. The user must explicitly click the "Connect" button in the panel. If s&box is not running or the bridge server is not reachable, the addon displays:

> "Cannot reach s&box on localhost:8099. Is the Bridge addon enabled in s&box?"

**2. Disconnected Operation Queue**

When disconnected, ALL outbound operations (creates, mesh updates, transforms, material assignments) are queued in an internal buffer:

```python
_outbound_queue: list[BridgeMessage] = []
```

Objects with queued operations display a yellow "QUEUED" badge (clock icon) in the viewport and panel. When the connection is established, the queue does NOT flush automatically. Instead, the queue is presented during the hello/hello_ack reconciliation phase via a dialog:

```
╔═══════════════════════════════════════════════╗
║  12 queued operations from disconnected       ║
║  session. Review and send?                    ║
╠═══════════════════════════════════════════════╣
║  [Review Plan]  [Send All]  [Discard]         ║
╚═══════════════════════════════════════════════╝
```

- **Review Plan**: Opens the standard sync plan preview (Section 9.1) with all queued operations listed.
- **Send All**: Flushes the entire queue in order.
- **Discard**: Clears the queue. Objects remain in Blender but are not sent to s&box. Their "QUEUED" badges are removed.

**3. Session-Level Authority Default**

The addon preferences include a session-level default for ownership assignment on newly created objects:

```python
authority_default: bpy.props.EnumProperty(
    items=[
        ("BLENDER", "Blender First", "New objects default to BLENDER-owned"),
        ("SBOX", "s&box First", "New objects default to SBOX-owned"),  
        ("UNLOCKED", "Unlocked", "New objects are unclaimed until first edit"),
    ],
    default="BLENDER",
    description="Default ownership for newly created objects"
)
```

This setting gates depsgraph behavior:
- If `authority_default=BLENDER`, new objects are auto-claimed by Blender on creation. The depsgraph handler immediately assigns `shape_owner=BLENDER` and `channels=BLENDER`.
- If `authority_default=SBOX`, new objects are assigned `shape_owner=SBOX` and `channels=SBOX`. This is useful when Blender is being used purely as a viewer for engine-authored content.
- If `authority_default=UNLOCKED`, new objects remain unclaimed (`shape_owner=UNLOCKED`, `channels=BIDIRECTIONAL`) until the first edit on either side claims them.

**4. Heartbeat**

The WebSocket connection uses a heartbeat mechanism with a **5-second interval**. Each side sends a `heartbeat` message every 5 seconds. If **3 consecutive heartbeats are missed** (15 seconds of silence), the connection is declared dead.

On unclean disconnect (crash, network failure):
- The server releases the crashed client's BLENDER-owned objects after a configurable timeout (default: **60 seconds**).
- Other connected clients see a notification:

```
"Artist A disconnected. Their objects will be released in 45s. [Release Now] [Keep Locked]"
```

- **Release Now**: Immediately transitions all of Artist A's objects to `UNLOCKED`, allowing other artists to claim them.
- **Keep Locked**: Maintains the ownership lock indefinitely until Artist A reconnects or an admin force-releases.

**5. Version Negotiation**

The `hello` message includes a `protocol_version: int` field. If the server and client protocol versions disagree, the server sends a version mismatch error and closes the connection:

```json
{
  "type": "version_mismatch",
  "server_version": 2,
  "client_version": 1
}
```

The client displays: "Bridge protocol version mismatch. Update your addon." The connection is not established, and no data is exchanged. This prevents silent incompatibility between mismatched addon/server versions.

### 8.2 WebSocket Handshake & Session Protocol

**Missing from initial design — added after review.**

On WebSocket connect, the client sends a `hello` message as the first frame:

```json
{
  "type": "hello",
  "protocol_version": 1,
  "user_id": "artist_a@workstation1",
  "blend_file": "C:/projects/my_level.blend",
  "known_ids": ["a1f3c2e90b4d", "b2e4d1f80c3a", ...],
  "known_versions": { "a1f3c2e90b4d": 7, "b2e4d1f80c3a": 3 }
}
```

Server responds with `hello_ack`:

```json
{
  "type": "hello_ack",
  "session_id": "srv_9f8e7d6c",
  "server_version": 1,
  "scene_name": "my_level.scene",
  "active_users": ["artist_a@workstation1", "engineer_b@workstation2"],
  "full_state": [
    {
      "stable_id": "a1f3c2e90b4d",
      "name": "Pillar_01",
      "shape_owner": "BLENDER:artist_a@workstation1",
      "transform_channel": "BIDIRECTIONAL",
      "version": 7,
      "geometry_hash": "f8c2...",
      "transform": [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0]
    },
    ...
  ]
}
```

This solves the **cold-start problem**: new client gets full scene state on connect. The `known_ids` + `known_versions` fields enable delta sync — server only sends full geometry for objects the client doesn't have or has stale versions of.

**Reconnect flow:**
1. Client sends `hello` with its last-known `known_ids` and `known_versions`
2. Server compares against current state
3. For each object: if client version < server version, include full state in `hello_ack`
4. For objects client has that server doesn't (deleted while disconnected), include `{"stable_id": "xxx", "deleted": true}`
5. Client applies delta, shows post-sync report (P0-C)

**Server-side session management:**
```csharp
private readonly ConcurrentDictionary<string, ClientSession> _clients = new();

class ClientSession {
    public WebSocket Socket;
    public string UserId;       // from hello message
    public string BlendFile;
    public HashSet<string> KnownIds;
    public DateTime ConnectedAt;
    public DateTime LastHeartbeat;
}

async Task OnWebSocketConnect(HttpListenerWebSocketContext ctx) {
    var hello = await ReceiveJson<HelloMessage>(ctx.WebSocket);
    var session = new ClientSession {
        Socket = ctx.WebSocket,
        UserId = hello.UserId,
        BlendFile = hello.BlendFile,
        KnownIds = new HashSet<string>(hello.KnownIds),
        ConnectedAt = DateTime.UtcNow,
    };
    _clients[hello.UserId] = session;
    
    var ack = BuildHelloAck(session);
    await SendJson(ctx.WebSocket, ack);
    
    // Broadcast to other clients: "artist_a connected"
    await BroadcastExcept(hello.UserId, new {
        type = "user_joined", user_id = hello.UserId
    });
    
    await ReceiveLoop(session);
}
```

### 8.3 Mesh Data Wire Format

v3: JSON arrays of floats (`"vertices": [1.0, 2.0, 3.0, ...]`)

v4: Binary struct-packed:
```python
# Blender sends:
header = struct.pack('IIII', vertex_count, face_count, uv_count, has_normals)
verts = struct.pack(f'{vertex_count * 3}f', *flat_vertices)
faces = struct.pack(f'{face_data_len}i', *face_data)
uvs = struct.pack(f'{uv_count * 4}I', *uv_entries)  # face_idx, vert_idx, u, v per entry
if has_normals:
    # Per-loop custom normals (same indexing as UVs: face_idx + vert_idx keyed)
    normals = struct.pack(f'{normal_count * 5}f', *normal_entries)  # face_idx, vert_idx, nx, ny, nz
```

> **Custom normals are critical for hard-surface art.** Without them, s&box recomputes normals from face geometry, destroying bevels, creases, and weighted normals that artists carefully set. The bridge extracts custom normals from `mesh.loops[i].normal` (with `mesh.calc_normals_split()` called first) and sends them alongside UVs. On the s&box side, normals are applied via the Vertex struct's Normal field for ModelBuilder meshes.

**Estimated savings:** 60-75% bandwidth reduction for mesh data.

### 8.4 UV Wire Format (Vertex-ID Keyed, Not Implicit)

**Review correction:** Sections 12.7 and Appendix C previously contradicted each other — 12.7 proposed vertex-ID keying while Appendix C said implicit indexing. **Vertex-ID keying wins.** Implicit indexing assumes both sides iterate faces in the same order, which is NOT guaranteed after undo/redo in Blender or after topology changes in s&box.

UV data is sent as a binary frame with explicit face+vertex indices:

```
[uint32 entry_count]
[per entry: uint32 face_idx, uint32 vert_idx, float32 u, float32 v]
```

Size: 16 bytes per UV entry. For 10K faces, avg 4 verts/face = 40K entries = **640KB**.
This is ~2x the implicit approach but eliminates all ordering assumptions.

**C# application on PolygonMesh (the missing traversal code):**

```csharp
void ApplyUVsFromBridge(PolygonMesh mesh, Dictionary<(int faceIdx, int vertIdx), Vector2> uvMap) {
    int faceIdx = 0;
    foreach (var face in mesh.Faces) {
        // Traverse half-edges around the face
        var firstEdge = face.Edge;
        var currentEdge = firstEdge;
        do {
            int vertIdx = currentEdge.Vertex.Index;
            var key = (faceIdx, vertIdx);
            if (uvMap.TryGetValue(key, out var uv)) {
                mesh.SetTextureCoord(currentEdge, uv);
            }
            currentEdge = currentEdge.Next;
        } while (currentEdge != firstEdge);
        faceIdx++;
    }
}
```

This replaces the dangerous `TextureAlignToGrid` call and preserves Blender's artist UVs.

---

## 9. UX & UI Design

### 9.1 Sync Plan Preview

Before ANY data moves between Blender and s&box, the user sees a confirmation dialog:

```
╔═══════════════════════════════════════════╗
║  Sync Plan: Send 5 objects to s&box       ║
╠═══════════════════════════════════════════╣
║  ✓ Pillar_01   geometry + transforms      ║
║  ✓ Pillar_02   geometry + transforms      ║
║  ✓ Floor_01    transforms only (SBOX geo) ║
║  ⚠ Wall_03     BLOCKED: geometry owned    ║
║                by s&box                    ║
║  ─ Citizen     SKIP: VMDL proxy           ║
╠═══════════════════════════════════════════╣
║  [Send Allowed]  [Cancel]                 ║
╚═══════════════════════════════════════════╝
```

**Dialog behavior:**
- **Modal** (blocks Blender interaction while open). Rationale: the plan reflects a point-in-time snapshot of both sides. Allowing edits while the dialog is open would invalidate the plan.
- **Stale detection:** If the dialog has been open >5 seconds, re-query s&box state before executing. Show "Plan updated — 1 object changed since you opened this dialog" if anything differs.
- **Cancel is always safe:** Closing the dialog without clicking "Send Allowed" has zero side effects.
- **Keyboard:** Enter = Send Allowed, Escape = Cancel.

### 9.2 Viewport Overlay Colors

| State | Color | Meaning |
|-------|-------|---------|
| Synced (clean) | Green outline | Up-to-date on both sides |
| Modified locally | Yellow outline | Pending changes to send |
| Conflict | Red outline | Both sides modified since last sync |
| VMDL Proxy | Cyan wireframe | Read-only reference |
| Disconnected proxy | Gray wireframe | Stale, not syncing |

### 9.3 Blender Panel Layout

```
┌─ s&box Bridge ──────────────────────┐
│ Status: Connected ●                  │
│ Scene: my_level.scene                │
│                                      │
│ [Send Selected] [Pull Selected]      │
│ [Send Collection ▾] [Sync Plan]      │
│ [Pause Sync] [Activity Log]         │
│                                      │
│ ── Selected: Pillar_01 ──           │
│ Shape: BLENDER (yours)              │
│ Transforms: BIDIRECTIONAL            │
│ Materials: BLENDER                   │
│ [Transfer Shape to s&box]           │
│ [Lock / Unlock]                      │
│                                      │
│ ── Materials ──                      │
│ [Export Materials] [Preview ▾]       │
│ [Dry Run] [Import from s&box]       │
│                                      │
│ ── VMDL Proxies ──                  │
│ [Import VMDL Reference]             │
│ [Verify Proxy Integrity]            │
│ citizen.vmdl ● Connected            │
│ prop_barrel.vmdl ● Connected        │
└──────────────────────────────────────┘
```

### 9.4 Activity Log

Scrollable log with clickable object links:

```
[10:31:02] Sent Pillar_01 geometry (1,247 verts)
[10:31:03] Received Pillar_01 position from s&box
[10:31:15] ⚠ Wall_03 geometry BLOCKED (owned by s&box)
[10:31:20] Imported citizen.vmdl proxy (read-only)
[10:32:01] Material 'Chrome_Panel' exported (vr_complex, 2 textures baked)
```

**Specification:**
- **Persistence**: In-memory only during session. NOT saved to disk. Cleared on disconnect. Rationale: log entries reference runtime state (object handles) that don't survive reload.
- **Scope**: Per-scene (each connected scene has its own log).
- **Retention**: Last 500 entries. Older entries discarded (FIFO).
- **Clicking an object link**: Selects the object in Blender's viewport and centers the view on it (`bpy.ops.object.select_all(action='DESELECT')` → `obj.select_set(True)` → `bpy.ops.view3d.view_selected()`).
- **Error display**: Server rejections (`mesh_nack`, ownership denied, etc.) appear as red entries: `[10:31:15] ✗ Wall_03 mesh REJECTED by s&box: OWNERSHIP_DENIED`
- **s&box side**: The s&box addon has its own log (in the Bridge tool panel). Logs are NOT synchronized between sides — each side logs its own operations.
- **Export**: "Copy Log" button copies plain text to clipboard for bug reports.

---

## 10. Safeguards & Corruption Prevention

### 9.5 Server-Side Error Recovery (C#)

The Python side has exhaustive error handling. The C# server needs its own:

| Error | Detection | Response | Client Notification |
|-------|-----------|----------|-------------------|
| Malformed binary frame | Deserialization throws | Drop frame, log warning | `{"type": "error", "code": "MALFORMED_FRAME", "detail": "..."}` |
| Unknown stable_id | ID not in server registry | Log, return error | `{"type": "error", "code": "UNKNOWN_ID", "stable_id": "..."}` |
| Scene.BatchGroup() throws | try/catch around bulk ops | Rollback partial creates, log | `{"type": "error", "code": "BATCH_FAILED", "created": N, "failed": M}` |
| WebSocket send fails | SendAsync throws | Remove client from _clients dict, log | (client is already gone) |
| Disk full during .vmat write | IOException | Atomic write pattern (temp+rename) prevents partial files | `{"type": "error", "code": "DISK_FULL"}` |
| Concurrent modification | PolygonMesh throws | Retry once with lock, then fail | `{"type": "error", "code": "CONCURRENT_MOD", "stable_id": "..."}` |

```csharp
async Task HandleMeshUpdate(ClientSession client, byte[] payload) {
    try {
        var (stableId, verts, faces, uvs) = DeserializeMeshFrame(payload);
        
        if (!_registry.TryGetValue(stableId, out var obj)) {
            await SendError(client, "UNKNOWN_ID", stableId);
            return;
        }
        
        // Check ownership before applying
        if (!CanAcceptGeometry(obj, client.UserId)) {
            await SendError(client, "OWNERSHIP_DENIED", stableId);
            return;
        }
        
        using (Scene.BatchGroup()) {
            ApplyMeshData(obj.GameObject, verts, faces, uvs);
        }
        
        // Broadcast to other clients
        await BroadcastMeshUpdate(client.UserId, stableId, payload);
        await SendJson(client, new { type = "mesh_ack", stable_id = stableId });
    }
    catch (FormatException ex) {
        await SendError(client, "MALFORMED_FRAME", ex.Message);
    }
    catch (Exception ex) {
        Log.Error($"[Bridge] Mesh update failed: {ex}");
        await SendError(client, "INTERNAL_ERROR", ex.Message);
    }
}
```

### 10.0 Critical UX Safety Issues (P0)

These were identified through scenario-based UX auditing and MUST be resolved before any public release.

#### P0-A: Undo Destroys Bridge IDs

**Problem:** Blender's undo stack includes custom properties. If an artist presses Ctrl+Z past the point where a bridge ID was assigned, the `sbox_bridge_stable` property disappears. The object still exists in s&box with that ID. Blender no longer has a matching object. Result: orphaned duplicates or silent data loss.

**Solution: External ID Registry (outside undo stack)**

```python
# Session-scoped storage NOT in undo stack
# Option A: WindowManager property (session-only, not saved to .blend)
# Option B: External JSON file alongside .blend

# Write-ahead log pattern: registry write MUST complete before Blender state changes.
# If registry write fails, the bridge operation is aborted (no state change occurs).

REGISTRY_PATH = blend_file_path.with_suffix(".sbox_bridge_ids.json")
WAL_PATH = blend_file_path.with_suffix(".sbox_bridge_ids.wal")

def bridge_operation_with_wal(operation_fn):
    """Wrapper ensuring registry is updated BEFORE Blender state changes."""
    # Step 1: Write intended change to WAL (write-ahead log)
    wal_entry = {"op": operation_fn.__name__, "timestamp": time.time()}
    atomic_write(WAL_PATH, json.dumps(wal_entry))
    
    # Step 2: Update registry (if this fails, WAL records the intent)
    try:
        registry = load_registry(REGISTRY_PATH)
        operation_fn(registry)  # modifies registry dict
        atomic_write(REGISTRY_PATH, json.dumps(registry))
    except (IOError, OSError) as e:
        # Registry write failed — abort the operation
        log.error(f"Registry write failed: {e}. Operation aborted.")
        raise BridgeOperationAborted(str(e))
    
    # Step 3: Only NOW modify Blender state
    # (if we crash here, registry is already correct — on restart,
    #  on_undo_detected will re-inject from registry)
    
    # Step 4: Clean up WAL
    WAL_PATH.unlink(missing_ok=True)

def on_undo_detected():
    """Called after every undo/redo via bpy.app.handlers.undo_post"""
    # Check for incomplete WAL (crash recovery)
    if WAL_PATH.exists():
        log.warning("Found incomplete WAL — last operation may have crashed mid-write")
        # WAL exists = registry was written but Blender state may be stale
        # Re-inject all IDs from registry
    
    registry = load_registry(REGISTRY_PATH)
    for stable_id, obj_info in registry.items():
        obj = find_object_by_name_and_data(obj_info)
        if obj and not obj.get("sbox_bridge_stable"):
            obj["sbox_bridge_stable"] = stable_id
            obj.data["sbox_bridge_stable"] = stable_id
            log.warning(f"Re-linked {obj.name} after undo (ID: {stable_id})")
    
    orphaned = find_orphaned_ids(registry)
    if orphaned:
        show_warning(f"{len(orphaned)} bridge objects removed by undo. "
                     "They still exist in s&box. Redo to restore, or "
                     "click 'Re-link Scene' to attempt matching.")
```

**Registration:** `bpy.app.handlers.undo_post.append(on_undo_detected)`

This handler fires after EVERY undo/redo and cross-checks the external registry against scene state. The external registry file is updated on every bridge operation (create, delete, ownership change) but is NOT part of Blender's undo stack.

#### P0-B: Team Workflow — No Session Identity

**Problem:** If two artists connect to the same s&box scene, ownership is per-object but not per-user. Artist B can "Claim Ownership" and silently steal Artist A's objects. Artist A's next send gets rejected with no context.

**Solution: Per-User Session Identity**

```python
# Stored in addon preferences (persists across files)
class BridgePreferences(bpy.types.AddonPreferences):
    user_id: bpy.props.StringProperty(
        name="User ID",
        description="Identifies you in multi-user sessions",
        default=""  # Auto-generated on first use: f"{os.getlogin()}@{hostname}"
    )

# Ownership becomes: BLENDER:artist_a@workstation1, not just BLENDER
# Claiming another user's object requires explicit "Force Claim" with confirmation:
# "Pillar_01 is owned by Artist A (last edited 5 min ago). Force-claiming will
#  lock them out. Proceed?"
```

**Pre-send lock check:** The Send button is grayed out for objects owned by other sessions, with tooltip: "Owned by Artist A -- request ownership to edit."

#### Ownership Transfer Request Flow (Non-Force)

Instead of force-claiming, Artist A can REQUEST ownership from Artist B:

1. Artist A right-clicks SBOX-owned object → "Request Ownership"
2. Bridge sends to server: `{"type": "ownership_request", "stable_id": "xxx", "requester": "artist_a@ws1"}`
3. Server forwards to current owner (Artist B): `{"type": "ownership_request", "stable_id": "xxx", "requester": "artist_a@ws1"}`
4. Artist B sees toast notification: "Artist A requests ownership of Pillar_01. [Grant] [Deny]"
5. If Grant: server transfers ownership, notifies both sides
6. If Deny: Artist A sees "Request denied by Artist B"
7. If no response within 30 seconds: request expires, Artist A notified
8. If Artist B is offline: "Artist B is offline. [Force Claim] [Cancel]"

The force-claim still exists as an escape hatch but the polite request flow is the DEFAULT for multi-user.

**user_id collision fix:** Change from `f"{os.getlogin()}@{hostname}"` to:
```python
# user_id: manually settable display name with auto-generated fallback
user_id: bpy.props.StringProperty(
    name="User ID", 
    default="",  # Empty = auto-generate on first use
    description="Your identity in multi-user sessions. Set a unique name."
)

def get_user_id(prefs):
    if prefs.user_id:
        return prefs.user_id
    # Auto-generate but let user override
    auto_id = f"{os.getlogin()}@{socket.gethostname()}"
    prefs.user_id = auto_id  # Save so user can edit it
    return auto_id
```

#### P0-C: Pull Feedback — Behavior Varies Per Object Silently

**Problem:** When pulling 20 objects, 15 get full updates but 5 (BLENDER-owned) only get transform updates. The user sees one button, one action, but 20 different outcomes with no feedback.

**Solution: Post-Sync Report Panel**

After every sync operation, display a dismissible panel in the sidebar:

```
Sync Complete (20 objects)
  15 objects: geometry + transforms from s&box
  5 objects: transforms only (geometry preserved — Blender-owned)
  [Dismiss]
```

This is NOT a popup (would block workflow). It's an inline panel that auto-dismisses after 30 seconds or on user interaction.

### 10.1 Atomic Operations

All file writes use the write-to-temp-then-rename pattern:

```python
def atomic_write(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(data, encoding="utf-8")
    os.replace(temp, path)  # Atomic on NTFS
```

### 10.2 Manifest Redundancy

- Primary: `manifest.json`
- Backup: `manifest.json.bak` (copy before every write)
- Tertiary: Per-file `.meta` sidecars (can rebuild manifest from these)
- Schema validation on every load

### 10.3 Undo Safety

Bridge IDs stored in triple-storage (Section 7.1). If Ctrl+Z removes the custom property:
1. Check mesh data property
2. Check scene registry
3. If all three lost: re-identify by geometry hash + name + position

### 10.4 What We NEVER Do

- Never modify original vmdl files (always work with copies)
- Never auto-overwrite without confirmation dialog
- Never delete objects without explicit user action
- Never send geometry for VMDL proxy objects
- Never run cleanup silently (always confirmation dialog)
- Never trust embedded metadata over current file state (hash-verify first)

---

## 11. s&box Engine API Reference

### 11.1 Three Tiers of Mesh Generation

**Tier 1 — `Sandbox.Mesh` (low-level runtime):**
```csharp
// Raw vertex/index buffers — most control, most work
var mesh = new Mesh(material);
mesh.CreateVertexBuffer<Vertex>(verts.Length, verts);
mesh.CreateIndexBuffer(indices.Length, indices);
mesh.Bounds = new BBox(mins, maxs);
```

**Tier 2 — `ModelBuilder` (model construction with collision):**
```csharp
// Fluent builder pattern — creates Model objects
var model = new ModelBuilder()
    .AddMesh(renderMesh)
    .AddCollisionMesh(vertices, indices)
    .Create();
var renderer = go.AddComponent<ModelRenderer>();
renderer.Model = model;
```

**Tier 3 — `PolygonMesh` + `MapMesh` (Hammer/Editor geometry):**
```csharp
var mesh = new PolygonMesh();
var hVertices = mesh.AddVertices(vertices);  // Vector3[]
var hFace = mesh.AddFace(vertexHandles);     // VertexHandle[]
mesh.SetFaceMaterial(hFace, material);       // Per-face material
mesh.SetTextureCoord(halfEdge, uv);          // Per-vertex-per-face UVs
mesh.TextureAlignToGrid(mesh.Transform);     // WARNING: Destroys custom UVs
mesh.SetSmoothingAngle(40.0f);
meshComp.Mesh = mesh;
```

**Critical**: Do NOT call `TextureAlignToGrid` when receiving mesh from Blender. It overwrites all UV data. Instead, apply Blender's UVs via `SetTextureCoord` per half-edge.

**Which tier to use?** Hammer is planned for eventual removal. Target the Scene system (Tier 1/2 with `ModelBuilder`) for future-proofing. Use Tier 3 only if the user is working in Hammer specifically.

### 11.2 Material System (CONFIRMED)

```csharp
// Programmatic creation — CONFIRMED WORKING
Material mat = Material.Create("my_material", "complex.vfx");
mat.Set("TextureColor", myTexture);
mat.Set("TextureNormal", normalTex);
mat.Set("TextureRoughness", roughTex);
mat.Set("TextureMetalness", metalTex);
mat.Set("g_vColorTint", new Vector4(1, 0, 0, 1));
mat.Set("g_flMetalness", 0.8f);           // Uniform metalness (no texture)
mat.Set("g_flModelTintAmount", 1.0f);
mat.SetFeature("F_SPECULAR", true);
mat.SetFeature("F_METALNESS_TEXTURE", true);  // REQUIRED for metalness textures

// Loading existing
var mat = Material.Load("materials/my_material.vmat");
```

**.vmat file format** (plain text, NOT binary):
```
Layer0
{
    shader "complex.vfx"
    F_METALNESS_TEXTURE 1
    F_SPECULAR 1
    TextureColor "materials/model/albedo.png"
    TextureNormal "materials/model/normal.png"
    TextureRoughness "materials/model/rough.png"
    TextureMetalness "materials/model/metal.png"
    TextureAmbientOcclusion "materials/model/ao.png"
    TextureSelfIllumMask "materials/model/emissive.png"
    g_vColorTint "[1.000000 1.000000 1.000000 0.000000]"
    g_flModelTintAmount "1.000"
}
```

**Texture slots confirmed:**
| VMat Parameter | Blender Equivalent |
|---|---|
| `TextureColor` | Base Color |
| `TextureNormal` | Normal Map |
| `TextureRoughness` | Roughness |
| `TextureMetalness` | Metallic (requires F_METALNESS_TEXTURE) |
| `TextureAmbientOcclusion` | AO |
| `TextureSelfIllumMask` | Emission (requires F_SELF_ILLUM) |
| `TextureTranslucency` | Alpha |

Source textures (PNG, TGA, PSD) auto-compile to `.vtex_c` by engine.

### 11.3 Model/VMDL

```csharp
// vmdl = compiled model (skeleton, LODs, materials, physics)
// Source from .fbx file, compiled by asset pipeline
// NO native glTF/GLB support — must go through FBX → vmdl pipeline
var renderer = go.AddComponent<ModelRenderer>();
renderer.Model = Model.Load("models/citizen.vmdl");
```

### 11.4 Scene & Prefab Serialization

- `.scene` / `.prefab` — JSON format with `"_type": "gameobject"` discriminator
- Components with `[Property]` attributes serialize automatically
- `GameObject.JsonRead()` / `GameObject.JsonWrite()` for programmatic access
- `ResourceLibrary.Get<SceneFile>("scenes/level.scene")` for loading
- `SceneUtility.Instantiate()` for creating instances
- **Prefab system** may be more natural export target than `.scene` for bridge (self-contained, supports instancing)

### 11.4b Prefab Instancing (Architectural Decision)

**Decision: v4 does NOT model Blender instances as s&box prefab instances.** Rationale:
- Prefab creation requires writing a `.prefab` file, which is a heavyweight operation unsuitable for real-time sync
- Blender's instancing model (Alt+D linked duplicates, collection instances) does not map cleanly to s&box's prefab override system
- The complexity of tracking prefab source ↔ instance overrides ↔ Blender data blocks is out of scope for v4

**What v4 does instead:**
- Each object is sent as an independent GameObject, even if Blender has linked duplicates
- If 50 identical pillars exist in Blender (Alt+D instances), s&box gets 50 separate GameObjects with 50 copies of the mesh
- This is bandwidth-inefficient but correct and simple

**Future (v5+):** Prefab instancing is the #1 optimization target. The path would be:
1. Detect Blender linked duplicates (objects sharing the same `obj.data`)
2. First instance creates a .prefab file
3. Subsequent instances send `create_prefab_instance` messages referencing the prefab
4. Overrides (position, rotation, material slots) sent as instance deltas

### 11.5 Performance: Scene.BatchGroup() (CRITICAL)

```csharp
// MUST USE for bulk import — defers all OnEnable callbacks
using (Scene.BatchGroup())
{
    for (int i = 0; i < 500; i++)
    {
        var go = Scene.CreateObject();
        go.AddComponent<ModelRenderer>();
    }
}  // All OnEnabled callbacks fire here, in creation order
```

Without this, importing 500 objects causes 500 individual OnEnable callbacks with per-object overhead.

### 11.6 Auto-Reload via AssetSystem

**Writing files to disk triggers automatic recompilation.** The engine's AssetSystem monitors file CRCs. Writing a `.vmat`, `.scene`, or `.prefab` file directly to the content directory triggers on-demand recompilation. No explicit reload API needed.

This means the bridge can:
1. Write `.vmat` files to `assets/materials/blender_bridge/`
2. The engine auto-detects the change and recompiles
3. Materials immediately available for use

### 11.7 Light Components (Confirmed)

| Component | Key Properties | Blender Equivalent |
|-----------|---------------|-------------------|
| `PointLight` | Color, Radius/Range, Intensity, Shadows, LightSourceRadius | Point Light |
| `SpotLight` | + ConeInner, ConeOuter | Spot Light |
| `DirectionalLight` | + SkyColor | Sun Light |
| `AmbientLight` | Color (single) | World ambient |
| `Skybox2D` | Environment map, indirect light (~10k unit range) | World HDRI |
| `EnvmapProbe` | Reflection probe, priority ordering | Reflection Probe |

### 11.8 No Spline System

**Gap identified.** No spatial spline, bezier, NURBS, or path component exists. `Sandbox.Curve` is 1D only (parameter → float, like AnimationCurve). `LineRenderer` has `SplineInterpolation` but is rendering-only, not a path system.

For Blender curve import: sample/discretize to `List<Vector3>`, store in custom component, implement own interpolation.

---

## 12. Anvil & SourceIO Integration

### 12.1 Why These Tools Matter

The bridge alone syncs geometry and transforms. That's necessary but insufficient for production level design. Two existing open-source tools fill the critical gaps:

| Gap | Tool | What It Provides |
|-----|------|-----------------|
| No face-level texturing | **Anvil Level Design** (GPL-3.0) | Alt+LMB face painting, material picking, stretch-apply |
| No auto-UV workflow | **Anvil** | World-space UV Lock, depsgraph-driven re-UV on geometry changes |
| No geometry cutting tools | **Anvil** | Cube Cut (C key) — clean boolean-like edits, no N-gons/T-junctions |
| No trim sheet workflow | **Anvil** | Hotspot mapping with orientation-aware auto-assignment |
| No .vmat generation knowledge | **SourceIO** (MIT) | `complex.vfx` parameter mapping already implemented in Python |
| No s&box asset import | **SourceIO** | VMDL, VMAT, VTEX parsers for importing reference geometry |
| No material browser | **SourceIO** | ContentManager scans s&box addon dirs and VPK archives |

### 12.2 Anvil Level Design — Key Systems

**Face-Level Texturing:**
- `Alt+LMB` — Paint material onto face with seamless tiling
- `Alt+RMB` — Pick material from face (works across objects)
- `Shift+Alt+LMB` — Stretch-apply texture
- Material deduplication: one material per image filename (prevents .001 proliferation)
- File browser integration for direct image-to-face painting

**Auto-UV with World-Space Lock (highest-value feature):**
- UV Lock toggle: ON = UVs move with geometry, OFF = materials stay fixed in world space
- Powered by `depsgraph_update_post` handler tracking changed faces
- Automatically re-UVs only affected geometry on edits
- **Critical fragile detail:** Uses `ctypes` to disable Blender's built-in "Correct UV" via direct memory access (Python API doesn't expose this setting). Leaving it enabled causes face data memory shifts that crash the UV system.

**Cube Cut (C key):**
- Three-click modal: click face → define rectangle → set depth
- Produces clean topology: no N-gons, no T-junctions (critical for Source 2 mesh compilation)
- `W` inverts cut (additive ↔ subtractive)
- Only affects selected faces
- Direct mesh edit, not modifier-stack boolean

**Hotspot Mapping (Trim Sheets):**
- Define rectangular hotspot regions on a texture atlas in Image Editor
- Tag each with orientation: Any, Upwards/walls, Floor, Ceiling
- Algorithm: group connected faces → approximate as rectangle → match to best hotspot by aspect ratio
- Optional texel density weighting
- Serializes to `hotspots.json` for version control

**Grid & Navigation:**
- `[`/`]` — Grid doubling/halving
- Forced grid snapping on load
- WASD flying navigation (game-style, matches Hammer expectations)

### 12.3 SourceIO — Key Systems

**Pure Python Source 2 Parsers (MIT, `library/` package — no Blender dependency):**
- VMAT parser with `complex.vfx` shader handler — confirms ALL our parameter mappings
- VTEX decoder: DXT1, DXT5, BC7, BC6H, RGBA8888, float formats
- VMDL importer: vertex positions, normals, tangents, multi-UV, vertex colors, bone weights
- KV3 binary parser (read-only)
- ContentManager for game asset path resolution (VPK archives + filesystem)
- s&box explicitly listed as supported game

**What SourceIO's Complex Shader Handler Confirms:**

```python
# SourceIO's parameter mapping (already implemented, MIT license):
# These are the EXACT s&box parameter names:
TextureColor        # Base Color / Albedo
TextureNormal       # Normal Map
TextureRoughness    # Roughness
TextureMetalness    # Metallic (requires F_METALNESS_TEXTURE)
TextureAmbientOcclusion  # AO
TextureSelfIllumMask     # Emission (requires F_SELF_ILLUM)
TextureTranslucency      # Alpha
g_flMetalness       # Uniform metalness (no texture)
g_vColorTint        # Color tint vector
g_flSelfIllumScale  # Emission intensity
F_TRANSLUCENT       # Transparency flag
F_ALPHA_TEST        # Alpha clip flag
# Shader is "complex.vfx" (not "vr_complex")
```

**Critical Limitation:** SourceIO is import-only. No VMAT writer, no VTEX compiler, no VMDL exporter. We reference its code for format knowledge but build our own export pipeline.

**For VMAT text writing:**
```python
# WARNING: .vmat files use Valve KeyValues (VKV/VDF) format, NOT KV3.
# The keyvalues3 PyPI package is for KV3 format (different encoding).
# For .vmat generation, use the vdf PyPI package or a custom writer:

import vdf  # PyPI: vdf (MIT license, handles Valve KeyValues format)

def blender_material_to_vmat(mat, texture_export_path):
    layer0 = {
        "shader": "complex.vfx",
        "F_SPECULAR": "1",
        "F_METALNESS_TEXTURE": "1" if has_metalness_texture(mat) else "0",
        "TextureColor": f"materials/{texture_export_path}/color.tga",
        "TextureNormal": f"materials/{texture_export_path}/normal.tga",
        "TextureRoughness": f"materials/{texture_export_path}/rough.tga",
        "g_flMetalness": str(get_principled_value(mat, "Metallic")),
        "g_vColorTint": format_color_tint(mat),
    }
    # .vmat wraps everything in Layer0 { }
    vmat = {"Layer0": layer0}
    return vdf.dumps(vmat, pretty=True)
```

### 12.4 Four-Layer Architecture

```
Layer 1: Source 2 Format Library
  ├── SourceIO library/ (MIT) — VMAT/VTEX/VMDL readers
  ├── vdf (MIT) — VKV/VDF text writer for .vmat generation
  └── No Blender dependency — pure Python

Layer 2: Blender Level Design Tools
  ├── Face texturing operators (inspired by Anvil, clean-room reimplementation)
  ├── Auto-UV with world-space lock (depsgraph handler)
  ├── Cube Cut geometry tool
  ├── Hotspot mapping for trim sheets
  └── Grid/navigation tools

Layer 3: Bridge Sync Protocol (v4 — existing, extended)
  ├── WebSocket connection
  ├── Ownership model (two-tier hybrid)
  ├── Identity system (stable IDs, triple-storage)
  ├── Message protocol: geometry + transforms + materials + UVs + lights
  └── VMDL proxy system (read-only references)

Layer 4: Asset Pipeline
  ├── Texture export (Blender images → .tga/.png in s&box content dir)
  ├── VMAT generation (Blender materials → .vmat text files)
  ├── s&box auto-compiles .vmat → .vmat_c, textures → .vtex_c
  └── VMDL reference import via SourceIO (for proxy system)
```

### 12.5 Licensing Strategy

| Tool | License | Strategy |
|------|---------|----------|
| SourceIO `library/` | MIT | Direct dependency or vendored. Clean. |
| `vdf` (PyPI) | MIT | Direct dependency. Handles VKV/VDF format that .vmat actually uses. |
| Anvil Level Design | **GPL-3.0** | **Cannot copy code** into non-GPL bridge. Two options: |
| | | **Option A:** Fork Anvil, extend with bridge features (entire bridge becomes GPL) |
| | | **Option B:** Clean-room reimplement concepts independently (use documented behavior, not code). This is the recommended path. |

**Recommendation:** Option B. Reimplement Anvil's concepts (face painting, auto-UV, cube cut) from scratch using the documented behavior and our own code. Reference SourceIO's MIT-licensed parameter mappings directly. This keeps the bridge MIT-compatible.

**CORRECTION: .vmat files use Valve KeyValues (VKV/VDF) format, not KV3. The `keyvalues3` package is for a different format. Use `vdf` (PyPI, MIT) instead.**

### 12.6 Data Flow: Material Round-Trip (Highest Priority Integration)

```
Designer paints material on face in Blender (Alt+LMB)
  → auto-UV assigns world-space-consistent UVs
  → bridge depsgraph handler detects change
  → exports textures to s&box content dir (PNG/TGA)
  → generates .vmat text file (Layer 1: keyvalues3)
  → pushes geometry + UV + material assignment over WebSocket
  → s&box addon applies to ModelBuilder mesh / PolygonMesh
  → s&box AssetSystem auto-compiles .vmat → .vmat_c
  → material appears in-engine (no manual step)
```

This is the **single highest-impact feature** for v4. Once designers can paint textures in Blender and see them in s&box within seconds, the bridge becomes indispensable.

### 12.6b Data Flow: s&box → Blender Mass Texturing (Reverse Direction)

For mass texturing, the **s&box → Blender** direction is often more practical because s&box has a large built-in material library across mounted packages:

```
Designer picks a vmat in s&box's material browser, applies to faces
  → bridge C# side detects material assignment change on PolygonMesh
  → pushes material_sync message: { face_id, vmat_path }
  → Blender receives, SourceIO's ContentManager resolves the .vmat_c path
  → VTEX decoder extracts textures from compiled .vtex_c files
  → Blender creates Principled BSDF material with actual textures
  → Face shows the real material in Blender's viewport
```

**Material enumeration:** s&box C# side uses `ResourceLibrary.GetAll<Material>()` to enumerate all available materials across mounted packages. Push the full list to Blender's UI as a searchable picker. No manual importing needed.

### 12.6c Optional Dependency Architecture

Anvil and SourceIO are **optional** pre-installed addons. The bridge works without them but gains features when they're present:

```python
def check_optional_deps():
    deps = {}
    try:
        from SourceIO.library.source2 import ...
        deps["sourceio"] = True
    except ImportError:
        deps["sourceio"] = False
    try:
        import anvil_level_design
        deps["anvil"] = True
    except ImportError:
        deps["anvil"] = False
    return deps
```

**Feature gating:**

| Dependency | Without It | With It |
|-----------|-----------|---------|
| **SourceIO** | VMDL proxy disabled, no VTEX previews, material browser shows paths only | Full VMDL import, texture thumbnails, ContentManager asset scanning |
| **Anvil** | Basic Blender material assignment, no UV Lock, no Cube Cut | Face painting, auto-UV, world-space UV Lock, Cube Cut, hotspot mapping |
| **Neither** | Core bridge still works: geometry + transform + light sync | — |

**UI surface:**
```
── Optional Integrations ──
✅ SourceIO detected — VMDL import enabled
⚠ Anvil not found — Install for face painting tools
   [How to install Anvil]
```

### 12.7 Integration with Bridge Systems

#### P0: Unified Depsgraph Handler (Single Handler, Phase Pipeline)

**Problem:** Blender's `depsgraph_update_post` does not guarantee callback execution order. Two independent handlers (bridge sync + auto-UV) will fight — if bridge fires first, it sends stale UVs before auto-UV corrects them. If auto-UV fires first and modifies UVs, bridge sees a second change and double-sends.

**Solution:** ONE registered handler with an internal phase pipeline:

```python
_in_handler = False  # reentrance guard

def unified_depsgraph_handler(scene, depsgraph):
    global _in_handler
    if _in_handler:
        return  # prevent re-entry from auto-UV modifications
    _in_handler = True
    try:
        changed_objects = detect_changes(depsgraph)
        
        # Phase 1: Pre-sync processing (auto-UV, hotspot mapping)
        for obj in changed_objects:
            if obj.get("sbox_bridge_ownership") == "SBOX":
                continue  # skip SBOX-owned objects
            if needs_auto_uv(obj):
                run_auto_uv(obj, depsgraph)  # must use bmesh, not operators
        
        # Phase 2: Read FINAL state and sync to s&box
        for obj in changed_objects:
            if should_sync(obj):
                sync_to_sbox(obj)  # now includes corrected UVs
    finally:
        _in_handler = False
```

**Critical constraints:**
- Auto-UV in Phase 1 must use `bmesh` operations writing directly to `mesh.loops[].uv`, NOT operators that trigger depsgraph re-evaluation
- The reentrance guard prevents infinite loops
- Use `depsgraph.updates` iterator for only changed IDs (not full scene scan)
- Gate expensive work behind `depsgraph.id_type_updated('MESH')` checks

**Concrete bmesh pattern for auto-UV inside depsgraph handler:**

```python
def run_auto_uv(obj, depsgraph):
    """Write UVs directly to mesh data without triggering depsgraph re-entry.
    
    Key: operate on obj.data (the original mesh), NOT on the evaluated copy.
    depsgraph.objects[name].evaluated_get(depsgraph) gives a READ-ONLY evaluated copy.
    We need the ORIGINAL mesh to write UVs back.
    """
    mesh = obj.data  # Original, writable mesh — NOT evaluated
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return
    
    # bmesh approach: create from original mesh, modify, write back
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    
    uv_lay = bm.loops.layers.uv.active
    if uv_lay is None:
        bm.free()
        return
    
    # Apply world-space UV projection to changed faces
    for face in bm.faces:
        if face.select or is_recently_changed(face):
            for loop in face.loops:
                # World-space planar projection
                co = obj.matrix_world @ loop.vert.co
                loop[uv_lay].uv = compute_world_uv(co, face.normal)
    
    # Write back to mesh — this DOES trigger a depsgraph update,
    # but our reentrance guard (_in_handler) prevents infinite loops
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()  # Ensure normals/tangents recalculated
```

**WARNING: `bm.to_mesh()` WILL trigger a depsgraph update.** This is why the `_in_handler` reentrance guard is critical. Without it, the handler re-enters infinitely. The guard ensures the write-back is a no-op on re-entry.

**Do NOT use `bmesh.from_object(obj, depsgraph)`** — that gives you the evaluated (read-only) mesh. Use `bmesh.new()` + `bm.from_mesh(obj.data)` for write access.

#### Preventing Auto-UV Re-entry (Per-Object Write Sequence)

The `_in_handler` flag prevents re-entry within the SAME depsgraph evaluation. But `bm.to_mesh()` schedules a NEW depsgraph evaluation for the next frame. When that fires, `_in_handler` is False and the handler sees the mesh as dirty again.

**Solution: per-object UV write counter.**

```python
_uv_write_seq: dict[str, int] = {}  # stable_id → last UV write seq
_mesh_change_seq: dict[str, int] = {}  # stable_id → last user-edit seq

def unified_depsgraph_handler(scene, depsgraph):
    global _in_handler
    if _in_handler:
        return
    _in_handler = True
    try:
        for update in depsgraph.updates:
            obj = update.id
            if not hasattr(obj, 'get') or not obj.get('sbox_bridge_stable'):
                continue
            sid = obj['sbox_bridge_stable']
            
            if update.is_updated_geometry:
                # Increment mesh change seq
                _mesh_change_seq[sid] = _mesh_change_seq.get(sid, 0) + 1
                current_seq = _mesh_change_seq[sid]
                
                # Phase 1: Auto-UV — but ONLY if this change wasn't caused by our own UV write
                last_uv_write = _uv_write_seq.get(sid, -1)
                if current_seq > last_uv_write and needs_auto_uv(obj):
                    run_auto_uv(obj, depsgraph)
                    # Record that we just wrote UVs at this seq
                    _uv_write_seq[sid] = _mesh_change_seq.get(sid, 0) + 1
                    # bm.to_mesh() will increment _mesh_change_seq on next frame,
                    # but _uv_write_seq will match, so we skip auto-UV
                
                # Phase 2: Sync to s&box (always, with final mesh state)
                if should_sync(obj):
                    sync_to_sbox(obj)
            
            elif update.is_updated_transform:
                send_transform(obj)
    finally:
        _in_handler = False
```

**How it works:** When auto-UV writes via `bm.to_mesh()`, it stamps `_uv_write_seq[sid]` to the current change sequence + 1. On the next depsgraph fire (caused by `bm.to_mesh()`), `_mesh_change_seq` increments but now equals `_uv_write_seq` — so auto-UV is skipped. Only a genuine user edit (which increments `_mesh_change_seq` beyond `_uv_write_seq`) triggers auto-UV again.

#### Modal Operator Sync Suppression

While a modal operator (Cube Cut, knife tool, etc.) is running, the bridge should NOT sync intermediate geometry states.

```python
_modal_active: set[str] = set()  # stable_ids of objects being modally edited

class MESH_OT_cube_cut(bpy.types.Operator):
    def invoke(self, context, event):
        sid = context.active_object.get('sbox_bridge_stable')
        if sid:
            _modal_active.add(sid)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        if event.type in {'LEFTMOUSE', 'RET'}:
            # Operation complete — resume sync
            sid = context.active_object.get('sbox_bridge_stable')
            if sid:
                _modal_active.discard(sid)
            return {'FINISHED'}
        elif event.type == 'ESC':
            sid = context.active_object.get('sbox_bridge_stable')
            if sid:
                _modal_active.discard(sid)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}
```

In `unified_depsgraph_handler`, add early exit:
```python
if sid in _modal_active:
    continue  # don't sync intermediate modal states
```

#### P0: Ownership Pre-Checks in ALL Operators

Every Anvil-derived operator (Cube Cut, face paint, stretch UV, hotspot assign) must check ownership in BOTH `poll()` and `invoke()`:

```python
class MESH_OT_cube_cut(bpy.types.Operator):
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if obj.get("sbox_bridge_ownership") == "SBOX":
            cls.poll_message_set("Cannot modify s&box-owned geometry")
            return False
        if obj.get("sbox_is_proxy"):
            cls.poll_message_set("VMDL proxies are read-only")
            return False
        return True
    
    def invoke(self, context, event):
        # Double-check (ownership could change between poll and invoke)
        obj = context.active_object
        if obj.get("sbox_bridge_ownership") == "SBOX":
            self.report({'WARNING'}, "Object ownership changed — edit blocked")
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
```

`poll()` grays out the operator in menus. `invoke()` catches race conditions where ownership changed via WebSocket between poll and invoke.

#### P1: Material Sync Debounce (200ms Coalesce + Atomic Writes)

Rapid face painting (3 faces in 300ms) must NOT trigger 3 separate .vmat writes + 3 WebSocket messages:

```python
class MaterialSyncQueue:
    def __init__(self, debounce_ms=200):
        self._pending = {}  # mat_name → {assignments: [(obj, face)], needs_vmat: bool}
        self._timer = None
        self._debounce = debounce_ms / 1000.0
    
    def enqueue(self, obj, face_idx, material):
        key = material.name
        if key not in self._pending:
            self._pending[key] = {
                'material': material,
                'assignments': [],
                'needs_vmat': not vmat_exists(material),
            }
        self._pending[key]['assignments'].append((obj, face_idx))
        # Reset debounce timer
        if self._timer: self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self._flush)
        self._timer.start()
    
    def _flush(self):
        for mat_name, entry in self._pending.items():
            if entry['needs_vmat']:
                write_vmat_atomic(entry['material'])  # temp + os.replace
            bridge.ws_send({
                'type': 'material_batch_assign',
                'material': mat_name,
                'assignments': [(o.name, f) for o, f in entry['assignments']]
            })
        self._pending.clear()
```

Key: atomic file writes (temp + `os.replace`), batched WebSocket messages, deduplication for same material.

#### P1: UV Sync by Vertex-ID (Not Index)

Blender loops and s&box half-edges have different ordering. Naive index-based mapping produces rotated/flipped UVs after Cube Cut topology changes.

**Solution:** Map UVs by `(face_index, vertex_index)` pairs — topology-invariant:

```python
def build_uv_sync_data(mesh):
    uv_layer = mesh.uv_layers.active
    uv_data = {}
    for poly in mesh.polygons:
        for loop_idx in range(poly.loop_start, poly.loop_start + poly.loop_total):
            vert_idx = mesh.loops[loop_idx].vertex_index
            uv = uv_layer.data[loop_idx].uv
            uv_data[(poly.index, vert_idx)] = (uv.x, uv.y)
    return uv_data
```

s&box side: iterate each face's half-edges, get vertex index, look up `(face_index, vertex_index)` in received dict. O(n) with hash lookup, ordering-immune.

#### P1: SourceIO Abstraction Layer

Don't hard-depend on SourceIO. Wrap it behind an interface with native fallback:

```python
class FormatBackend:
    def read_vmat(self, path) -> dict: ...
    def read_vtex(self, path) -> 'Image': ...
    def read_vmdl(self, path) -> dict: ...

class SourceIOBackend(FormatBackend):
    """Uses SourceIO when available."""
    def __init__(self):
        try:
            from SourceIO.library.source2 import ...
            self._available = True
        except ImportError:
            self._available = False

class NativeBackend(FormatBackend):
    """Minimal built-in parser for .vmat (text KV3) and basic .vtex.
    Does NOT cover .vmdl (complex binary, requires SourceIO)."""
    pass

def get_backend():
    sio = SourceIOBackend()
    return sio if sio.available else NativeBackend()
```

Pin version: `SourceIO >= 5.5.0, < 6.0.0`. Startup check logs warning on unknown versions.

#### P1: ctypes UV Lock — Version Table + Fallback

The "Correct Face Attributes" ctypes hack needs per-version memory offsets:

```python
class CorrectFaceAttribsOverride:
    _OFFSETS = {
        (4, 0): 0x___,  # populate from testing
        (4, 1): 0x___,
        (4, 2): 0x___,
    }
    
    def __enter__(self):
        version = bpy.app.version[:2]
        self._offset = self._OFFSETS.get(version)
        if self._offset is None:
            log.warning(f"Blender {version} not verified for UV Lock — using fallback")
            return self  # no-op, fallback mode
        # ... ctypes patch
    
    def __exit__(self, *exc):
        # ... restore original value
```

On unknown Blender versions: refuse to patch, fall back to pure-Python UV correction (slower but safe). Add startup self-test that validates the offset.

**Other integration points (unchanged):**

**SourceIO VMDL Import + VMDL Proxy System:**
- SourceIO's VMDL importer becomes the implementation for Section 5 (VMDL Proxy System)
- Import via SourceIO → tag as proxy → apply all Layer 2-5 safeguards from Section 5
- SourceIO handles the mesh/armature/material extraction
- Bridge handles the safety (copy-not-original, edit prevention, transform sync)

**SourceIO ContentManager + Material Browser:**
- ContentManager scans s&box project addons for available materials/models
- Powers the Blender-side material browser panel (Section 9.3)
- VTEX decoder provides thumbnail previews without full texture import

**Hotspot Mapping + Bridge Sync:**
- Hotspot definitions stored alongside bridge project data
- When hotspot auto-assigns a face to a trim sheet region, bridge syncs the UV + material
- `hotspots.json` versioned in project for team consistency

### 12.8 Integration Risk Priority Matrix

| Risk | Severity | Priority | Mitigation |
|------|----------|----------|-----------|
| Depsgraph handler ordering | Critical | **P0** | Single unified handler with phase pipeline |
| Cube Cut on SBOX-owned object | High | **P0** | `poll()` + `invoke()` ownership checks |
| Material paint race condition | High | **P1** | 200ms debounce + atomic writes + batched WS messages |
| ctypes memory hack fragility | High | **P1** | Version offset table + pure-Python fallback |
| UV index mapping mismatch | Medium | **P1** | Vertex-ID-based mapping, not loop index |
| SourceIO API instability | Medium | **P2** | Abstraction layer + native fallback for critical formats |
| Clean-room face change detection | Medium | **P2** | Ship with "manual UV mode" escape hatch, log all auto-UV ops |
| WebSocket backpressure at scale | Medium | **P2** | Send queue with max size, coalesce pending messages per object |

### 12.9 What This Enables vs. What Remains

| Capability | Status After Integration |
|-----------|------------------------|
| Geometry sync | Existing + Cube Cut clean topology |
| Transform sync | Existing |
| Light sync | Existing |
| Material sync | **NEW** — Auto-generate .vmat, face-level assignment |
| UV workflow | **NEW** — Auto-UV, UV Lock, face painting, hotspot mapping |
| Texture pipeline | **NEW** — Auto-export to s&box content dir, VTEX previews |
| Asset import | **NEW** — VMDL/VMAT/VTEX via SourceIO |
| Material browser | **NEW** — ContentManager scans s&box for available materials |
| Trim sheet workflow | **NEW** — Hotspot mapping with orientation-aware assignment |
| Geometry editing tools | **NEW** — Cube Cut, grid snapping |

**Remaining gaps (post-integration):**
- Entity/gameplay object placement (prefab browser + instance system)
- Collision mesh auto-generation from level geometry
- Blender ↔ s&box node graph translation (only parameter-level, not full procedural)
- Spline/path system (s&box has no native splines — custom implementation needed)

---

## 13. Migration Guide (v3 to v4)

### 12.1 Breaking Changes

| v3 | v4 | Migration |
|----|-----|-----------|
| HTTP connection | WebSocket | Server code rewrite required |
| `sbox_bridge_id` (single property) | Triple-storage stable ID | Auto-migrate on first v4 connect |
| No ownership model | Two-tier hybrid | All existing objects start as UNLOCKED |
| Embedded material metadata | Sidecar `.blend_meta` files | Regenerate on first material export |
| `sync.py` monolith | Module split | Complete code restructure |

### 12.2 Auto-Migration

On first v4 connection with a scene that has v3 bridge objects:

1. Scan for `sbox_bridge_id` properties
2. Generate stable IDs from existing IDs + creation timestamp
3. Write to triple-storage
4. Set ownership: shape=UNLOCKED, channels=BIDIRECTIONAL (safe default)
5. Show migration report to user

---

## 14. Known Limitations & Trade-offs

1. **Geometry is non-mergeable.** If both sides edit a mesh, one side's changes are lost. The ownership model prevents this from happening silently, but it cannot merge vertices.

2. **Procedural materials cannot be reconstructed.** Baking is one-way. Reverse sync rebuilds Principled BSDF with flat values, not the original node tree.

3. **Source IO is a hard dependency for VMDL proxies.** Without it, no vmdl import. The bridge should detect if Source IO is installed and disable vmdl features gracefully if not.

4. **s&box shader names may change.** The shader registry (JSON config) must be verified against each s&box update. If shaders are renamed, the registry must be updated.

5. **Normal map convention: RESOLVED.** Both Blender and s&box (Source 2) use OpenGL convention (+Y up). No green channel flip needed. Third-party DX normal maps can use vtex's "Legacy Source 1 inverted normals" setting.

6. **Displacement has no engine support.** s&box cannot tessellate. Displacement maps are metadata-only or baked-to-normal (lossy).

7. **Transform sync during disconnection.** For VMDL proxies, s&box wins on reconnect. For owned geometry, the owning side wins. This is a design choice, not a limitation — but users must understand it.

---

## 15. Open Questions

### Must Resolve Before Implementation

- [x] **~~Verify s&box shader names~~**: RESOLVED — s&box uses `complex.vfx`, NOT `vr_complex.vfx`. Feature flags: `F_SPECULAR`, `F_METALNESS_TEXTURE`, `F_SELF_ILLUM`, `F_TRANSLUCENT`, `F_ALPHA_TEST`.
- [x] **~~Verify material API~~**: RESOLVED — `Material.Create("name", "complex.vfx")` + `Material.Set()` + `Material.SetFeature()` confirmed.
- [x] **~~Performance for bulk import~~**: RESOLVED — `Scene.BatchGroup()` defers OnEnable callbacks. Must use.
- [x] **~~Auto-reload mechanism~~**: RESOLVED — AssetSystem monitors file CRCs, auto-recompiles on disk write.
- [x] **~~Verify normal map convention~~**: RESOLVED — Source 2 uses **OpenGL convention (+Y up)**, NOT DirectX. Blender also uses OpenGL. **No green channel flip needed.** The "Legacy Source 1 inverted normals" option in vtex settings handles DX normals from third-party sources. Confirmed via Valve Developer Community.
- [x] **~~Roughness parameter semantics~~**: RESOLVED — `g_flRoughnessScaleFactor` is a multiplier on `TextureRoughness`. For uniform roughness without a texture, generate a 1x1 constant-color PNG at the roughness value, reference it as `TextureRoughness`, set scale=1.0. This is the standard community approach. No absolute roughness param exists.
- [x] **~~Source IO API~~**: RESOLVED — `bpy.ops.sourceio.vmdl(filepath="path/to/model.vmdl_c")`. Accepts: `filepath`, `import_materials` (True), `import_physics` (False), `import_attachments` (False), `discover_resources` (True), `scale` (0.01905 = Hammer units to meters), `lod_mask` (0xFFFF). Note: expects `.vmdl_c` (compiled), not raw `.vmdl`. Installed at `AppData\Roaming\Blender Foundation\Blender\5.x\scripts\addons\SourceIO-master\`. Creates a new collection per model.
- [x] **~~s&box WebSocket support~~**: RESOLVED — YES. Existing `HttpListener` in `BlenderBridgeServer.cs` supports WebSocket upgrade natively via `context.Request.IsWebSocketRequest` → `context.AcceptWebSocketAsync(null)`. Same port 8099, no new listener needed. `System.Net.WebSockets` confirmed available unsandboxed in editor context. Replace `_outbox` poll queue with `ConcurrentDictionary<string, WebSocket>` of connected clients + direct `SendAsync` pushes.
- [x] **~~UV data in bridge protocol~~**: RESOLVED — Flat float array with implicit indexing. Both sides iterate faces/half-edges in canonical order. UV data = `[uint32 count][float32 u0, v0, u1, v1, ...]`. For 10K faces (40K UVs): ~320KB raw floats. Sent as `WebSocketMessageType.Binary` frame. No explicit face/vert index mapping needed — array position = iteration order. 60% smaller than JSON, fastest to marshal.
- [ ] **Team workflow**: Session identity design complete (P0-B), but needs implementation planning. `ConcurrentDictionary<string, WebSocket>` supports multiple connections. Each client identified by session user_id from handshake. Needs engine testing for ownership arbitration under concurrent edits.
- [x] **~~SSS shader support~~**: RESOLVED — Dedicated `skin.shader` (NOT `complex.vfx`). SSS is always-on in skin shader, controlled by: `g_flCurvatureScale`, `g_flTransmissionFalloff`, `g_vTransmissionColor`, `TextureTransmissiveMask`, `g_vAmbientNormalSoftness` (per-channel), `g_flDiffuseNormalBlur`. No `F_SSS` feature flag exists. Map Blender SSS weight > 0 → `skin.shader`.
- [x] **~~Hammer vs Scene target~~**: RESOLVED — Support both. Hammer is NOT being removed imminently; s&box actively supports both. Per-object flag: `sbox_bridge_target: "hammer" | "scene"`. Scene tier preferred for new projects (ModelBuilder + ModelRenderer). Hammer tier (PolygonMesh/MapMesh) kept for legacy. New features (hotspot, instancing) should be Scene-first.

### Must Resolve Before Implementation (UX-Critical)

- [ ] **Undo registry**: Implement external ID registry outside Blender's undo stack (`undo_post` handler)
- [ ] **Session identity**: Per-user ownership with `user_id` in addon preferences for multi-artist workflows
- [ ] **Post-sync report**: Inline panel showing per-object actions after every pull/push
- [ ] **Proxy modifier blocking**: Proactive depsgraph-based blocking (not 2s polling). Watchdog is fallback only.
- [ ] **Reconnect conflict dialog**: When proxy diverged while disconnected, show dialog instead of silent snap
- [ ] **Material staleness diff**: Show field-level diff (roughness 0.3->0.7, texture changed) not just "stale"
- [ ] **Onboarding flow**: First-run inline guide in panel, rich tooltips on ownership icons
- [ ] **Remote transform notifications**: Toast notification when s&box moves objects ("3 objects updated by s&box")

### Nice-to-Have (Post-MVP)

- [ ] Material preview thumbnails in Blender panel (bpy.utils.previews)
- [ ] Texture grid picker (template_icon_view)
- [ ] Rig preservation for vmdl variant workflow (edit mesh, recompile as variant)
- [ ] Collision auto-generation from vmdl physics data
- [ ] Edge smoothing sync (per-edge creasing)
- [ ] Material export grouped by tier with "Changed Only" default filter
- [ ] Material dry-run visual preview (not just text report)
- [ ] Pre-send lock checking for team workflows (gray out Send for others' objects)

---

## 16. Testing Strategy

### 16.1 Unit Tests (Pure Python, No Blender)

| Component | Test Target | Method |
|-----------|------------|--------|
| Ownership state machine | All transitions, UNLOCKED race, claim/reject | `pytest` with mocked server responses |
| Echo suppression (seq/echo_of) | Rapid updates, out-of-order delivery | Direct function calls with synthetic messages |
| Stable ID generation | Uniqueness, determinism, collision resistance | Generate 10K IDs, assert no duplicates |
| Material parameter mapping | Every Blender → .vmat conversion | Input Principled BSDF values, assert correct .vmat output |
| UV vertex-ID keying | Round-trip: build dict → serialize → deserialize → compare | Synthetic mesh data, verify exact UV recovery |

### 16.2 Integration Tests (Blender + Mock Server)

| Scenario | Setup | Assertion |
|----------|-------|-----------|
| Undo recovery | Create object, assign bridge ID, undo 5x, redo 3x | External registry re-injects ID correctly |
| Depsgraph handler ordering | Modify mesh, check auto-UV fires before sync | Sync message contains corrected UVs, not stale |
| VMDL proxy edit prevention | Import proxy, enter Edit Mode | Force-exit within 1 frame, mesh hash unchanged |
| Material debounce | Paint 5 faces in 200ms | Exactly 1 WebSocket message sent |
| Ownership blocking | Set object SBOX-owned, attempt Cube Cut | Operator returns CANCELLED, mesh unchanged |

### 16.3 Mock WebSocket Server (for C# testing in isolation)

```python
# test_server.py — standalone mock that mimics bridge protocol
import asyncio, websockets, json

async def mock_bridge(ws, path):
    hello = json.loads(await ws.recv())
    assert hello["type"] == "hello"
    
    await ws.send(json.dumps({
        "type": "hello_ack",
        "session_id": "test_session",
        "full_state": []
    }))
    
    async for msg in ws:
        data = json.loads(msg) if isinstance(msg, str) else None
        if data and data["type"] == "mesh_update":
            # Echo back ack
            await ws.send(json.dumps({
                "type": "mesh_ack",
                "stable_id": data.get("stable_id"),
                "version": 1
            }))

asyncio.run(websockets.serve(mock_bridge, "localhost", 8099))
```

### 16.4 What Must Pass Before Shipping

- [ ] All ownership transitions: 100% branch coverage on state machine
- [ ] Undo recovery: 15 undos past bridge ID assignment, ID survives
- [ ] VMDL proxy: 0 geometry bytes sent for proxy objects across 1000 sync cycles
- [ ] Material round-trip: Blender → .vmat → s&box material → .vmat read-back matches within epsilon
- [ ] UV preservation: Round-trip UV data through wire protocol, max error < 1e-5
- [ ] Concurrent claims: 2 clients claim UNLOCKED object simultaneously, exactly 1 succeeds

---

## Appendix A: Coordinate Conversion

```python
# Scale conversion: Blender uses meters, s&box uses inches (1 inch = 1 unit)
METERS_TO_SBOX = 39.3701  # 1 meter = 39.3701 inches

def blender_to_sbox_pos(bx, by, bz):
    """Convert Blender position (meters, Z-up, -Y forward) to s&box (inches, Z-up, Y forward)."""
    return (by * METERS_TO_SBOX, -bx * METERS_TO_SBOX, bz * METERS_TO_SBOX)

def sbox_to_blender_pos(sx, sy, sz):
    """Convert s&box position (inches) to Blender (meters)."""
    return (-sy / METERS_TO_SBOX, sx / METERS_TO_SBOX, sz / METERS_TO_SBOX)

def blender_to_sbox_scale(bsx, bsy, bsz):
    """Scale is unitless ratio — no conversion needed for uniform scale.
    For geometry: apply object scale to mesh data BEFORE sending (bpy.ops.object.transform_apply).
    Non-uniform object scale is applied to vertex positions, not sent as a separate scale."""
    return (bsy, bsx, bsz)  # axis swap only, no unit conversion
```

> **Unit convention**: s&box uses inches (1 unit = 1 inch). Blender uses meters. The bridge applies METERS_TO_SBOX (39.3701) to all position data. Geometry vertex positions are converted at extraction time. Non-uniform object scale MUST be applied to mesh data before sending (`bpy.ops.object.transform_apply(scale=True)`) — the bridge never sends raw object scale for geometry objects (only for VMDL proxies where scale is a component property).

## Appendix B: File Layout After v4

```
~/.cache/sbox_bridge/
  vmdls/
    {session_uuid}/
      manifest.json
      manifest.json.bak
      citizen_male.vmdl        # COPY, never original
      citizen_male.vmdl.meta   # Sidecar with original path + checksum
      citizen_male.vmdl.lock   # Advisory lock with PID

project/assets/materials/blender_bridge/
  chrome_panel.vmat
  chrome_panel.vmat.blend_meta  # Sidecar metadata
  textures/
    chrome_panel_color.png
    chrome_panel_normal.png     # OpenGL convention (NO flip — both Blender and s&box use +Y up)

project/.sbox_bridge_cache/
  {bridgeId}.meshcache          # Binary geometry cache (existing v3 format)
```

## Appendix C: Message Types (v4 Wire Protocol)

| Type | Direction | Payload | Binary? |
|------|-----------|---------|---------|
| `create` | Blender -> s&box | JSON: name, ownership, parent | No |
| `create_ack` | s&box -> Blender | JSON: stable_id, sbox_object_id | No |
| `mesh_update` | Either | Binary: header + verts + faces + UVs (vertex-ID keyed, see Section 8.4) | Yes |
| `mesh_ack` | Either | JSON: stable_id, version, hash | No |
| `mesh_nack` | s&box -> Blender | JSON: stable_id, error_code, detail | No |
| `transform` | Either | Binary: 12 floats (pos + rot + scale) | Yes |
| `material_sync` | Either | JSON: material def + texture paths | No |
| `ownership_change` | Either | JSON: stable_id, new_state | No |
| `ownership_request` | Either | JSON: stable_id, requester | No |
| `ownership_response` | Either | JSON: stable_id, granted (bool), new_owner | No |
| `delete` | Either | JSON: stable_id | No |
| `reparent` | Either | JSON: stable_id, parent_id (null = unparent) | No |
| `sync_request` | Either | JSON: known_ids + versions | No |
| `sync_response` | Either | JSON: full state for reconciliation | No |
| `vmdl_reference` | s&box -> Blender | JSON: vmdl path, object_id, transform | No |
| `heartbeat` | Both | Empty | No |

**`mesh_nack` error codes:** `OWNERSHIP_DENIED`, `MALFORMED_DATA`, `OBJECT_DELETED`, `INTERNAL_ERROR`. Blender shows the error in the activity log and marks the object with a red warning badge.
