from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
import os
import platform
import subprocess

import bpy
import numpy as np
from bpy.types import Image, Scene


DENOISER_ITEMS = (
    ('NONE', "None", "Save the noisy bake without denoising"),
    ('INTEGRATED_OIDN', "Integrated OIDN", "Use Blender's built-in compositor denoiser"),
    ('LIGHTMAP_OIDN', "Lightmap OIDN", "Denoise lightmaps with Open Image Denoise"),
    ('OPTIX', "OptiX", "Use an external NVIDIA OptiX denoiser"),
)

@dataclass
class DenoiseContext:
    source_image: Image
    output_path: str
    render_resolution: int
    scene: Scene
    report: Callable[[set[str], str], None]
    source_pixels: np.ndarray | None = None
    oidn_path: str = ""
    optix_path: str = ""


class DenoiserBackend(ABC):
    """Common lifecycle for lightmap denoisers."""

    def __init__(self, context: DenoiseContext):
        self.context = context
        self._temp_dir: TemporaryDirectory | None = None
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._log_path: Path | None = None
        self._cancelled = False
        self._result_pixels: np.ndarray | None = None

    def validate(self) -> str | None:
        return None

    @abstractmethod
    def start(self) -> None:
        pass

    def poll(self) -> bool:
        """Return True while work is still running, False when complete."""
        return False

    def cancel(self) -> None:
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self.cleanup()

    def cleanup(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def _make_temp_dir(self) -> Path:
        if self._temp_dir is None:
            output_parent = Path(self.context.output_path).parent
            output_parent.mkdir(parents=True, exist_ok=True)
            # Keep staging on the destination volume so os.replace stays atomic.
            self._temp_dir = TemporaryDirectory(prefix="pvm_lightmap_", dir=str(output_parent))
        return Path(self._temp_dir.name)

    def _source_pixels(self) -> np.ndarray:
        if self.context.source_pixels is not None:
            return np.asarray(self.context.source_pixels, dtype=np.float32).copy()
        width, height = self.context.source_image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        self.context.source_image.pixels.foreach_get(pixels)
        return pixels.reshape((height, width, 4))

    def _save_image_as(self, image: Image, path: Path, file_format: str) -> None:
        old_path = image.filepath_raw
        old_format = image.file_format
        try:
            image.filepath_raw = str(path)
            image.file_format = file_format
            image.save()
        finally:
            image.filepath_raw = old_path
            image.file_format = old_format

    def _save_source_as(self, path: Path, file_format: str) -> None:
        if self.context.source_pixels is None:
            self._save_image_as(self.context.source_image, path, file_format)
            return
        image = bpy.data.images.new(
            "PVM Denoiser Source",
            width=self.context.render_resolution,
            height=self.context.render_resolution,
            alpha=True,
            float_buffer=True,
            is_data=True,
        )
        try:
            image.colorspace_settings.name = 'Non-Color'
            image.pixels.foreach_set(self.context.source_pixels.ravel())
            image.update()
            self._save_image_as(image, path, file_format)
        finally:
            bpy.data.images.remove(image)

    def _set_result_pixels(self, pixels: np.ndarray) -> None:
        height, width, channels = pixels.shape
        if channels != 4:
            raise RuntimeError("Denoiser result must contain RGBA pixels")
        expected = (self.context.render_resolution, self.context.render_resolution)
        if (width, height) != expected:
            raise RuntimeError(f"Denoiser returned {width}x{height}; expected {expected[0]}x{expected[1]}")
        if not np.isfinite(pixels).all():
            raise RuntimeError("Denoiser result contains non-finite pixels")
        self._result_pixels = np.asarray(pixels, dtype=np.float32).copy()

    def take_result_pixels(self) -> np.ndarray:
        if self._result_pixels is None:
            raise RuntimeError("Denoiser produced no pixel result")
        pixels = self._result_pixels
        self._result_pixels = None
        return pixels

    def _commit_image_file(self, path: Path, preserve_source_alpha: bool = False) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Denoiser output was not written: {path}")
        image = bpy.data.images.load(str(path), check_existing=False)
        try:
            width, height = image.size
            expected = (self.context.render_resolution, self.context.render_resolution)
            if (width, height) != expected:
                raise RuntimeError(f"Denoiser returned {width}x{height}; expected {expected[0]}x{expected[1]}")
            pixels = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            pixels = pixels.reshape((height, width, 4))
            if preserve_source_alpha:
                pixels[:, :, 3] = self._source_pixels()[:, :, 3]
            self._set_result_pixels(pixels)
        finally:
            bpy.data.images.remove(image)


class NoneDenoiser(DenoiserBackend):
    def start(self) -> None:
        self._set_result_pixels(self._source_pixels())


class IntegratedOIDNDenoiser(DenoiserBackend):
    """Run Blender's OIDN compositor in an isolated temporary scene."""

    def start(self) -> None:
        temp_dir = self._make_temp_dir()
        temp_scene = bpy.data.scenes.new("PVM Integrated OIDN")
        camera_data = bpy.data.cameras.new("PVM Denoise Camera")
        camera = bpy.data.objects.new("PVM Denoise Camera", camera_data)
        prepared_image = None
        temp_scene.collection.objects.link(camera)
        temp_scene.camera = camera
        try:
            temp_scene.render.engine = 'BLENDER_EEVEE_NEXT'
            temp_scene.render.resolution_x = self.context.render_resolution
            temp_scene.render.resolution_y = self.context.render_resolution
            temp_scene.render.resolution_percentage = 100
            temp_scene.render.use_compositing = True
            temp_scene.use_nodes = True
            tree = temp_scene.node_tree
            for node in list(tree.nodes):
                tree.nodes.remove(node)

            image_node = tree.nodes.new('CompositorNodeImage')
            if self.context.source_pixels is not None:
                prepared_image = bpy.data.images.new(
                    "PVM Prepared Lightmap",
                    width=self.context.render_resolution,
                    height=self.context.render_resolution,
                    alpha=True,
                    float_buffer=True,
                    is_data=True,
                )
                prepared_image.pixels.foreach_set(self.context.source_pixels.ravel())
                prepared_image.update()
            image_node.image = prepared_image or self.context.source_image
            denoise_node = tree.nodes.new('CompositorNodeDenoise')
            if hasattr(denoise_node, "prefilter"):
                denoise_node.prefilter = 'ACCURATE'
            if hasattr(denoise_node, "quality"):
                denoise_node.quality = 'HIGH'
            if denoise_node.inputs.get("HDR") is not None:
                denoise_node.inputs["HDR"].default_value = True

            output_node = tree.nodes.new('CompositorNodeOutputFile')
            output_node.base_path = str(temp_dir)
            output_node.file_slots[0].path = "integrated_"
            output_node.format.file_format = 'OPEN_EXR'
            output_node.format.color_mode = 'RGBA'
            output_node.format.color_depth = '32'
            tree.links.new(image_node.outputs["Image"], denoise_node.inputs["Image"])
            tree.links.new(denoise_node.outputs["Image"], output_node.inputs[0])

            bpy.ops.render.render(scene=temp_scene.name, write_still=False)
            outputs = sorted(temp_dir.glob("integrated_*.exr"))
            if not outputs:
                raise RuntimeError("Integrated OIDN compositor produced no output")
            self._commit_image_file(outputs[-1], preserve_source_alpha=True)
        finally:
            bpy.data.scenes.remove(temp_scene)
            if prepared_image is not None:
                bpy.data.images.remove(prepared_image)
            if camera.name in bpy.data.objects:
                bpy.data.objects.remove(camera, do_unlink=True)
            if camera_data.name in bpy.data.cameras:
                bpy.data.cameras.remove(camera_data)


def _validate_executable(path: str, label: str) -> tuple[str | None, str | None]:
    if not path.strip():
        return None, f"Set the {label} executable path in PsychoVertexMaster add-on preferences"
    absolute = bpy.path.abspath(path)
    if not os.path.isfile(absolute):
        return None, f"{label} executable was not found: {absolute}"
    return absolute, None


class ExternalDenoiser(DenoiserBackend, ABC):
    executable_label = "Denoiser"

    def __init__(self, context: DenoiseContext):
        super().__init__(context)
        self._result_path: Path | None = None

    @property
    @abstractmethod
    def configured_path(self) -> str:
        pass

    def validate(self) -> str | None:
        _path, error = _validate_executable(self.configured_path, self.executable_label)
        return error

    def start(self) -> None:
        executable, error = _validate_executable(self.configured_path, self.executable_label)
        if error:
            raise RuntimeError(error)
        command, result_path = self._build_command(executable)
        self._result_path = result_path
        self._log_path = self._make_temp_dir() / "denoiser.log"
        self._log_handle = self._log_path.open('wb')
        self._process = subprocess.Popen(
            command,
            shell=False,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            env=self._environment(),
        )

    def _environment(self):
        return None

    @abstractmethod
    def _build_command(self, executable: str) -> tuple[list[str], Path]:
        pass

    def poll(self) -> bool:
        if self._process is None:
            return False
        if self._process.poll() is None:
            return True
        return_code = self._process.returncode
        self._process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        details = ""
        if self._log_path is not None and self._log_path.is_file():
            details = self._log_path.read_text(encoding='utf-8', errors='replace').strip()
        if self._cancelled:
            return False
        if return_code != 0:
            details = details or "No diagnostic output"
            raise RuntimeError(f"{self.executable_label} failed ({return_code}): {details[-1000:]}")
        if details:
            print(f"[LIGHTMAP] {self.executable_label}: {details[-2000:]}")
        if self._result_path is None:
            raise RuntimeError(f"{self.executable_label} produced no result path")
        self._consume_result(self._result_path)
        return False

    def _consume_result(self, result_path: Path) -> None:
        self._commit_image_file(result_path, preserve_source_alpha=True)


class LightmapOIDNDenoiser(ExternalDenoiser):
    executable_label = "Open Image Denoise"

    @property
    def configured_path(self) -> str:
        return self.context.oidn_path

    def _build_command(self, executable: str) -> tuple[list[str], Path]:
        temp_dir = self._make_temp_dir()
        source_path = temp_dir / "noisy.pfm"
        result_path = temp_dir / "denoised.pfm"
        save_pfm(source_path, self._source_pixels()[:, :, :3])
        return [executable, '-f', 'RTLightmap', '-hdr', str(source_path), '-o', str(result_path)], result_path

    def _environment(self):
        environment = os.environ.copy()
        environment.setdefault('OIDN_VERBOSE', '1')
        return environment

    def _consume_result(self, result_path: Path) -> None:
        rgb = load_pfm(result_path)
        source = self._source_pixels()
        if rgb.shape != source[:, :, :3].shape:
            raise RuntimeError(f"Open Image Denoise returned shape {rgb.shape}; expected {source[:, :, :3].shape}")
        self._set_result_pixels(np.dstack((rgb, source[:, :, 3])))


class OptixDenoiser(ExternalDenoiser):
    executable_label = "OptiX"

    @property
    def configured_path(self) -> str:
        return self.context.optix_path

    def validate(self) -> str | None:
        if platform.system() != 'Windows':
            return "The configured OptiX Denoiser.exe backend is supported only on Windows"
        return super().validate()

    def _build_command(self, executable: str) -> tuple[list[str], Path]:
        temp_dir = self._make_temp_dir()
        source_path = temp_dir / "noisy.hdr"
        result_path = temp_dir / "denoised.hdr"
        self._save_source_as(source_path, 'HDR')
        return [executable, '-i', str(source_path), '-o', str(result_path)], result_path


BACKENDS = {
    'NONE': NoneDenoiser,
    'INTEGRATED_OIDN': IntegratedOIDNDenoiser,
    'LIGHTMAP_OIDN': LightmapOIDNDenoiser,
    'OPTIX': OptixDenoiser,
}


def create_backend(identifier: str, context: DenoiseContext) -> DenoiserBackend:
    backend_type = BACKENDS.get(identifier)
    if backend_type is None:
        raise ValueError(f"Unknown denoiser backend: {identifier}")
    return backend_type(context)


def downsample_premultiplied(pixels: np.ndarray, final_resolution: int) -> np.ndarray:
    """Resize RGBA while retaining the noisy bake's existing gutter coverage."""
    height, width, channels = pixels.shape
    if channels != 4 or width != height:
        raise RuntimeError("Lightmap pixels must be a square RGBA image")
    if width == final_resolution:
        return pixels.copy()

    premultiplied = pixels.copy()
    premultiplied[:, :, :3] *= pixels[:, :, 3:4]
    if width % final_resolution == 0:
        factor = width // final_resolution
        result = premultiplied.reshape(
            final_resolution, factor, final_resolution, factor, 4
        ).mean(axis=(1, 3))
    else:
        y = (np.arange(final_resolution) + 0.5) * height / final_resolution - 0.5
        x = (np.arange(final_resolution) + 0.5) * width / final_resolution - 0.5
        y0 = np.clip(np.floor(y).astype(int), 0, height - 1)
        x0 = np.clip(np.floor(x).astype(int), 0, width - 1)
        y1 = np.minimum(y0 + 1, height - 1)
        x1 = np.minimum(x0 + 1, width - 1)
        fy = (y - y0)[:, None, None]
        fx = (x - x0)[None, :, None]
        top = (premultiplied[y0[:, None], x0[None, :]] * (1.0 - fx)
               + premultiplied[y0[:, None], x1[None, :]] * fx)
        bottom = (premultiplied[y1[:, None], x0[None, :]] * (1.0 - fx)
                  + premultiplied[y1[:, None], x1[None, :]] * fx)
        result = top * (1.0 - fy) + bottom * fy
    alpha = result[:, :, 3:4]
    result[:, :, :3] = np.divide(
        result[:, :, :3], alpha,
        out=np.zeros_like(result[:, :, :3]), where=alpha > 1e-8,
    )
    return result.astype(np.float32)


def crop_pixels_to_mask(pixels: np.ndarray, coverage: np.ndarray) -> np.ndarray:
    """Remove stale gutters while retaining only covered, actually baked pixels."""
    pixels = np.asarray(pixels, dtype=np.float32)
    coverage = np.asarray(coverage, dtype=bool)
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise RuntimeError("Lightmap pixels must be an RGBA image")
    if coverage.shape != pixels.shape[:2]:
        raise RuntimeError("Lightmap coverage mask does not match the pixel dimensions")
    core = coverage & (pixels[:, :, 3] > 1e-8)
    result = np.zeros_like(pixels)
    result[core] = pixels[core]
    return result


def dilate_rgba(pixels: np.ndarray, margin: int) -> np.ndarray:
    """Expand opaque RGBA pixels by simultaneous deterministic 8-neighbor rings."""
    pixels = np.asarray(pixels, dtype=np.float32)
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise RuntimeError("Lightmap pixels must be an RGBA image")
    if margin < 0:
        raise RuntimeError("Lightmap margin cannot be negative")

    result = pixels.copy()
    occupied = result[:, :, 3] > 1e-8
    if margin == 0 or not occupied.any():
        return result

    # Earlier directions win ties. All candidates are read from the previous
    # ring so one iteration can never propagate farther than one pixel.
    directions = (
        (-1, 0), (0, -1), (0, 1), (1, 0),
        (-1, -1), (-1, 1), (1, -1), (1, 1),
    )
    height, width = occupied.shape
    for _ in range(margin):
        previous_occupied = occupied
        fillable = ~previous_occupied
        filled_any = False
        for dy, dx in directions:
            src_y0 = max(0, -dy)
            src_y1 = min(height, height - dy)
            src_x0 = max(0, -dx)
            src_x1 = min(width, width - dx)
            dst_y0 = src_y0 + dy
            dst_y1 = src_y1 + dy
            dst_x0 = src_x0 + dx
            dst_x1 = src_x1 + dx
            source_mask = previous_occupied[src_y0:src_y1, src_x0:src_x1]
            target_open = fillable[dst_y0:dst_y1, dst_x0:dst_x1]
            take = source_mask & target_open
            if not take.any():
                continue
            target_pixels = result[dst_y0:dst_y1, dst_x0:dst_x1]
            source_pixels = result[src_y0:src_y1, src_x0:src_x1]
            target_pixels[take] = source_pixels[take]
            target_open[take] = False
            filled_any = True
        occupied = ~fillable
        if not filled_any:
            break
    return result


def save_pixels_exr_atomic(pixels: np.ndarray, output_path: str, final_resolution: int) -> None:
    pixels = np.asarray(pixels, dtype=np.float32)
    if pixels.ndim != 3 or pixels.shape[2] != 4:
        raise RuntimeError("Lightmap output must contain RGBA pixels")
    if not np.isfinite(pixels).all():
        raise RuntimeError("Lightmap output contains non-finite pixels")
    height, width, _channels = pixels.shape
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pvm_lightmap_commit_", dir=str(output.parent)) as temp_name:
        stage_path = Path(temp_name) / "final.exr"
        result = bpy.data.images.new(
            "PVM_LightmapCommit",
            width=width,
            height=height,
            alpha=True,
            float_buffer=True,
            is_data=True,
        )
        try:
            result.colorspace_settings.name = 'Non-Color'
            result.pixels.foreach_set(pixels.ravel())
            result.update()
            if final_resolution != width or final_resolution != height:
                result.scale(final_resolution, final_resolution)
            old_path = result.filepath_raw
            old_format = result.file_format
            try:
                result.filepath_raw = str(stage_path)
                result.file_format = 'OPEN_EXR'
                result.save()
            finally:
                result.filepath_raw = old_path
                result.file_format = old_format
        finally:
            bpy.data.images.remove(result)
        if not stage_path.is_file() or stage_path.stat().st_size == 0:
            raise RuntimeError("Lightmap writer produced no staged EXR")
        verification = bpy.data.images.load(str(stage_path), check_existing=False)
        try:
            if tuple(verification.size) != (final_resolution, final_resolution):
                raise RuntimeError(
                    f"Staged lightmap is {verification.size[0]}x{verification.size[1]}; "
                    f"expected {final_resolution}x{final_resolution}"
                )
        finally:
            bpy.data.images.remove(verification)
        os.replace(stage_path, output)


def save_pfm(path: Path, image: np.ndarray) -> None:
    image = np.asarray(image, dtype=np.float32)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("PFM image must have shape H x W x 3")
    with path.open('wb') as handle:
        handle.write(b"PF\n")
        handle.write(f"{image.shape[1]} {image.shape[0]}\n".encode('ascii'))
        scale = -1.0 if np.little_endian else 1.0
        handle.write(f"{scale}\n".encode('ascii'))
        image.tofile(handle)


def load_pfm(path: Path) -> np.ndarray:
    with path.open('rb') as handle:
        header = handle.readline().decode('ascii').strip()
        if header != 'PF':
            raise RuntimeError("OIDN output is not a color PFM file")
        dimensions = handle.readline().decode('ascii').strip().split()
        if len(dimensions) != 2:
            raise RuntimeError("OIDN output has an invalid PFM size")
        width, height = map(int, dimensions)
        scale = float(handle.readline().decode('ascii').strip())
        endian = '<' if scale < 0 else '>'
        data = np.fromfile(handle, dtype=endian + 'f4')
    expected = width * height * 3
    if data.size != expected:
        raise RuntimeError(f"OIDN output contains {data.size} values; expected {expected}")
    return data.reshape((height, width, 3)).astype(np.float32, copy=False)
