"""
s&box Bridge v3 - Bidirectional scene sync between Blender and s&box.
Features sync direction controls, hierarchy mapping, status indicators, and geometry hashing.
"""

bl_info = {
    "name": "s&box Bridge",
    "author": "SanicTehHedgehog",
    "version": (3, 7, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > s&box",
    "description": "Bidirectional scene sync with s&box game engine",
    "category": "Scene",
}

import bpy
from . import connection
from . import sync
from . import panel


def _apply_overlay_grid_scale(grid_size, scale_factor):
    """Set every 3D viewport's overlay grid to grid_size / scale_factor Blender
    units per cell (with the default 16/16, one Blender unit per cell).

    Walks bpy.data.screens rather than bpy.context.screen: grid changes pushed
    by s&box are applied from the poll timer, where bpy.context has no window
    and context.screen is None — the data API works from any context and keeps
    every workspace's viewports consistent."""
    try:
        sf = max(scale_factor, 1e-6)
        display_scale = float(grid_size) / sf
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.overlay.grid_scale = display_scale
                area.tag_redraw()
    except Exception:
        pass


def _on_grid_size_changed(self, context):
    """grid_size changed in Blender. Push to s&box (mirrors into the editor's
    grid spacing) and update Blender's viewport overlay so the visual grid
    matches what the bridge is reporting to the engine."""
    try:
        sync.send_grid_changed(self.grid_size)
    except Exception as e:
        print(f"[Bridge] grid_size send error: {e}")
    _apply_overlay_grid_scale(self.grid_size, self.scale_factor)


# Handle of the currently-scheduled deferred resync, so a slider drag
# (which fires the update callback per mouse step) rearms ONE timer instead
# of queuing a full-scene resync per step.
_scale_resync_pending = None


def _on_scale_factor_changed(self, context):
    """Project scale_factor changed. The factor is baked into vertex positions
    on the wire, so every existing bridge object now has stale geometry on the
    s&box side. Clear all stored geometry hashes and queue a deferred force-
    resync of every bridge object. Debounced 0.75s: only the final value after
    a slider drag triggers the (expensive) full-scene resync. Deferred via
    timer because property-update callbacks run mid-write — we can't safely
    walk bpy.data inline."""
    global _scale_resync_pending

    def deferred():
        global _scale_resync_pending
        _scale_resync_pending = None
        if not connection.is_connected():
            return None
        for obj in bpy.data.objects:
            if "sbox_bridge_hash" in obj:
                del obj["sbox_bridge_hash"]
            if obj.data is not None and "sbox_bridge_hash" in obj.data:
                del obj.data["sbox_bridge_hash"]
        count = 0
        for obj in list(bpy.data.objects):
            if obj.type == "MESH" and not sync._should_skip_object(obj):
                if sync.get_bridge_id(obj):
                    sync.send_update_mesh(obj)
                    count += 1
            elif obj.type == "LIGHT" and sync.get_bridge_id(obj):
                sync.send_update_light(obj)
                count += 1
        try:
            sf = bpy.context.scene.sbox_bridge.scale_factor
        except Exception:
            sf = 0.0
        sync.log_activity(f"Project scale -> {sf:.2f}, resynced {count} bridge object(s)")
        return None

    # Rearm: cancel any previously-scheduled resync, keep only the latest.
    try:
        if _scale_resync_pending is not None and bpy.app.timers.is_registered(_scale_resync_pending):
            bpy.app.timers.unregister(_scale_resync_pending)
    except Exception:
        pass
    _scale_resync_pending = deferred
    bpy.app.timers.register(deferred, first_interval=0.75)
    # Overlay cell size is grid_size / scale_factor, so a scale change moves
    # the visual grid too.
    _apply_overlay_grid_scale(self.grid_size, self.scale_factor)


# ── Addon Properties ───────────────────────────────────────────────���──────

class SboxBridgeSettings(bpy.types.PropertyGroup):
    host: bpy.props.StringProperty(
        name="Host", default="localhost",
        description="s&box bridge server hostname",
    )
    port: bpy.props.IntProperty(
        name="Port", default=8099, min=1024, max=65535,
        description="s&box bridge server port",
    )
    is_connected: bpy.props.BoolProperty(name="Connected", default=False)
    scale_factor: bpy.props.FloatProperty(
        name="Scale Factor", default=40.0, min=0.001, max=10000.0,
        description=(
            "Blender unit to s&box source-unit multiplier. Default 40: near-"
            "real scale (1.6% off true inches) where a 2 m wall is 80 units "
            "next to the 72-unit player, and with grid 40 one grid cell is "
            "exactly 1 m. Changing this re-bakes every bridged mesh's "
            "vertices and triggers a full resync."
        ),
        update=_on_scale_factor_changed,
    )
    auto_sync: bpy.props.BoolProperty(
        name="Auto Sync", default=True,
        description="Automatically sync scene changes",
    )
    sync_on_connect: bpy.props.BoolProperty(
        name="Sync on Connect", default=False,
        description=(
            "Request a full scene sync automatically on Connect and on engine "
            "restart. Off: connect is instant — press Sync All when you want "
            "to reconcile the two scenes"
        ),
    )
    project_assets_path: bpy.props.StringProperty(
        name="Assets Path", default="", subtype='DIR_PATH',
        description="Path to the s&box project's Assets folder (for material/texture export)",
    )
    citizen_fbx_path: bpy.props.StringProperty(
        name="Citizen FBX", default="", subtype='FILE_PATH',
        description=(
            "Optional path to a Citizen model FBX for the Player Reference "
            "button (s&box does not ship one — export your own or leave "
            "empty). Empty: auto-checks the addon's assets folder and "
            "Documents/Blender/Citizen.fbx, else uses a wireframe box"
        ),
    )
    grid_size: bpy.props.IntProperty(
        name="Grid Size", default=40, min=1, max=256,
        description=(
            "Bridge grid size in s&box source units. Mirrored to the "
            "s&box editor's grid spacing and to Blender's viewport "
            "overlay grid (display scale = grid_size / scale_factor). "
            "Use [ / ] hotkeys to halve / double."
        ),
        update=_on_grid_size_changed,
    )
    auto_reconnect: bpy.props.BoolProperty(
        name="Auto Reconnect", default=True,
        description="Automatically reconnect on connection loss",
    )
    reconnect_interval: bpy.props.FloatProperty(
        name="Reconnect Interval", default=3.0, min=1.0, max=30.0,
        description="Base interval between reconnect attempts (seconds)",
    )
    sync_mode: bpy.props.EnumProperty(
        name="Sync Mode",
        items=[
            ('BIDIRECTIONAL', "Bidirectional", "Full two-way sync (default)"),
            ('EXPORT_ONLY', "Export Only", "Blender to s&box only. s&box never overwrites Blender mesh data"),
            ('MANUAL', "Manual Only", "No auto-sync. Use Send to Scene / Sync All explicitly"),
        ],
        default='BIDIRECTIONAL',
        description="Controls how data flows between Blender and s&box",
    )
    show_activity_log: bpy.props.BoolProperty(
        name="Activity Log", default=False,
        description="Show the activity log",
    )


# ── Registration ──────────────────────────────────────────────────────────

classes = (
    SboxBridgeSettings,
    panel.SBOX_OT_Connect,
    panel.SBOX_OT_Disconnect,
    panel.SBOX_OT_ClearWarnings,
    panel.SBOX_OT_ForceResync,
    panel.SBOX_OT_DeleteBridgeMaterial,
    panel.SBOX_OT_OpenBridgeMaterialFolder,
    panel.SBOX_OT_SetGridSize,
    panel.SBOX_OT_HalveGrid,
    panel.SBOX_OT_DoubleGrid,
    panel.SBOX_OT_SyncAll,
    panel.SBOX_OT_SendToScene,
    panel.SBOX_OT_ApplyMaterial,
    panel.SBOX_OT_RemoveFromScene,
    panel.SBOX_OT_ClearBridgeID,
    panel.SBOX_OT_SendChildren,
    panel.SBOX_OT_SelectBridgeObject,
    panel.SBOX_OT_ConfirmPendingDeletes,
    panel.SBOX_OT_CancelPendingDeletes,
    panel.SBOX_OT_ConfirmInboundDeletes,
    panel.SBOX_OT_RestoreInboundDeletes,
    panel.SBOX_OT_ScalePreset,
    panel.SBOX_OT_AlignSnap,
    panel.SBOX_OT_AddPlayerReference,
    panel.SBOX_PT_BridgePanel,
)


_keymaps = []


def _register_keymaps():
    """Bind '[' / ']' to halve / double grid in the 3D View. Keymaps are attached
    to the user's keyconfig (addon section) so the bindings unregister cleanly."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return
    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
    kmi = km.keymap_items.new('sbox.bridge_halve_grid', 'LEFT_BRACKET', 'PRESS')
    _keymaps.append((km, kmi))
    kmi = km.keymap_items.new('sbox.bridge_double_grid', 'RIGHT_BRACKET', 'PRESS')
    _keymaps.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sbox_bridge = bpy.props.PointerProperty(type=SboxBridgeSettings)
    if sync.on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(sync.on_depsgraph_update)
    if sync.on_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(sync.on_undo_post)
    if sync.on_undo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(sync.on_undo_post)
    _register_keymaps()
    print("[s&box Bridge v2] Addon registered.")


def unregister():
    connection.disconnect()
    sync.stop_timer()
    _unregister_keymaps()
    try:
        panel.clear_material_previews()
    except Exception:
        pass
    if sync.on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync.on_depsgraph_update)
    if sync.on_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(sync.on_undo_post)
    if sync.on_undo_post in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(sync.on_undo_post)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "sbox_bridge"):
        del bpy.types.Scene.sbox_bridge
    print("[s&box Bridge v2] Addon unregistered.")
