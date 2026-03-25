"""
Draw call / action operations service for RenderDoc.
"""

import csv
import json
import os
import re
import struct
import threading
import time
import traceback

import renderdoc as rd

from ..utils import Serializers, Helpers


class ActionService:
    """Draw call / action operations service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn
        self._export_jobs = {}
        self._export_jobs_lock = threading.Lock()

    def _sanitize_filename(self, name):
        """Sanitize a name for use as a Windows filename."""
        if not name:
            return ""

        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()
        sanitized = sanitized.rstrip(" .")
        return sanitized

    def _resolve_mesh_output_path(self, output_path, action_name, event_id, mesh_stage):
        """Resolve the final output path for a saved mesh CSV."""
        safe_name = self._sanitize_filename(action_name)
        if not safe_name:
            safe_name = "event_%d" % event_id

        safe_stage = self._sanitize_filename(mesh_stage) or "vs_input"
        default_filename = "%s_event%d_%s.csv" % (safe_name, event_id, safe_stage)

        normalized_path = os.path.normpath(output_path)
        _, ext = os.path.splitext(normalized_path)
        has_extension = bool(ext)

        if output_path.endswith(("/", "\\")) or os.path.isdir(normalized_path) or not has_extension:
            directory = normalized_path if not has_extension else os.path.dirname(normalized_path)
            if not directory:
                directory = "."
            return os.path.join(directory, default_filename)

        return normalized_path

    def _resolve_texture_output_path(self, output_dir, texture_name, resource_id, slot_name, extension):
        """Resolve the final output path for an exported texture."""
        resource_numeric_id = int(str(resource_id).split("::")[-1])
        safe_name = self._sanitize_filename(texture_name)
        if not safe_name:
            safe_name = "texture_%d" % resource_numeric_id

        safe_slot = self._sanitize_filename(slot_name) or "texture"
        filename = "%s_%s.%s" % (safe_slot, safe_name, extension)
        return os.path.join(output_dir, filename)

    def _event_asset_status_path(self, output_dir):
        """Return the status file path for an event asset export."""
        return os.path.join(os.path.normpath(output_dir), "export_status.json")

    def _write_event_asset_status(self, output_dir, status):
        """Persist export status so external callers can observe progress."""
        export_root = os.path.normpath(output_dir)
        os.makedirs(export_root, exist_ok=True)
        status_path = self._event_asset_status_path(export_root)
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        return status_path

    def _read_event_asset_status(self, output_dir):
        """Read the persisted export status if present."""
        status_path = self._event_asset_status_path(output_dir)
        if not os.path.exists(status_path):
            return None
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_completed_status_from_manifest(self, output_dir, fallback_status=None):
        """Reconstruct a completed status from an existing manifest on disk."""
        export_root = os.path.normpath(output_dir)
        manifest_path = os.path.join(export_root, "manifest.json")
        if not os.path.exists(manifest_path):
            return None

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        mesh = manifest.get("mesh")
        textures = manifest.get("textures", [])
        fallback_status = fallback_status or {}
        completed_status = self._build_event_asset_status(
            event_id=manifest.get("event_id", fallback_status.get("event_id", 0)),
            output_dir=export_root,
            state="completed",
            include_mesh=mesh is not None if fallback_status.get("include_mesh") is None else fallback_status.get("include_mesh"),
            include_textures=bool(textures) if fallback_status.get("include_textures") is None else fallback_status.get("include_textures"),
            texture_stages=fallback_status.get("texture_stages") or ["pixel"],
            mesh_stage=(mesh or {}).get("mesh_stage", fallback_status.get("mesh_stage", "vs_input")),
            instance=manifest.get("instance", fallback_status.get("instance", 0)),
            view=manifest.get("view", fallback_status.get("view", 0)),
            texture_file_format=fallback_status.get("texture_file_format", "png"),
            status_path=self._event_asset_status_path(export_root),
            message="Export completed (reconciled from manifest).",
            result={
                "event_id": int(manifest.get("event_id", fallback_status.get("event_id", 0))),
                "action_name": manifest.get("action_name", ""),
                "output_dir": export_root,
                "manifest_path": manifest_path,
                "mesh_exported": mesh is not None,
                "texture_count": len(textures),
            },
        )
        return completed_status

    def _build_event_asset_status(
        self,
        event_id,
        output_dir,
        state,
        include_mesh,
        include_textures,
        texture_stages,
        mesh_stage,
        instance,
        view,
        texture_file_format,
        **extra
    ):
        """Build a serializable status payload for background exports."""
        payload = {
            "event_id": int(event_id),
            "output_dir": os.path.normpath(output_dir),
            "state": state,
            "include_mesh": bool(include_mesh),
            "include_textures": bool(include_textures),
            "texture_stages": list(texture_stages or ["pixel"]),
            "mesh_stage": str(mesh_stage),
            "instance": int(instance),
            "view": int(view),
            "texture_file_format": str(texture_file_format),
            "updated_at_epoch": time.time(),
        }
        payload.update(extra)
        return payload

    def _make_mesh_attribute(self, **kwargs):
        """Create a mesh attribute descriptor."""
        return kwargs

    def _get_indices(self, controller, mesh, action):
        """Decode draw indices for the requested mesh source."""
        num_indices = int(mesh["num_indices"])
        index_stride = int(mesh["index_byte_stride"])
        index_resource_id = mesh["index_resource_id"]

        if index_resource_id != rd.ResourceId.Null():
            index_format = "B"
            if index_stride == 2:
                index_format = "H"
            elif index_stride == 4:
                index_format = "I"
            else:
                raise RuntimeError("Unsupported index stride: %d" % index_stride)

            index_data = controller.GetBufferData(index_resource_id, mesh["index_byte_offset"], 0)
            index_format = str(num_indices) + index_format
            offset = int(mesh.get("index_offset", 0)) * index_stride
            indices = struct.unpack_from(index_format, index_data, offset)
            base_vertex = int(mesh.get("base_vertex", 0))
            return [int(i) + base_vertex for i in indices]

        vertex_offset = int(getattr(action, "vertexOffset", 0))
        return list(range(vertex_offset, vertex_offset + num_indices))

    def _unpack_data(self, fmt, data):
        """Decode a vertex attribute tuple from raw bytes."""
        if fmt.Special():
            raise RuntimeError("Packed formats are not supported")

        comp_type_double = getattr(rd.CompType, "Double", None)
        comp_type_uscaled = getattr(rd.CompType, "UScaled", None)
        comp_type_sscaled = getattr(rd.CompType, "SScaled", None)
        format_chars = {}
        #                                 012345678
        format_chars[rd.CompType.UInt] = "xBHxIxxxQ"
        format_chars[rd.CompType.SInt] = "xbhxixxxq"
        format_chars[rd.CompType.Float] = "xxexfxxxd"
        format_chars[rd.CompType.UNorm] = format_chars[rd.CompType.UInt]
        format_chars[rd.CompType.SNorm] = format_chars[rd.CompType.SInt]
        if comp_type_uscaled is not None:
            format_chars[comp_type_uscaled] = format_chars[rd.CompType.UInt]
        if comp_type_sscaled is not None:
            format_chars[comp_type_sscaled] = format_chars[rd.CompType.SInt]
        if comp_type_double is not None:
            format_chars[comp_type_double] = format_chars[rd.CompType.Float]

        if fmt.compType not in format_chars:
            raise RuntimeError("Unsupported component type: %s" % str(fmt.compType))

        type_chars = format_chars[fmt.compType]
        if fmt.compByteWidth >= len(type_chars) or type_chars[fmt.compByteWidth] == "x":
            raise RuntimeError(
                "Unsupported component byte width %d for %s"
                % (fmt.compByteWidth, str(fmt.compType))
            )

        vertex_format = str(fmt.compCount) + type_chars[fmt.compByteWidth]
        value = struct.unpack_from(vertex_format, data, 0)

        if fmt.compType == rd.CompType.UNorm:
            divisor = float((1 << (fmt.compByteWidth * 8)) - 1)
            value = tuple(float(v) / divisor for v in value)
        elif fmt.compType == rd.CompType.SNorm:
            max_neg = -(1 << (fmt.compByteWidth * 8 - 1))
            divisor = float(-(max_neg - 1))
            value = tuple(
                float(v) if v == max_neg else float(v) / divisor
                for v in value
            )

        bgra_order = False
        if hasattr(fmt, "BGRAOrder"):
            bgra_order = fmt.BGRAOrder()
        elif hasattr(fmt, "bgraOrder"):
            bgra_order = fmt.bgraOrder

        if bgra_order and len(value) >= 4:
            value = tuple(value[i] for i in [2, 1, 0, 3])

        return value

    def _build_input_mesh_attributes(self, pipe, action):
        """Build mesh attribute descriptors for VS Input."""
        ib = pipe.GetIBuffer()
        vbs = pipe.GetVBuffers()
        attrs = pipe.GetVertexInputs()

        mesh = {
            "index_resource_id": ib.resourceId,
            "index_byte_offset": int(ib.byteOffset),
            "index_byte_stride": int(ib.byteStride),
            "base_vertex": int(getattr(action, "baseVertex", 0)),
            "index_offset": int(getattr(action, "indexOffset", 0)),
            "num_indices": int(action.numIndices),
        }

        if not (action.flags & rd.ActionFlags.Indexed):
            mesh["index_resource_id"] = rd.ResourceId.Null()

        mesh_attrs = []
        for attr in attrs:
            if not getattr(attr, "used", True):
                continue

            if attr.genericEnabled:
                mesh_attrs.append(
                    self._make_mesh_attribute(
                        name=attr.name,
                        format=attr.format,
                        comp_count=int(attr.format.compCount),
                        vertex_resource_id=rd.ResourceId.Null(),
                        vertex_byte_offset=0,
                        vertex_byte_stride=0,
                        per_instance=bool(attr.perInstance),
                        instance_rate=max(1, int(attr.instanceRate)),
                        generic_enabled=True,
                        generic_value=tuple(attr.genericValue.f32v[: int(attr.format.compCount)]),
                    )
                )
                continue

            vb = vbs[attr.vertexBuffer]
            mesh_attrs.append(
                self._make_mesh_attribute(
                    name=attr.name,
                    format=attr.format,
                    comp_count=int(attr.format.compCount),
                    vertex_resource_id=vb.resourceId,
                    vertex_byte_offset=int(attr.byteOffset) + int(vb.byteOffset),
                    vertex_byte_stride=int(vb.byteStride),
                    per_instance=bool(attr.perInstance),
                    instance_rate=max(1, int(attr.instanceRate)),
                    generic_enabled=False,
                    generic_value=None,
                )
            )

        return mesh, mesh_attrs

    def _build_postvs_mesh_attributes(self, controller, pipe, stage_enum, stage_name, instance, view):
        """Build mesh attribute descriptors for post-VS / post-GS data."""
        postvs = controller.GetPostVSData(instance, view, stage_enum)
        if not postvs or postvs.vertexResourceId == rd.ResourceId.Null():
            raise RuntimeError("No %s data available for this draw call" % stage_name)

        reflection_stage = rd.ShaderStage.Vertex
        if stage_enum == rd.MeshDataStage.GSOut:
            reflection_stage = rd.ShaderStage.Geometry

        shader_reflection = pipe.GetShaderReflection(reflection_stage)
        if shader_reflection is None:
            raise RuntimeError("No shader reflection available for %s" % stage_name)

        outputs = []
        pos_index = -1
        signature = shader_reflection.outputSignature
        comp_type_double = getattr(rd.CompType, "Double", None)

        for attr in signature:
            fmt = rd.ResourceFormat()
            is_double = comp_type_double is not None and attr.compType == comp_type_double
            fmt.compByteWidth = 8 if is_double else 4
            fmt.compCount = attr.compCount
            fmt.compType = comp_type_double if is_double else rd.CompType.Float
            fmt.type = rd.ResourceFormatType.Regular

            name = attr.semanticIdxName if attr.varName == "" else attr.varName
            outputs.append(
                self._make_mesh_attribute(
                    name=name,
                    format=fmt,
                    comp_count=int(fmt.compCount),
                    vertex_resource_id=postvs.vertexResourceId,
                    vertex_byte_offset=0,
                    vertex_byte_stride=int(postvs.vertexByteStride),
                    per_instance=False,
                    instance_rate=1,
                    generic_enabled=False,
                    generic_value=None,
                )
            )

            if attr.systemValue == rd.ShaderBuiltin.Position:
                pos_index = len(outputs) - 1

        if pos_index > 0:
            pos = outputs[pos_index]
            del outputs[pos_index]
            outputs.insert(0, pos)

        accum_offset = int(postvs.vertexByteOffset)
        for attr in outputs:
            attr["vertex_byte_offset"] = accum_offset
            fmt = attr["format"]
            accum_offset += (8 if comp_type_double is not None and fmt.compType == comp_type_double else 4) * fmt.compCount

        mesh = {
            "index_resource_id": postvs.indexResourceId,
            "index_byte_offset": int(postvs.indexByteOffset),
            "index_byte_stride": int(postvs.indexByteStride),
            "base_vertex": int(postvs.baseVertex),
            "index_offset": 0,
            "num_indices": int(postvs.numIndices),
        }

        return mesh, outputs

    def _component_suffixes(self, comp_count):
        """Get component suffixes for CSV column names."""
        default_suffixes = ["x", "y", "z", "w"]
        if comp_count <= len(default_suffixes):
            return default_suffixes[:comp_count]
        return [str(i) for i in range(comp_count)]

    def _read_mesh_rows(self, controller, mesh, mesh_attrs, instance):
        """Decode mesh rows ready to be written to CSV."""
        indices = self._get_indices(controller, mesh, action=mesh["action"])
        buffer_cache = {}
        rows = []

        for row_index, vertex_index in enumerate(indices):
            row = {
                "row": row_index,
                "vertex_index": int(vertex_index),
                "instance": int(instance),
            }

            for attr in mesh_attrs:
                if attr["generic_enabled"]:
                    value = attr["generic_value"]
                else:
                    source_index = int(vertex_index)
                    if attr["per_instance"]:
                        source_index = int(instance) // max(1, attr["instance_rate"])

                    resource_id = attr["vertex_resource_id"]
                    resource_key = str(resource_id)
                    if resource_key not in buffer_cache:
                        buffer_cache[resource_key] = controller.GetBufferData(resource_id, 0, 0)

                    buffer_data = buffer_cache[resource_key]
                    offset = attr["vertex_byte_offset"] + attr["vertex_byte_stride"] * source_index
                    value = self._unpack_data(attr["format"], buffer_data[offset:])

                suffixes = self._component_suffixes(attr["comp_count"])
                for i, component in enumerate(value):
                    column_name = attr["name"]
                    if len(suffixes) > 1:
                        column_name = "%s_%s" % (attr["name"], suffixes[i])
                    row[column_name] = component

            rows.append(row)

        return rows

    def _build_mesh_fieldnames(self, mesh_attrs):
        """Build CSV fieldnames for mesh export."""
        fieldnames = ["row", "vertex_index", "instance"]
        for attr in mesh_attrs:
            suffixes = self._component_suffixes(attr["comp_count"])
            if len(suffixes) == 1:
                fieldnames.append(attr["name"])
            else:
                for suffix in suffixes:
                    fieldnames.append("%s_%s" % (attr["name"], suffix))
        return fieldnames

    def _write_mesh_csv(self, final_path, rows, mesh_attrs, index_mode="original"):
        """Write mesh rows to CSV."""
        fieldnames = self._build_mesh_fieldnames(mesh_attrs)
        with open(final_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            if index_mode == "triangle_soup":
                for row in rows:
                    row_copy = dict(row)
                    row_copy["vertex_index"] = row_copy["row"]
                    writer.writerow(row_copy)
            else:
                writer.writerows(rows)

    def _export_mesh_csv_from_current_event(
        self,
        controller,
        action,
        structured_file,
        output_path,
        mesh_stage="vs_input",
        instance=0,
        view=0,
        index_mode="original",
    ):
        """Export mesh CSV assuming the target event is already set on the controller."""
        pipe = controller.GetPipelineState()
        action_name = action.GetName(structured_file)
        topology = ""
        try:
            ia = pipe.GetIAState()
            if ia:
                topology = str(ia.topology)
        except Exception:
            topology = ""

        normalized_stage = str(mesh_stage).lower().strip()
        if normalized_stage in ("vs_input", "input", "vsin"):
            mesh, mesh_attrs = self._build_input_mesh_attributes(pipe, action)
            exported_stage = "vs_input"
        elif normalized_stage in ("vs_output", "output", "vsout"):
            mesh, mesh_attrs = self._build_postvs_mesh_attributes(
                controller,
                pipe,
                rd.MeshDataStage.VSOut,
                "VS Output",
                int(instance),
                int(view),
            )
            exported_stage = "vs_output"
        elif normalized_stage in ("gs_output", "gsout"):
            mesh, mesh_attrs = self._build_postvs_mesh_attributes(
                controller,
                pipe,
                rd.MeshDataStage.GSOut,
                "GS Output",
                int(instance),
                int(view),
            )
            exported_stage = "gs_output"
        else:
            raise RuntimeError(
                "Unsupported mesh_stage '%s'. Supported values: vs_input, vs_output, gs_output"
                % mesh_stage
            )

        if not mesh_attrs:
            raise RuntimeError("No mesh attributes available for export")

        final_path = self._resolve_mesh_output_path(
            output_path=output_path,
            action_name=action_name,
            event_id=action.eventId,
            mesh_stage=exported_stage if index_mode == "original" else exported_stage + "_" + index_mode,
        )
        output_dir = os.path.dirname(final_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)

        mesh["action"] = action
        rows = self._read_mesh_rows(controller, mesh, mesh_attrs, int(instance))
        self._write_mesh_csv(final_path, rows, mesh_attrs, index_mode=index_mode)

        return {
            "event_id": action.eventId,
            "action_name": action_name,
            "mesh_stage": exported_stage,
            "topology": topology,
            "index_mode": index_mode,
            "instance": int(instance),
            "view": int(view),
            "row_count": len(rows),
            "attribute_count": len(mesh_attrs),
            "output_path": final_path,
        }

    def get_draw_calls(
        self,
        include_children=True,
        marker_filter=None,
        exclude_markers=None,
        event_id_min=None,
        event_id_max=None,
        only_actions=False,
        flags_filter=None,
    ):
        """
        Get all draw calls/actions in the capture with optional filtering.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"actions": []}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            result["actions"] = Serializers.serialize_actions(
                root_actions,
                structured_file,
                include_children,
                marker_filter=marker_filter,
                exclude_markers=exclude_markers,
                event_id_min=event_id_min,
                event_id_max=event_id_max,
                only_actions=only_actions,
                flags_filter=flags_filter,
            )

        self._invoke(callback)
        return result

    def get_frame_summary(self):
        """
        Get a summary of the current capture frame.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"summary": None}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            api = controller.GetAPIProperties().pipelineType

            # Statistics counters
            stats = {
                "draw_calls": 0,
                "dispatches": 0,
                "clears": 0,
                "copies": 0,
                "presents": 0,
                "markers": 0,
            }
            total_actions = [0]

            def count_actions(actions):
                for action in actions:
                    total_actions[0] += 1
                    flags = action.flags

                    if flags & rd.ActionFlags.Drawcall:
                        stats["draw_calls"] += 1
                    if flags & rd.ActionFlags.Dispatch:
                        stats["dispatches"] += 1
                    if flags & rd.ActionFlags.Clear:
                        stats["clears"] += 1
                    if flags & rd.ActionFlags.Copy:
                        stats["copies"] += 1
                    if flags & rd.ActionFlags.Present:
                        stats["presents"] += 1
                    if flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker):
                        stats["markers"] += 1

                    if action.children:
                        count_actions(action.children)

            count_actions(root_actions)

            # Top-level markers
            top_markers = []
            for action in root_actions:
                if action.flags & rd.ActionFlags.PushMarker:
                    child_count = Helpers.count_children(action)
                    top_markers.append({
                        "name": action.GetName(structured_file),
                        "event_id": action.eventId,
                        "child_count": child_count,
                    })

            # Resource counts
            textures = controller.GetTextures()
            buffers = controller.GetBuffers()

            result["summary"] = {
                "api": str(api),
                "total_actions": total_actions[0],
                "statistics": stats,
                "top_level_markers": top_markers,
                "resource_counts": {
                    "textures": len(textures),
                    "buffers": len(buffers),
                },
            }

        self._invoke(callback)
        return result["summary"]

    def get_draw_call_details(self, event_id):
        """Get detailed information about a specific draw call"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"details": None, "error": None}

        def callback(controller):
            # Move to the event
            controller.SetFrameEvent(event_id, True)

            action = self.ctx.GetAction(event_id)
            if not action:
                result["error"] = "No action at event %d" % event_id
                return

            structured_file = controller.GetStructuredFile()

            details = {
                "event_id": action.eventId,
                "action_id": action.actionId,
                "name": action.GetName(structured_file),
                "flags": Serializers.serialize_flags(action.flags),
                "num_indices": action.numIndices,
                "num_instances": action.numInstances,
                "base_vertex": action.baseVertex,
                "vertex_offset": action.vertexOffset,
                "instance_offset": action.instanceOffset,
                "index_offset": action.indexOffset,
            }

            # Output resources
            outputs = []
            for i, output in enumerate(action.outputs):
                if output != rd.ResourceId.Null():
                    outputs.append({"index": i, "resource_id": str(output)})
            details["outputs"] = outputs

            if action.depthOut != rd.ResourceId.Null():
                details["depth_output"] = str(action.depthOut)

            result["details"] = details

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["details"]

    def get_action_timings(
        self,
        event_ids=None,
        marker_filter=None,
        exclude_markers=None,
    ):
        """
        Get GPU timing information for actions.

        Args:
            event_ids: Optional list of specific event IDs to get timings for.
                      If None, returns timings for all actions.
            marker_filter: Only include actions under markers containing this string.
            exclude_markers: Exclude actions under markers containing these strings.

        Returns:
            Dictionary with:
            - available: Whether GPU timing counters are supported
            - unit: Time unit (typically "seconds")
            - timings: List of {event_id, name, duration_seconds, duration_ms}
            - total_duration_ms: Sum of all durations
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            # Check if EventGPUDuration counter is available
            counters = controller.EnumerateCounters()
            if rd.GPUCounter.EventGPUDuration not in counters:
                result["data"] = {
                    "available": False,
                    "error": "GPU timing counters not supported on this capture",
                }
                return

            # Get counter description
            counter_desc = controller.DescribeCounter(rd.GPUCounter.EventGPUDuration)

            # Fetch timing data
            counter_results = controller.FetchCounters([rd.GPUCounter.EventGPUDuration])

            # Build event_id to timing map
            timing_map = {}
            target_counter = int(rd.GPUCounter.EventGPUDuration)
            for r in counter_results:
                if r.counter == target_counter:
                    # EventGPUDuration typically returns double
                    # Try to get the value in the most appropriate way
                    val = r.value.d  # double is the standard for duration
                    timing_map[r.eventId] = val

            # Get structured file for action names
            structured_file = controller.GetStructuredFile()
            root_actions = controller.GetRootActions()

            # Collect actions to report timings for
            timings = []
            total_duration = [0.0]

            def collect_timings(actions, parent_markers=None):
                if parent_markers is None:
                    parent_markers = []

                for action in actions:
                    action_name = action.GetName(structured_file)
                    current_markers = parent_markers[:]

                    # Track marker hierarchy
                    is_marker = bool(action.flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker))
                    if is_marker:
                        current_markers.append(action_name)

                    # Apply marker filter
                    if marker_filter:
                        marker_path = "/".join(current_markers)
                        if marker_filter.lower() not in marker_path.lower():
                            # Still recurse into children
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Apply exclude filter
                    if exclude_markers:
                        skip = False
                        for exclude in exclude_markers:
                            for m in current_markers:
                                if exclude.lower() in m.lower():
                                    skip = True
                                    break
                            if skip:
                                break
                        if skip:
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Check if we should include this event
                    event_id = action.eventId
                    include = True
                    if event_ids is not None:
                        include = event_id in event_ids

                    if include and event_id in timing_map:
                        duration_sec = timing_map[event_id]
                        duration_ms = duration_sec * 1000.0
                        timings.append({
                            "event_id": event_id,
                            "name": action_name,
                            "duration_seconds": duration_sec,
                            "duration_ms": duration_ms,
                        })
                        total_duration[0] += duration_ms

                    # Recurse into children
                    if action.children:
                        collect_timings(action.children, current_markers)

            collect_timings(root_actions)

            # Sort by event_id
            timings.sort(key=lambda x: x["event_id"])

            result["data"] = {
                "available": True,
                "unit": str(counter_desc.unit),
                "timings": timings,
                "total_duration_ms": total_duration[0],
                "count": len(timings),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def save_mesh_csv(self, event_id, output_path, mesh_stage="vs_input", instance=0, view=0):
        """Export the current draw call mesh data to CSV."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                action = self.ctx.GetAction(event_id)
                if not action:
                    result["error"] = "No action at event %d" % event_id
                    return

                if not (action.flags & rd.ActionFlags.Drawcall):
                    result["error"] = "Event %d is not a draw call" % event_id
                    return
                structured_file = controller.GetStructuredFile()
                result["data"] = self._export_mesh_csv_from_current_event(
                    controller,
                    action,
                    structured_file,
                    output_path,
                    mesh_stage=mesh_stage,
                    instance=int(instance),
                    view=int(view),
                )
            except Exception as e:
                import traceback

                result["error"] = "Error saving mesh CSV: %s\n%s" % (
                    str(e),
                    traceback.format_exc(),
                )

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def export_event_assets(
        self,
        event_id,
        output_dir,
        include_mesh=True,
        include_textures=True,
        texture_stages=None,
        mesh_stage="vs_input",
        instance=0,
        view=0,
        texture_file_format="png",
    ):
        """Export a draw call's mesh and texture assets into a structured directory."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        export_root = os.path.normpath(output_dir)
        status_path = self._event_asset_status_path(export_root)
        if isinstance(texture_stages, str):
            normalized_texture_stages = [texture_stages]
        else:
            normalized_texture_stages = texture_stages or ["pixel"]
        requested_stages = [str(stage).lower().strip() for stage in normalized_texture_stages]
        job_key = export_root.lower()

        def perform_export():
            result = {"data": None, "error": None}

            def callback(controller):
                try:
                    controller.SetFrameEvent(event_id, True)

                    action = self.ctx.GetAction(event_id)
                    if not action:
                        result["error"] = "No action at event %d" % event_id
                        return

                    if not (action.flags & rd.ActionFlags.Drawcall):
                        result["error"] = "Event %d is not a draw call" % event_id
                        return

                    structured_file = controller.GetStructuredFile()
                    action_name = action.GetName(structured_file)
                    os.makedirs(export_root, exist_ok=True)
                    manifest_path = os.path.join(export_root, "manifest.json")
                    if os.path.exists(manifest_path):
                        os.remove(manifest_path)

                    manifest = {
                        "event_id": int(event_id),
                        "action_name": action_name,
                        "instance": int(instance),
                        "view": int(view),
                        "unity_import_hints": {
                            "flip_uv": True,
                            "rotation_euler": [0.0, 90.0, -90.0],
                            "preferred_mesh_output_path": None,
                        },
                        "mesh": None,
                        "textures": [],
                    }

                    if include_textures:
                        pipe = controller.GetPipelineState()
                        textures_dir = os.path.join(export_root, "textures")
                        os.makedirs(textures_dir, exist_ok=True)

                        stage_map = {
                            "vertex": rd.ShaderStage.Vertex,
                            "pixel": rd.ShaderStage.Pixel,
                            "geometry": rd.ShaderStage.Geometry,
                            "hull": rd.ShaderStage.Hull,
                            "domain": rd.ShaderStage.Domain,
                            "compute": rd.ShaderStage.Compute,
                        }

                        seen_resource_ids = set()

                        for stage_name in requested_stages:
                            normalized_stage = str(stage_name).lower().strip()
                            if normalized_stage not in stage_map:
                                result["error"] = (
                                    "Unsupported texture stage '%s'. Supported values: vertex, pixel, geometry, hull, domain, compute"
                                    % stage_name
                                )
                                return

                            stage_enum = stage_map[normalized_stage]
                            reflection = pipe.GetShaderReflection(stage_enum)
                            if reflection is None:
                                continue

                            resources = pipe.GetReadOnlyResources(stage_enum, False)
                            bind_map = {}
                            for bind in reflection.readOnlyResources:
                                bind_map[int(bind.fixedBindNumber)] = bind.name

                            for resource in resources:
                                descriptor = resource.descriptor
                                resource_id = descriptor.resource
                                if resource_id == rd.ResourceId.Null():
                                    continue

                                resource_key = str(resource_id)
                                if resource_key in seen_resource_ids:
                                    continue

                                tex_desc = None
                                for tex in controller.GetTextures():
                                    if tex.resourceId == resource_id:
                                        tex_desc = tex
                                        break
                                if tex_desc is None:
                                    continue
                                if tex_desc.type != rd.TextureType.Texture2D:
                                    continue

                                bind_point = int(resource.access.index)
                                slot_name = bind_map.get(bind_point, "texture%d" % bind_point)
                                texture_name = self.ctx.GetResourceName(resource_id)
                                final_texture_path = self._resolve_texture_output_path(
                                    output_dir=textures_dir,
                                    texture_name=texture_name,
                                    resource_id=resource_id,
                                    slot_name=slot_name,
                                    extension=texture_file_format,
                                )

                                texsave = rd.TextureSave()
                                texsave.resourceId = resource_id
                                texsave.mip = 0
                                texsave.slice.sliceIndex = 0
                                if hasattr(texsave, "sample") and hasattr(texsave.sample, "sampleIndex"):
                                    texsave.sample.sampleIndex = 0
                                elif hasattr(texsave, "sampleIndex"):
                                    texsave.sampleIndex = 0
                                texsave.alpha = rd.AlphaMapping.Preserve
                                if texture_file_format == "png":
                                    texsave.destType = rd.FileType.PNG
                                elif texture_file_format in ("jpg", "jpeg"):
                                    texsave.destType = rd.FileType.JPG
                                elif texture_file_format == "dds":
                                    texsave.destType = rd.FileType.DDS
                                elif texture_file_format == "hdr":
                                    texsave.destType = rd.FileType.HDR
                                else:
                                    result["error"] = (
                                        "Unsupported texture_file_format '%s'. Supported values: png, jpg, jpeg, dds, hdr"
                                        % texture_file_format
                                    )
                                    return

                                controller.SaveTexture(texsave, final_texture_path)
                                if not os.path.exists(final_texture_path):
                                    result["error"] = "RenderDoc did not produce texture file at %s" % final_texture_path
                                    return

                                manifest["textures"].append(
                                    {
                                        "stage": normalized_stage,
                                        "slot": bind_point,
                                        "name": slot_name,
                                        "resource_id": resource_key,
                                        "resource_name": texture_name,
                                        "width": int(tex_desc.width),
                                        "height": int(tex_desc.height),
                                        "format": str(tex_desc.format.Name()),
                                        "output_path": final_texture_path,
                                    }
                                )
                                seen_resource_ids.add(resource_key)

                    if include_mesh:
                        mesh_dir = os.path.join(export_root, "mesh")
                        os.makedirs(mesh_dir, exist_ok=True)
                        mesh_export = self._export_mesh_csv_from_current_event(
                            controller,
                            action,
                            structured_file,
                            mesh_dir,
                            mesh_stage=mesh_stage,
                            instance=int(instance),
                            view=int(view),
                            index_mode="original",
                        )
                        manifest["mesh"] = {
                            "mesh_stage": mesh_export["mesh_stage"],
                            "topology": mesh_export.get("topology", ""),
                            "row_count": mesh_export["row_count"],
                            "attribute_count": mesh_export["attribute_count"],
                            "output_path": mesh_export["output_path"],
                        }
                        manifest["unity_import_hints"]["preferred_mesh_output_path"] = mesh_export["output_path"]

                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=2)

                    result["data"] = {
                        "event_id": int(event_id),
                        "action_name": action_name,
                        "output_dir": export_root,
                        "manifest_path": manifest_path,
                        "mesh_exported": manifest["mesh"] is not None,
                        "texture_count": len(manifest["textures"]),
                    }
                except Exception as e:
                    result["error"] = "Error exporting event assets: %s\n%s" % (
                        str(e),
                        traceback.format_exc(),
                    )

            self._invoke(callback)

            if result["error"]:
                raise ValueError(result["error"])
            return result["data"]

        existing_status = self._read_event_asset_status(export_root)
        if existing_status and existing_status.get("state") == "running":
            reconciled_status = self._build_completed_status_from_manifest(
                export_root,
                fallback_status=existing_status,
            )
            if reconciled_status is not None:
                self._write_event_asset_status(export_root, reconciled_status)
                return reconciled_status
            return existing_status

        status = self._build_event_asset_status(
            event_id=event_id,
            output_dir=export_root,
            state="running",
            include_mesh=include_mesh,
            include_textures=include_textures,
            texture_stages=requested_stages,
            mesh_stage=mesh_stage,
            instance=instance,
            view=view,
            texture_file_format=texture_file_format,
            status_path=status_path,
            message=(
                "Export started in background. Large events may take 3-10 minutes. "
                "Poll export_status.json until state becomes completed or failed; "
                "manifest.json will be written on success."
            ),
        )
        self._write_event_asset_status(export_root, status)

        def worker():
            try:
                data = perform_export()
                finished_status = self._build_event_asset_status(
                    event_id=event_id,
                    output_dir=export_root,
                    state="completed",
                    include_mesh=include_mesh,
                    include_textures=include_textures,
                    texture_stages=requested_stages,
                    mesh_stage=mesh_stage,
                    instance=instance,
                    view=view,
                    texture_file_format=texture_file_format,
                    status_path=status_path,
                    message="Export completed.",
                    result=data,
                )
                self._write_event_asset_status(export_root, finished_status)
            except Exception as e:
                failed_status = self._build_event_asset_status(
                    event_id=event_id,
                    output_dir=export_root,
                    state="failed",
                    include_mesh=include_mesh,
                    include_textures=include_textures,
                    texture_stages=requested_stages,
                    mesh_stage=mesh_stage,
                    instance=instance,
                    view=view,
                    texture_file_format=texture_file_format,
                    status_path=status_path,
                    message="Export failed.",
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                self._write_event_asset_status(export_root, failed_status)
            finally:
                with self._export_jobs_lock:
                    self._export_jobs.pop(job_key, None)

        with self._export_jobs_lock:
            existing_job = self._export_jobs.get(job_key)
            if existing_job and existing_job.is_alive():
                return status

            thread = threading.Thread(
                target=worker,
                name="renderdoc_export_event_assets_%d" % int(event_id),
            )
            thread.daemon = True
            self._export_jobs[job_key] = thread
            thread.start()

        return status
