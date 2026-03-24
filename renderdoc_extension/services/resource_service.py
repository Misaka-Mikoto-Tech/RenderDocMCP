"""
Resource information service for RenderDoc.
"""

import base64
import os
import re

import renderdoc as rd

from ..utils import Parsers


class ResourceService:
    """Resource information service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _find_texture_by_id(self, controller, resource_id):
        """Find texture by resource ID"""
        target_id = Parsers.extract_numeric_id(resource_id)
        for tex in controller.GetTextures():
            tex_id_str = str(tex.resourceId)
            tex_id = Parsers.extract_numeric_id(tex_id_str)
            if tex_id == target_id:
                return tex
        return None

    def _sanitize_filename(self, name):
        """Sanitize a resource name for use as a Windows filename."""
        if not name:
            return ""

        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()
        sanitized = sanitized.rstrip(" .")
        return sanitized

    def _resolve_texture_output_path(self, output_path, texture_name, resource_id, extension):
        """Resolve the final output path for a saved texture."""
        resource_numeric_id = Parsers.extract_numeric_id(resource_id)
        safe_name = self._sanitize_filename(texture_name)
        if not safe_name:
            safe_name = "texture_%d" % resource_numeric_id

        normalized_path = os.path.normpath(output_path)
        base, ext = os.path.splitext(normalized_path)
        has_extension = bool(ext)

        if output_path.endswith(("/", "\\")) or os.path.isdir(normalized_path) or not has_extension:
            directory = normalized_path if not has_extension else os.path.dirname(normalized_path)
            if not directory:
                directory = "."
            return os.path.join(directory, safe_name + "." + extension)

        return normalized_path

    def get_buffer_contents(self, resource_id, offset=0, length=0):
        """Get buffer data"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            # Parse resource ID
            try:
                rid = Parsers.parse_resource_id(resource_id)
            except Exception:
                result["error"] = "Invalid resource ID: %s" % resource_id
                return

            # Find buffer
            buf_desc = None
            for buf in controller.GetBuffers():
                if buf.resourceId == rid:
                    buf_desc = buf
                    break

            if not buf_desc:
                result["error"] = "Buffer not found: %s" % resource_id
                return

            # Get data
            actual_length = length if length > 0 else buf_desc.length
            data = controller.GetBufferData(rid, offset, actual_length)

            result["data"] = {
                "resource_id": resource_id,
                "length": len(data),
                "total_size": buf_desc.length,
                "offset": offset,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def get_texture_info(self, resource_id):
        """Get texture metadata"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"texture": None, "error": None}

        def callback(controller):
            try:
                tex_desc = self._find_texture_by_id(controller, resource_id)

                if not tex_desc:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

                result["texture"] = {
                    "resource_id": resource_id,
                    "width": tex_desc.width,
                    "height": tex_desc.height,
                    "depth": tex_desc.depth,
                    "array_size": tex_desc.arraysize,
                    "mip_levels": tex_desc.mips,
                    "format": str(tex_desc.format.Name()),
                    "dimension": str(tex_desc.type),
                    "msaa_samples": tex_desc.msSamp,
                    "byte_size": tex_desc.byteSize,
                }
            except Exception as e:
                import traceback
                result["error"] = "Error: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["texture"]

    def get_texture_data(self, resource_id, mip=0, slice=0, sample=0, depth_slice=None):
        """Get texture pixel data."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            tex_desc = self._find_texture_by_id(controller, resource_id)

            if not tex_desc:
                result["error"] = "Texture not found: %s" % resource_id
                return

            # Validate mip level
            if mip < 0 or mip >= tex_desc.mips:
                result["error"] = "Invalid mip level %d (texture has %d mips)" % (
                    mip,
                    tex_desc.mips,
                )
                return

            # Validate slice for array/cube textures
            max_slices = tex_desc.arraysize
            if tex_desc.cubemap:
                max_slices = tex_desc.arraysize * 6
            if slice < 0 or (max_slices > 1 and slice >= max_slices):
                result["error"] = "Invalid slice %d (texture has %d slices)" % (
                    slice,
                    max_slices,
                )
                return

            # Validate sample for MSAA
            if sample < 0 or (tex_desc.msSamp > 1 and sample >= tex_desc.msSamp):
                result["error"] = "Invalid sample %d (texture has %d samples)" % (
                    sample,
                    tex_desc.msSamp,
                )
                return

            # Calculate dimensions at this mip level
            mip_width = max(1, tex_desc.width >> mip)
            mip_height = max(1, tex_desc.height >> mip)
            mip_depth = max(1, tex_desc.depth >> mip)

            # Validate depth_slice for 3D textures
            is_3d = tex_desc.depth > 1
            if depth_slice is not None:
                if not is_3d:
                    result["error"] = "depth_slice can only be used with 3D textures"
                    return
                if depth_slice < 0 or depth_slice >= mip_depth:
                    result["error"] = "Invalid depth_slice %d (texture has %d depth at mip %d)" % (
                        depth_slice,
                        mip_depth,
                        mip,
                    )
                    return

            # Create subresource specification
            sub = rd.Subresource()
            sub.mip = mip
            sub.slice = slice
            sub.sample = sample

            # Get texture data
            try:
                data = controller.GetTextureData(tex_desc.resourceId, sub)
            except Exception as e:
                result["error"] = "Failed to get texture data: %s" % str(e)
                return

            # Extract depth slice for 3D textures if requested
            output_depth = mip_depth
            if is_3d and depth_slice is not None:
                total_size = len(data)
                bytes_per_slice = total_size // mip_depth
                slice_start = depth_slice * bytes_per_slice
                slice_end = slice_start + bytes_per_slice
                data = data[slice_start:slice_end]
                output_depth = 1

            result["data"] = {
                "resource_id": resource_id,
                "width": mip_width,
                "height": mip_height,
                "depth": output_depth,
                "mip": mip,
                "slice": slice,
                "sample": sample,
                "depth_slice": depth_slice,
                "format": str(tex_desc.format.Name()),
                "dimension": str(tex_desc.type),
                "is_3d": is_3d,
                "total_depth": mip_depth if is_3d else 1,
                "data_length": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def save_texture(
        self,
        resource_id,
        output_path,
        mip=0,
        slice=0,
        sample=0,
        file_format="png",
    ):
        """Save a texture resource directly to disk using RenderDoc's native exporter."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        format_map = {
            "png": rd.FileType.PNG,
            "jpg": rd.FileType.JPG,
            "jpeg": rd.FileType.JPG,
            "hdr": rd.FileType.HDR,
            "dds": rd.FileType.DDS,
        }

        normalized_format = str(file_format).lower().strip()
        if normalized_format not in format_map:
            raise ValueError(
                "Unsupported file_format '%s'. Supported formats: png, jpg, jpeg, hdr, dds"
                % file_format
            )

        result = {"saved": None, "error": None}

        def callback(controller):
            try:
                tex_desc = self._find_texture_by_id(controller, resource_id)

                if not tex_desc:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

                if mip < 0 or mip >= tex_desc.mips:
                    result["error"] = "Invalid mip level %d (texture has %d mips)" % (
                        mip,
                        tex_desc.mips,
                    )
                    return

                max_slices = tex_desc.arraysize
                if tex_desc.cubemap:
                    max_slices = tex_desc.arraysize * 6
                if slice < 0 or (max_slices > 1 and slice >= max_slices):
                    result["error"] = "Invalid slice %d (texture has %d slices)" % (
                        slice,
                        max_slices,
                    )
                    return

                if sample < 0 or (tex_desc.msSamp > 1 and sample >= tex_desc.msSamp):
                    result["error"] = "Invalid sample %d (texture has %d samples)" % (
                        sample,
                        tex_desc.msSamp,
                    )
                    return

                resource_name = self.ctx.GetResourceName(tex_desc.resourceId)
                final_path = self._resolve_texture_output_path(
                    output_path=output_path,
                    texture_name=resource_name,
                    resource_id=resource_id,
                    extension=normalized_format,
                )
                output_dir = os.path.dirname(final_path)
                if output_dir and not os.path.exists(output_dir):
                    os.makedirs(output_dir)

                texsave = rd.TextureSave()
                texsave.resourceId = tex_desc.resourceId
                texsave.mip = mip
                texsave.slice.sliceIndex = slice
                if hasattr(texsave, "sample") and hasattr(texsave.sample, "sampleIndex"):
                    texsave.sample.sampleIndex = sample
                elif hasattr(texsave, "sampleIndex"):
                    texsave.sampleIndex = sample
                elif sample != 0:
                    result["error"] = "This RenderDoc version does not expose sample selection in TextureSave"
                    return
                texsave.alpha = (
                    rd.AlphaMapping.Preserve
                    if normalized_format in ("png", "dds")
                    else rd.AlphaMapping.BlendToCheckerboard
                )
                texsave.destType = format_map[normalized_format]

                controller.SaveTexture(texsave, final_path)
                if not os.path.exists(final_path):
                    result["error"] = "RenderDoc did not produce an output file at %s" % final_path
                    return

                result["saved"] = {
                    "resource_id": resource_id,
                    "resource_name": resource_name,
                    "output_path": final_path,
                    "file_format": normalized_format,
                    "width": max(1, tex_desc.width >> mip),
                    "height": max(1, tex_desc.height >> mip),
                    "mip": mip,
                    "slice": slice,
                    "sample": sample,
                }
            except Exception as e:
                import traceback

                result["error"] = "Error saving texture: %s\n%s" % (
                    str(e),
                    traceback.format_exc(),
                )

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["saved"]
