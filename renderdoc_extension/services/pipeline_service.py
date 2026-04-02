"""
Pipeline state service for RenderDoc.
"""

import renderdoc as rd

from ..utils import Parsers, Serializers, Helpers


class PipelineService:
    """Pipeline state service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_shader_info(self, event_id, stage):
        """Get shader information for a specific stage"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"shader": None, "error": None}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                pipe = controller.GetPipelineState()
                stage_enum = Parsers.parse_stage(stage)

                shader = pipe.GetShader(stage_enum)
                if shader == rd.ResourceId.Null():
                    result["error"] = "No %s shader bound" % stage
                    return

                entry = pipe.GetShaderEntryPoint(stage_enum)
                reflection = pipe.GetShaderReflection(stage_enum)

                shader_info = {
                    "resource_id": str(shader),
                    "entry_point": entry,
                    "stage": stage,
                }

                # Get disassembly
                try:
                    targets = controller.GetDisassemblyTargets(True)
                    if targets:
                        disasm = controller.DisassembleShader(
                            pipe.GetGraphicsPipelineObject(), reflection, targets[0]
                        )
                        shader_info["disassembly"] = disasm
                except Exception as e:
                    shader_info["disassembly_error"] = str(e)

                # Get constant buffer info
                if reflection:
                    shader_info["constant_buffers"] = self._get_cbuffer_info(
                        controller, pipe, reflection, stage_enum
                    )
                    shader_info["resources"] = self._get_resource_bindings(reflection)

                result["shader"] = shader_info
            except Exception as e:
                result["error"] = str(e)

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["shader"]

    def get_constant_buffer_data(self, event_id, stage, slot):
        """Get a single constant buffer's bound data and decoded variables."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"cbuffer": None, "error": None}

        def callback(controller):
            try:
                controller.SetFrameEvent(event_id, True)

                pipe = controller.GetPipelineState()
                stage_enum = Parsers.parse_stage(stage)

                shader = pipe.GetShader(stage_enum)
                if shader == rd.ResourceId.Null():
                    result["error"] = "No %s shader bound" % stage
                    return

                reflection = pipe.GetShaderReflection(stage_enum)
                if not reflection:
                    result["error"] = "No %s shader reflection available" % stage
                    return

                cbuffer = self._get_single_cbuffer_info(
                    controller, pipe, reflection, stage_enum, slot
                )

                if cbuffer is None:
                    result["error"] = "No constant buffer bound at slot %d" % slot
                    return

                result["cbuffer"] = cbuffer
            except Exception as e:
                result["error"] = str(e)

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["cbuffer"]

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"pipeline": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            api = controller.GetAPIProperties().pipelineType

            pipeline_info = {
                "event_id": event_id,
                "api": str(api),
            }

            # Shader stages with detailed bindings
            stages = {}
            stage_list = Helpers.get_all_shader_stages()
            for stage in stage_list:
                shader = pipe.GetShader(stage)
                if shader != rd.ResourceId.Null():
                    stage_info = {
                        "resource_id": str(shader),
                        "entry_point": pipe.GetShaderEntryPoint(stage),
                    }

                    reflection = pipe.GetShaderReflection(stage)

                    stage_info["resources"] = self._get_stage_resources(
                        controller, pipe, stage, reflection
                    )
                    stage_info["uavs"] = self._get_stage_uavs(
                        controller, pipe, stage, reflection
                    )
                    stage_info["samplers"] = self._get_stage_samplers(
                        pipe, stage, reflection
                    )
                    stage_info["constant_buffers"] = self._get_stage_cbuffers(
                        controller, pipe, stage, reflection
                    )

                    stages[str(stage)] = stage_info

            pipeline_info["shaders"] = stages

            # Viewport and scissor
            try:
                vp_scissor = pipe.GetViewportScissor()
                if vp_scissor:
                    viewports = []
                    for v in vp_scissor.viewports:
                        viewports.append(
                            {
                                "x": v.x,
                                "y": v.y,
                                "width": v.width,
                                "height": v.height,
                                "min_depth": v.minDepth,
                                "max_depth": v.maxDepth,
                            }
                        )
                    pipeline_info["viewports"] = viewports
            except Exception:
                pass

            # Render targets
            try:
                om = pipe.GetOutputMerger()
                if om:
                    rts = []
                    for i, rt in enumerate(om.renderTargets):
                        if rt.resourceId != rd.ResourceId.Null():
                            rts.append({"index": i, "resource_id": str(rt.resourceId)})
                    pipeline_info["render_targets"] = rts

                    if om.depthTarget.resourceId != rd.ResourceId.Null():
                        pipeline_info["depth_target"] = str(om.depthTarget.resourceId)
            except Exception:
                pass

            # Input assembly
            try:
                ia = pipe.GetIAState()
                if ia:
                    pipeline_info["input_assembly"] = {"topology": str(ia.topology)}
            except Exception:
                pass

            # Rasterizer state
            try:
                rasterizer = self._get_rasterizer_state(controller, pipe)
                if rasterizer:
                    pipeline_info["rasterizer"] = rasterizer
            except Exception:
                pass

            result["pipeline"] = pipeline_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["pipeline"]

    def _get_stage_resources(self, controller, pipe, stage, reflection):
        """Get shader resource views (SRVs) for a stage"""
        resources = []
        try:
            srvs = pipe.GetReadOnlyResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readOnlyResources:
                    name_map[res.fixedBindNumber] = res.name

            for srv in srvs:
                if srv.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = srv.access.index
                res_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(srv.descriptor.resource),
                }

                res_info.update(
                    self._get_resource_details(controller, srv.descriptor.resource)
                )

                res_info["first_mip"] = srv.descriptor.firstMip
                res_info["num_mips"] = srv.descriptor.numMips
                res_info["first_slice"] = srv.descriptor.firstSlice
                res_info["num_slices"] = srv.descriptor.numSlices

                resources.append(res_info)
        except Exception as e:
            resources.append({"error": str(e)})

        return resources

    def _get_stage_uavs(self, controller, pipe, stage, reflection):
        """Get unordered access views (UAVs) for a stage"""
        uavs = []
        try:
            uav_list = pipe.GetReadWriteResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readWriteResources:
                    name_map[res.fixedBindNumber] = res.name

            for uav in uav_list:
                if uav.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = uav.access.index
                uav_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(uav.descriptor.resource),
                }

                uav_info.update(
                    self._get_resource_details(controller, uav.descriptor.resource)
                )

                uav_info["first_element"] = uav.descriptor.firstMip
                uav_info["num_elements"] = uav.descriptor.numMips

                uavs.append(uav_info)
        except Exception as e:
            uavs.append({"error": str(e)})

        return uavs

    def _get_stage_samplers(self, pipe, stage, reflection):
        """Get samplers for a stage"""
        samplers = []
        try:
            sampler_list = pipe.GetSamplers(stage, False)

            name_map = {}
            if reflection:
                for samp in reflection.samplers:
                    name_map[samp.fixedBindNumber] = samp.name

            for samp in sampler_list:
                slot = samp.access.index
                samp_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                }

                desc = samp.descriptor
                try:
                    samp_info["address_u"] = str(desc.addressU)
                    samp_info["address_v"] = str(desc.addressV)
                    samp_info["address_w"] = str(desc.addressW)
                except AttributeError:
                    pass

                try:
                    samp_info["filter"] = str(desc.filter)
                except AttributeError:
                    pass

                try:
                    samp_info["max_anisotropy"] = desc.maxAnisotropy
                except AttributeError:
                    pass

                try:
                    samp_info["min_lod"] = desc.minLOD
                    samp_info["max_lod"] = desc.maxLOD
                    samp_info["mip_lod_bias"] = desc.mipLODBias
                except AttributeError:
                    pass

                try:
                    samp_info["border_color"] = [
                        desc.borderColor[0],
                        desc.borderColor[1],
                        desc.borderColor[2],
                        desc.borderColor[3],
                    ]
                except (AttributeError, TypeError):
                    pass

                try:
                    samp_info["compare_function"] = str(desc.compareFunction)
                except AttributeError:
                    pass

                samplers.append(samp_info)
        except Exception as e:
            samplers.append({"error": str(e)})

        return samplers

    def _get_stage_cbuffers(self, controller, pipe, stage, reflection):
        """Get constant buffers for a stage from shader reflection"""
        cbuffers = []
        try:
            if not reflection:
                return cbuffers

            for cb in reflection.constantBlocks:
                slot = cb.bindPoint if hasattr(cb, 'bindPoint') else cb.fixedBindNumber
                cb_info = {
                    "slot": slot,
                    "name": cb.name,
                    "byte_size": cb.byteSize,
                    "variable_count": len(cb.variables) if cb.variables else 0,
                    "variables": [],
                }
                if cb.variables:
                    for var in cb.variables:
                        cb_info["variables"].append({
                            "name": var.name,
                            "byte_offset": var.byteOffset,
                            "type": str(var.type.name) if var.type else "",
                        })
                cbuffers.append(cb_info)

        except Exception as e:
            cbuffers.append({"error": str(e)})

        return cbuffers

    def _get_resource_details(self, controller, resource_id):
        """Get details about a resource (texture or buffer)"""
        details = {}

        try:
            resource_name = self.ctx.GetResourceName(resource_id)
            if resource_name:
                details["resource_name"] = resource_name
        except Exception:
            pass

        for tex in controller.GetTextures():
            if tex.resourceId == resource_id:
                details["type"] = "texture"
                details["width"] = tex.width
                details["height"] = tex.height
                details["depth"] = tex.depth
                details["array_size"] = tex.arraysize
                details["mip_levels"] = tex.mips
                details["format"] = str(tex.format.Name())
                details["dimension"] = str(tex.type)
                details["msaa_samples"] = tex.msSamp
                return details

        for buf in controller.GetBuffers():
            if buf.resourceId == resource_id:
                details["type"] = "buffer"
                details["length"] = buf.length
                return details

        return details

    def _get_cbuffer_info(self, controller, pipe, reflection, stage):
        """Get constant buffer information and values"""
        cbuffers = []

        for cb_index, cb in enumerate(reflection.constantBlocks):
            cbuffers.append(
                self._build_cbuffer_info(controller, pipe, reflection, stage, cb_index, cb)
            )

        return cbuffers

    def _get_single_cbuffer_info(self, controller, pipe, reflection, stage, slot):
        """Get one reflected constant buffer by bind slot."""
        for cb_index, cb in enumerate(reflection.constantBlocks):
            bind_slot = self._get_cbuffer_bind_slot(cb, cb_index)
            if bind_slot == slot:
                return self._build_cbuffer_info(
                    controller, pipe, reflection, stage, cb_index, cb
                )
        return None

    def _build_cbuffer_info(self, controller, pipe, reflection, stage, cb_index, cb):
        """Build a decoded constant buffer payload."""
        bind_slot = self._get_cbuffer_bind_slot(cb, cb_index)
        cb_info = {
            "name": cb.name,
            "slot": bind_slot,
            "size": cb.byteSize,
            "byte_size": cb.byteSize,
            "variable_count": len(cb.variables) if cb.variables else 0,
            "variables": [],
        }

        binding = self._get_bound_cbuffer_binding(
            controller, stage, bind_slot, cb_index
        )
        if binding is not None:
            resource_id = self._get_cbuffer_resource_id(binding)
            cb_info["resource_id"] = str(resource_id)

            byte_offset = self._get_cbuffer_byte_offset(binding)
            byte_size = self._get_cbuffer_byte_size(binding, cb.byteSize)

            access_offset = self._get_descriptor_access_byte_offset(binding)
            access_size = self._get_descriptor_access_byte_size(binding)

            if access_offset:
                cb_info["descriptor_access_byte_offset"] = access_offset
            if access_size:
                cb_info["descriptor_access_byte_size"] = access_size

            cb_info["byte_offset"] = byte_offset
            cb_info["byte_size"] = byte_size

            if resource_id != rd.ResourceId.Null():
                try:
                    variables, read_mode = self._read_cbuffer_variables(
                        controller,
                        pipe,
                        reflection,
                        stage,
                        cb_index,
                        cb,
                        resource_id,
                        byte_offset,
                        byte_size,
                    )
                    cb_info["variables"] = Serializers.serialize_variables(variables)
                    cb_info["read_mode"] = read_mode
                except Exception as e:
                    cb_info["error"] = str(e)
        else:
            cb_info["error"] = "Could not resolve bound constant buffer for slot %d" % bind_slot

        return cb_info

    def _read_cbuffer_variables(
        self, controller, pipe, reflection, stage, cb_index, cb, resource_id, byte_offset, byte_size
    ):
        """Read cbuffer variables, retrying with Null() if the explicit resource looks wrong."""
        pipeline_object = self._get_pipeline_object(pipe, stage)

        attempts = []
        if resource_id != rd.ResourceId.Null():
            attempts.append(("explicit_resource", resource_id))

        if getattr(cb, "bufferBacked", True):
            attempts.append(("pipeline_bound", rd.ResourceId.Null()))

        last_error = None

        for mode, candidate_resource in attempts:
            try:
                variables = controller.GetCBufferVariableContents(
                    pipeline_object,
                    reflection.resourceId,
                    stage,
                    reflection.entryPoint,
                    cb_index,
                    candidate_resource,
                    byte_offset,
                    byte_size,
                )

                serialized = Serializers.serialize_variables(variables)

                # If the explicit descriptor resolves to all-zero data, allow a fallback
                # through the currently bound pipeline cbuffer. This matches older APIs
                # where the descriptor abstraction can reference virtual/fake objects.
                if mode == "explicit_resource" and self._variables_all_zero(serialized):
                    continue

                return variables, mode
            except Exception as e:
                last_error = e

        if last_error is not None:
            raise last_error

        return [], "unavailable"

    def _variables_all_zero(self, variables):
        """Return True if every serialized numeric value is 0."""
        if not variables:
            return True

        for var in variables:
            if "value" in var:
                for value in var["value"]:
                    if value != 0:
                        return False
            if "members" in var and not self._variables_all_zero(var["members"]):
                return False

        return True

    def _get_cbuffer_bind_slot(self, cb, cb_index):
        """Get the public bind slot for a reflected constant buffer."""
        if hasattr(cb, "bindPoint"):
            return cb.bindPoint
        if hasattr(cb, "fixedBindNumber"):
            return cb.fixedBindNumber
        return cb_index

    def _get_bound_cbuffer_binding(self, controller, stage, bind_slot, cb_index):
        """Resolve the currently bound constant buffer for a stage and slot."""
        generic_pipe = controller.GetPipelineState()

        if hasattr(generic_pipe, "GetConstantBlock"):
            try:
                return generic_pipe.GetConstantBlock(stage, cb_index, 0)
            except Exception:
                pass

        if hasattr(generic_pipe, "GetConstantBuffer"):
            try:
                return generic_pipe.GetConstantBuffer(stage, cb_index, 0)
            except Exception:
                pass

        stage_name = self._stage_name(stage)

        d3d11_candidates = []
        try:
            d3d11_candidates.append(controller.GetD3D11PipelineState())
        except Exception:
            pass
        try:
            d3d11_candidates.append(self.ctx.CurD3D11PipelineState())
        except Exception:
            pass
        for d3d11_pipe in d3d11_candidates:
            binding = self._get_api_specific_cbuffer_binding(
                d3d11_pipe, stage_name, bind_slot, cb_index
            )
            if binding is not None:
                return binding

        d3d12_candidates = []
        try:
            d3d12_candidates.append(controller.GetD3D12PipelineState())
        except Exception:
            pass
        try:
            d3d12_candidates.append(self.ctx.CurD3D12PipelineState())
        except Exception:
            pass
        for d3d12_pipe in d3d12_candidates:
            binding = self._get_api_specific_cbuffer_binding(
                d3d12_pipe, stage_name, bind_slot, cb_index
            )
            if binding is not None:
                return binding

        return None

    def _get_api_specific_cbuffer_binding(self, pipe_state, stage_name, bind_slot, cb_index):
        """Resolve a cbuffer binding from a D3D11/D3D12 stage object."""
        if not pipe_state:
            return None

        shader_stage = getattr(pipe_state, "%sShader" % stage_name, None)
        if shader_stage is None:
            try:
                state_attrs = list(dir(pipe_state))
            except Exception:
                state_attrs = []
            raise AttributeError(
                "%s has no '%sShader' field; shader attrs=%s"
                % (type(pipe_state).__name__, stage_name, state_attrs)
            )

        cbuffer_bindings = self._get_shader_stage_cbuffer_bindings(shader_stage)
        mapped_slot = self._get_mapped_cbuffer_slot(shader_stage, bind_slot, cb_index)
        if (
            shader_stage
            and cbuffer_bindings is not None
            and mapped_slot is not None
            and mapped_slot < len(cbuffer_bindings)
        ):
            return cbuffer_bindings[mapped_slot]
        return None

    def _get_shader_stage_cbuffer_bindings(self, shader_stage):
        """Get a stage's constant buffer bindings across binding/version differences."""
        if not shader_stage:
            return None

        candidate_names = [
            "constantBuffers",
            "constantbuffers",
            "constantBuffer",
            "constantbuffer",
            "constantBlocks",
            "constantblocks",
            "cbuffers",
        ]

        for name in candidate_names:
            try:
                value = getattr(shader_stage, name)
                if value is not None:
                    return value
            except Exception:
                pass

        stage_attrs = []
        try:
            stage_attrs = list(dir(shader_stage))
        except Exception:
            pass

        raise AttributeError(
            "%s has no recognizable constant buffer binding field; candidate attrs=%s"
            % (type(shader_stage).__name__, stage_attrs)
        )

    def _get_mapped_cbuffer_slot(self, shader_stage, bind_slot, cb_index):
        """Map a reflected cbuffer slot to the underlying bound buffer index."""
        if not shader_stage:
            return None

        try:
            mapping = shader_stage.bindpointMapping.constantBlocks
            if bind_slot < len(mapping):
                bind = mapping[bind_slot]
                if hasattr(bind, "bind"):
                    return bind.bind
        except Exception:
            pass

        return cb_index

    def _stage_name(self, stage):
        """Convert a ShaderStage enum into the corresponding field prefix."""
        mapping = {
            rd.ShaderStage.Vertex: "vertex",
            rd.ShaderStage.Hull: "hull",
            rd.ShaderStage.Domain: "domain",
            rd.ShaderStage.Geometry: "geometry",
            rd.ShaderStage.Pixel: "pixel",
            rd.ShaderStage.Compute: "compute",
        }
        return mapping.get(stage, "")

    def _get_pipeline_object(self, pipe, stage):
        """Resolve the pipeline object expected by GetCBufferVariableContents."""
        if stage == rd.ShaderStage.Compute and hasattr(pipe, "GetComputePipelineObject"):
            return pipe.GetComputePipelineObject()
        return pipe.GetGraphicsPipelineObject()

    def _get_cbuffer_byte_offset(self, binding):
        """Get cbuffer byte offset across API-specific binding structs."""
        if hasattr(binding, "descriptor") and hasattr(binding.descriptor, "byteOffset"):
            return binding.descriptor.byteOffset
        if hasattr(binding, "access") and hasattr(binding.access, "byteOffset"):
            return binding.access.byteOffset
        if hasattr(binding, "byteOffset"):
            return binding.byteOffset
        if hasattr(binding, "vecOffset"):
            return binding.vecOffset * 16
        return 0

    def _get_cbuffer_byte_size(self, binding, reflected_size):
        """Get cbuffer byte size across API-specific binding structs."""
        if hasattr(binding, "descriptor") and hasattr(binding.descriptor, "byteSize"):
            if binding.descriptor.byteSize:
                return binding.descriptor.byteSize
        if hasattr(binding, "access") and hasattr(binding.access, "byteSize"):
            return binding.access.byteSize
        if hasattr(binding, "byteSize") and binding.byteSize:
            return min(binding.byteSize, reflected_size)
        if hasattr(binding, "vecCount") and binding.vecCount:
            return min(binding.vecCount * 16, reflected_size)
        return reflected_size

    def _get_descriptor_access_byte_offset(self, binding):
        """Get the descriptor-store byte offset for debug purposes."""
        if hasattr(binding, "access") and hasattr(binding.access, "byteOffset"):
            return binding.access.byteOffset
        return 0

    def _get_descriptor_access_byte_size(self, binding):
        """Get the descriptor-store byte size for debug purposes."""
        if hasattr(binding, "access") and hasattr(binding.access, "byteSize"):
            return binding.access.byteSize
        return 0

    def _get_cbuffer_resource_id(self, binding):
        """Get the underlying constant-buffer resource id from a binding."""
        if hasattr(binding, "descriptor") and hasattr(binding.descriptor, "resource"):
            return binding.descriptor.resource
        if hasattr(binding, "resourceId"):
            return binding.resourceId
        return rd.ResourceId.Null()

    def _get_resource_bindings(self, reflection):
        """Get shader resource bindings"""
        resources = []

        try:
            for res in reflection.readOnlyResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadOnly",
                    }
                )
        except Exception:
            pass

        try:
            for res in reflection.readWriteResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadWrite",
                    }
                )
        except Exception:
            pass

        return resources

    def _get_rasterizer_state(self, controller, pipe):
        """Get rasterizer state across generic and API-specific pipeline objects."""
        api_specific_candidates = []

        for getter in (
            lambda: controller.GetD3D11PipelineState(),
            lambda: self.ctx.CurD3D11PipelineState(),
            lambda: controller.GetD3D12PipelineState(),
            lambda: self.ctx.CurD3D12PipelineState(),
            lambda: controller.GetGLPipelineState(),
            lambda: self.ctx.CurGLPipelineState(),
            lambda: controller.GetVulkanPipelineState(),
            lambda: self.ctx.CurVulkanPipelineState(),
        ):
            try:
                api_specific_candidates.append(getter())
            except Exception:
                pass

        for pipe_state in api_specific_candidates:
            serialized = self._serialize_api_rasterizer_state(pipe_state)
            if serialized:
                return serialized

        return {}

    def _serialize_api_rasterizer_state(self, pipe_state):
        """Resolve an API-specific rasterizer state object and serialize it."""
        if not pipe_state:
            return {}

        for attr_name in (
            "rasterizer",
            "rasterizerState",
            "rasterizerDesc",
            "rast",
            "rastState",
        ):
            try:
                candidate = getattr(pipe_state, attr_name)
            except Exception:
                continue

            serialized = self._serialize_rasterizer_state(candidate)
            if serialized:
                return serialized

        return {}

    def _serialize_rasterizer_state(self, rasterizer):
        """Serialize a rasterizer state object using best-effort field discovery."""
        if not rasterizer:
            return {}

        wrapper = rasterizer

        if hasattr(rasterizer, "state"):
            try:
                rasterizer = rasterizer.state
            except Exception:
                rasterizer = wrapper

        if hasattr(rasterizer, "descriptor"):
            try:
                rasterizer = rasterizer.descriptor
            except Exception:
                pass

        serialized = {}

        wrapper_field_map = {
            "sample_mask": ("sampleMask",),
            "sample_coverage": ("sampleCoverage",),
            "sample_coverage_invert": ("sampleCoverageInvert",),
            "sample_coverage_value": ("sampleCoverageValue",),
            "sample_mask_value": ("sampleMaskValue",),
        }

        for public_name, candidate_names in wrapper_field_map.items():
            value = self._get_first_attr(wrapper, candidate_names)
            if value is None:
                continue
            serialized[public_name] = self._serialize_simple_value(value)

        field_map = {
            "resource_id": ("resourceId",),
            "fill_mode": ("fillMode", "fillmode"),
            "cull_mode": ("cullMode", "cullmode"),
            "front_ccw": ("frontCCW", "frontCCW", "frontCounterClockwise"),
            "depth_bias": ("depthBias",),
            "depth_bias_clamp": ("depthBiasClamp",),
            "slope_scaled_depth_bias": ("slopeScaledDepthBias",),
            "depth_clip": ("depthClip", "depthClipEnable"),
            "scissor_enable": ("scissorEnable",),
            "multisample_enable": ("multisampleEnable",),
            "antialiased_lines": ("antialiasedLines", "antialiasedLineEnable"),
            "forced_sample_count": ("forcedSampleCount",),
            "conservative_rasterization": (
                "conservativeRasterization",
                "conservativeRaster",
            ),
            "line_raster_mode": ("lineRasterMode",),
            "base_shading_rate": ("baseShadingRate",),
            "pipeline_shading_rate": ("pipelineShadingRate",),
            "shading_rate_combiners": ("shadingRateCombiners",),
            "shading_rate_image": ("shadingRateImage",),
            "depth_clamp": ("depthClamp", "depthClampEnable"),
            "depth_clip_enable": ("depthClipEnable",),
            "depth_bias_enable": ("depthBiasEnable",),
            "rasterizer_discard_enable": ("rasterizerDiscardEnable",),
            "line_width": ("lineWidth",),
            "line_stipple_factor": ("lineStippleFactor",),
            "line_stipple_pattern": ("lineStipplePattern",),
            "provoking_vertex_first": ("provokingVertexFirst",),
            "extra_primitive_overestimation_size": ("extraPrimitiveOverestimationSize",),
            "alpha_to_coverage": ("alphaToCoverage",),
            "alpha_to_one": ("alphaToOne",),
            "min_sample_shading_rate": ("minSampleShadingRate",),
            "point_fade_threshold": ("pointFadeThreshold",),
            "point_origin_upper_left": ("pointOriginUpperLeft",),
            "point_size": ("pointSize",),
            "programmable_point_size": ("programmablePointSize",),
        }

        for public_name, candidate_names in field_map.items():
            value = self._get_first_attr(rasterizer, candidate_names)
            if value is None:
                continue
            serialized[public_name] = self._serialize_simple_value(value)

        return serialized

    def _get_first_attr(self, obj, names):
        """Return the first available attribute value from a list of names."""
        for name in names:
            try:
                return getattr(obj, name)
            except Exception:
                continue
        return None

    def _serialize_simple_value(self, value):
        """Serialize scalar-ish RenderDoc values into JSON-friendly primitives."""
        if not isinstance(value, bool) and hasattr(value, "name"):
            try:
                return value.name
            except Exception:
                pass
        if isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._serialize_simple_value(v) for v in value]
        try:
            return str(value)
        except Exception:
            return repr(value)

    def get_shader_disassembly(self, event_id, stage, start_line=0, max_lines=200):
        """Get shader assembly/disassembly text with pagination support"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if start_line < 0:
            raise ValueError("start_line must be >= 0")
        if max_lines <= 0:
            raise ValueError("max_lines must be > 0")

        result = {"disassembly": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            shader = pipe.GetShader(stage_enum)
            if shader == rd.ResourceId.Null():
                result["error"] = "No %s shader bound" % stage
                return

            reflection = pipe.GetShaderReflection(stage_enum)

            # RenderDoc returns backend assembly/disassembly text here
            # (for example DXBC/DXIL disassembly), not decompiled HLSL.
            try:
                targets = controller.GetDisassemblyTargets(True)
                if targets:
                    disasm = controller.DisassembleShader(
                        pipe.GetGraphicsPipelineObject(), reflection, targets[0]
                    )
                    
                    # Split into lines and paginate
                    lines = disasm.split('\n')
                    total_lines = len(lines)
                    end_line = min(start_line + max_lines, total_lines)
                    
                    result["disassembly"] = {
                        "content": '\n'.join(lines[start_line:end_line]),
                        "start_line": start_line,
                        "end_line": end_line,
                        "total_lines": total_lines,
                        "has_more": end_line < total_lines
                    }
            except Exception as e:
                result["error"] = str(e)

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["disassembly"]
