from __future__ import annotations

import ctypes
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .model_policy import chat_messages, model_family, supports_thinking_switch, task_sampling, TRANSLATEGEMMA_TEMPLATE
from . import diagnostics
from .scene_voice import PROMPT_REVISION

GIB = 1024 ** 3
MIB = 1024 ** 2
LEGACY_LOCAL_NOTES = (
    "Translate as a professional visual-novel localization editor. Use natural literary Russian, preserve character voice, "
    "emotion, jokes, slang, profanity and adult content without censorship. Do not add explanations. /no_think"
)
DEFAULT_LOCAL_NOTES = "Сохраняй авторский смысл, голос персонажей, юмор и откровенность сцен. Не добавляй объяснения от переводчика."


@dataclass(frozen=True)
class ModelSpec:
    key: str
    title: str
    repo: str
    filename: str
    size_bytes: int
    sha256: str
    min_ram_gb: int
    recommended_vram_gb: int
    context_size: int
    batch_segments: int
    batch_chars: int
    quality: str
    thinking: bool = False

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.filename}?download=true"


MODEL_SPECS: dict[str, ModelSpec] = {
    "hy-mt2-7b-q6": ModelSpec(
        "hy-mt2-7b-q6", "Hy-MT2 7B Q6_K (Translation)", "tencent/Hy-MT2-7B-GGUF",
        "HY-MT2-7B-Q6_K.gguf", 6_160_000_000,
        "88ef0aba59952a4cfe4be36cb5baf797dbb370bc60e9dcbd7297036021e52831",
        16, 9, 6144, 8, 2200, "Специализированный перевод; кандидат для GPU 10 ГБ. Сначала сравните короткую сцену."),
    "hy-mt2-7b-q4": ModelSpec(
        "hy-mt2-7b-q4", "Hy-MT2 7B Q4_K_M (Translation Lite)", "tencent/Hy-MT2-7B-GGUF",
        "Hy-MT2-7B-Q4_K_M.gguf", 4_620_000_000,
        "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
        16, 7, 6144, 8, 2200, "Меньше памяти, чем Q6; качество на вашей игре требует сравнения."),
    "qwen3.5-9b": ModelSpec(
        "qwen3.5-9b", "Qwen3.5 9B Q4_K_M (Universal)", "unsloth/Qwen3.5-9B-GGUF",
        "Qwen3.5-9B-Q4_K_M.gguf", 5_680_000_000,
        "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
        16, 8, 8192, 8, 2200, "Универсальная модель для перевода, профилей и редактуры; reasoning отключён."),
    "translategemma-12b": ModelSpec(
        "translategemma-12b", "TranslateGemma 12B Q4_K_M (Experimental)", "NikolayKozloff/translategemma-12b-it-Q4_K_M-GGUF",
        "translategemma-12b-it-q4_k_m.gguf", 7_300_000_000,
        "f02d7086c23eedccf9ce2ca837bcea6055d9af3cbe8da18ea5f489b86fe680d1",
        24, 10, 2048, 1, 600, "Эксперимент: короткие запросы, нестандартный Gemma-шаблон. Профили/редактура не подтверждены Google."),
    "qwen3-1.7b": ModelSpec(
        "qwen3-1.7b", "Qwen3 1.7B Q4_K_M (Lite)", "ggml-org/Qwen3-1.7B-GGUF",
        "Qwen3-1.7B-Q4_K_M.gguf", 1_280_000_000,
        "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5",
        8, 0, 4096, 2, 2200, "Быстро, минимальные требования; качество ниже на сложных сценах."
    ),
    "qwen3-4b": ModelSpec(
        "qwen3-4b", "Qwen3 4B Q4_K_M (Balanced)", "ggml-org/Qwen3-4B-GGUF",
        "Qwen3-4B-Q4_K_M.gguf", 2_500_000_000,
        "ab27b9bfa375a178d6cba48f3ad892b94b7739659dcc7aae8058ce0ffed6b328",
        12, 4, 6144, 4, 4200, "Хороший универсальный вариант для большинства ПК."
    ),
    "qwen3-8b": ModelSpec(
        "qwen3-8b", "Qwen3 8B Q4_K_M (Quality)", "Qwen/Qwen3-8B-GGUF",
        "Qwen3-8B-Q4_K_M.gguf", 5_030_000_000,
        "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
        16, 7, 8192, 6, 6200, "Прежний универсальный вариант; сохранён для сравнения и совместимости."
    ),
    "qwen3-14b": ModelSpec(
        "qwen3-14b", "Qwen3 14B Q4_K_M (Max)", "ggml-org/Qwen3-14B-GGUF",
        "Qwen3-14B-Q4_K_M.gguf", 9_000_000_000,
        "5ff1fe7a07aebc8d090682d01b17cf268a1b4680c6477050ce75a600aecb9efb",
        28, 14, 8192, 6, 6200, "Высокое качество, но медленнее и требует много памяти."
    ),
    "deepseek-r1-7b": ModelSpec(
        "deepseek-r1-7b", "DeepSeek-R1-Distill-Qwen 7B Q4_K_M (Experimental)",
        "unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf", 4_680_000_000,
        "78272d8d32084548bd450394a560eb2d70de8232ab96a725769b1f9171235c1c",
        16, 7, 6144, 3, 3600,
        "Экспериментально: reasoning-модель, обычно медленнее Qwen3 для массового перевода.", True
    ),
}


@dataclass
class GPUInfo:
    name: str
    vram_bytes: int = 0
    vendor: str = "unknown"
    free_vram_bytes: int = 0
    used_vram_bytes: int = 0

    @property
    def effective_vram_bytes(self) -> int:
        return self.free_vram_bytes if self.free_vram_bytes > 0 or self.used_vram_bytes > 0 else self.vram_bytes


@dataclass
class HardwareInfo:
    os_name: str
    arch: str
    cpu: str
    logical_cores: int
    ram_bytes: int
    gpus: list[GPUInfo]

    @property
    def max_vram_bytes(self) -> int:
        return max((g.vram_bytes for g in self.gpus), default=0)

    @property
    def max_free_vram_bytes(self) -> int:
        values = [g.free_vram_bytes for g in self.gpus if g.free_vram_bytes > 0]
        return max(values, default=0)

    @property
    def effective_vram_bytes(self) -> int:
        return max((g.effective_vram_bytes for g in self.gpus), default=0)

    @property
    def has_nvidia(self) -> bool:
        return any(g.vendor == "nvidia" for g in self.gpus)

    @property
    def has_amd_or_intel_gpu(self) -> bool:
        return any(g.vendor in {"amd", "intel"} for g in self.gpus)


def _windows_ram_bytes() -> int:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    state = MEMORYSTATUSEX()
    state.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        return int(state.ullTotalPhys)
    return 0


def _ram_bytes() -> int:
    if os.name == "nt":
        try:
            return _windows_ram_bytes()
        except Exception:
            return 0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * size)
    except Exception:
        return 0


def _nvidia_gpus() -> list[GPUInfo]:
    try:
        cp = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except Exception:
        return []
    if cp.returncode != 0:
        return []
    out: list[GPUInfo] = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            total_mib = int(float(parts[1]))
            free_mib = int(float(parts[2])) if len(parts) > 2 else 0
            used_mib = int(float(parts[3])) if len(parts) > 3 else max(0, total_mib - free_mib)
        except Exception:
            total_mib = free_mib = used_mib = 0
        out.append(GPUInfo(parts[0], total_mib * MIB, "nvidia", free_mib * MIB, used_mib * MIB))
    return out


def _windows_video_controllers() -> list[GPUInfo]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    )
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=10)
        if cp.returncode != 0 or not cp.stdout.strip():
            return []
        value = json.loads(cp.stdout)
        rows = value if isinstance(value, list) else [value]
    except Exception:
        return []
    result = []
    for row in rows:
        name = str((row or {}).get("Name") or "Unknown GPU")
        low = name.lower()
        vendor = "nvidia" if "nvidia" in low else "amd" if ("amd" in low or "radeon" in low) else "intel" if "intel" in low else "unknown"
        try:
            vram = int((row or {}).get("AdapterRAM") or 0)
        except Exception:
            vram = 0
        result.append(GPUInfo(name, vram, vendor))
    return result


def detect_hardware() -> HardwareInfo:
    gpus = _nvidia_gpus()
    if not gpus:
        gpus = _windows_video_controllers()
    cpu = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or "Unknown CPU"
    return HardwareInfo(
        os_name=platform.system() or os.name,
        arch=platform.machine() or "unknown",
        cpu=cpu,
        logical_cores=max(1, os.cpu_count() or 1),
        ram_bytes=_ram_bytes(),
        gpus=gpus,
    )


def recommended_backend(hw: HardwareInfo) -> str:
    if hw.has_nvidia:
        return "cuda"
    if hw.has_amd_or_intel_gpu:
        return "vulkan"
    return "cpu"


def recommend_model(hw: HardwareInfo) -> ModelSpec:
    ram = hw.ram_bytes / GIB
    vram = hw.effective_vram_bytes / GIB
    # Prioritize a model that can run comfortably, not merely fit by a few MB.
    if ram >= 16 and vram >= 8.5:
        return MODEL_SPECS["hy-mt2-7b-q6"]
    if ram >= 16 and vram >= 6.5:
        return MODEL_SPECS["hy-mt2-7b-q4"]
    if (vram >= 4 and ram >= 12) or ram >= 12:
        return MODEL_SPECS["qwen3-4b"]
    return MODEL_SPECS["qwen3-1.7b"]


def recommended_parallel_capacity(hw: HardwareInfo, spec: ModelSpec, backend: str) -> int:
    """Conservative maximum number of simultaneous llama-server slots to test/use.

    Stage 9.2 prefers aggregate throughput but never sizes concurrency merely from
    CPU core count. GPU VRAM/model size determines how many active sequences can
    run comfortably. The one-time benchmark may still choose fewer workers.
    """
    if backend == "cpu":
        return 1
    vram = hw.effective_vram_bytes / GIB
    if model_family({"model": spec.key}) in {"hy_mt2", "qwen3_5", "translategemma"}:
        return 2 if vram >= spec.recommended_vram_gb + 4 else 1
    if spec.key == "qwen3-1.7b":
        return 4 if vram >= 6 else 2
    if spec.key == "qwen3-4b":
        return 3 if vram >= 9 else 2 if vram >= 5 else 1
    if spec.key in {"qwen3-8b", "deepseek-r1-7b"}:
        return 2 if vram >= 9 else 1
    if spec.key == "qwen3-14b":
        return 2 if vram >= 18 else 1
    return 1


def recommended_context_per_slot(spec: ModelSpec, parallel_capacity: int) -> int:
    """Useful context available to each active llama-server slot.

    llama.cpp --ctx-size is shared by parallel sequences. Stage 9.2 mistakenly
    treated it as per-slot, so 6144 with --parallel 2 became ~3072 per request.
    Stage 9.3 sizes the server total from an explicit per-slot target.
    """
    if spec.key == "translategemma-12b":
        return 2048
    if model_family({"model": spec.key}) in {"hy_mt2", "qwen3_5"}:
        return 6144
    if spec.key == "qwen3-1.7b":
        return 4096
    if parallel_capacity >= 2:
        if spec.key in {"qwen3-8b", "deepseek-r1-7b"}:
            return 5120
        return 4096
    return min(spec.context_size, 6144 if spec.context_size >= 6144 else spec.context_size)


def recommended_runtime_context(spec: ModelSpec, parallel_capacity: int) -> int:
    """Total --ctx-size passed to llama-server, not per-slot context."""
    capacity = max(1, int(parallel_capacity or 1))
    return recommended_context_per_slot(spec, capacity) * capacity

def required_free_bytes(model: ModelSpec, *, engine_installed: bool = False) -> int:
    # Model downloads to *.part and is renamed in place, so it does not require
    # 2x model space. Leave room for llama.cpp archives/extraction, logs and cache.
    overhead = 900 * MIB if engine_installed else 2 * GIB
    safety = max(1 * GIB, int(model.size_bytes * 0.15))
    return model.size_bytes + overhead + safety


def disk_info(path: str | Path) -> dict[str, int]:
    p = Path(path).expanduser().resolve()
    anchor = p
    while not anchor.exists() and anchor.parent != anchor:
        anchor = anchor.parent
    usage = shutil.disk_usage(anchor)
    return {"total": int(usage.total), "used": int(usage.used), "free": int(usage.free)}


def enumerate_storage_locations() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if os.name == "nt":
        for code in range(ord("C"), ord("Z") + 1):
            root = Path(chr(code) + ":\\")
            if not root.exists():
                continue
            try:
                info = disk_info(root)
            except Exception:
                continue
            result.append({"path": str(root), **info})
    else:
        result.append({"path": str(Path.home()), **disk_info(Path.home())})
    return result


def human_bytes(value: int) -> str:
    n = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or suffix == "TB":
            return f"{n:.2f} {suffix}"
        n /= 1024
    return f"{n:.2f} TB"


def model_runtime_estimate(model: ModelSpec, hw: HardwareInfo) -> dict[str, Any]:
    vram = hw.effective_vram_bytes / GIB
    ram = hw.ram_bytes / GIB
    backend = recommended_backend(hw)
    if backend == "cuda" and vram >= model.recommended_vram_gb:
        mode = "GPU"
    elif backend in {"cuda", "vulkan"} and vram >= 2:
        mode = "Hybrid GPU+RAM"
    else:
        mode = "CPU/RAM"
    comfortable = ram >= model.min_ram_gb and (model.recommended_vram_gb == 0 or vram >= max(0, model.recommended_vram_gb - 2) or ram >= model.min_ram_gb + 8)
    return {"backend": backend, "mode": mode, "comfortable": comfortable}


def load_settings(settings_path: str | Path) -> dict[str, Any]:
    p = Path(settings_path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_settings(settings_path: str | Path, value: dict[str, Any]) -> None:
    p = Path(settings_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    persistent = {key: val for key, val in value.items() if key not in {"runtime_api_key", "api_key_env", "dynamic_port"}}
    if value.get("dynamic_port"):
        persistent.pop("port", None)
    tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(persistent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def model_path(home: str | Path, spec: ModelSpec) -> Path:
    return Path(home) / "models" / spec.filename


def engine_dir(home: str | Path, backend: str) -> Path:
    return Path(home) / "engine" / backend


def find_server_executable(home: str | Path, backend: str) -> Path | None:
    root = engine_dir(home, backend)
    names = ["llama-server.exe", "llama-server"]
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
        hits = list(root.rglob(name)) if root.exists() else []
        if hits:
            return hits[0]
    return None


def installation_status(settings: dict[str, Any]) -> dict[str, Any]:
    home_raw = settings.get("home")
    key = settings.get("model_key")
    backend = settings.get("backend")
    if not home_raw or key not in MODEL_SPECS or backend not in {"cpu", "cuda", "vulkan"}:
        return {"configured": False, "ready": False}
    home = Path(home_raw)
    spec = MODEL_SPECS[key]
    mp = model_path(home, spec)
    exe = find_server_executable(home, backend)
    model_present = mp.is_file() and mp.stat().st_size >= int(spec.size_bytes * 0.95)
    return {
        "configured": True,
        "ready": bool(model_present and exe and exe.is_file()),
        "home": str(home), "model_key": key, "model_path": str(mp),
        "model_present": model_present, "engine_present": bool(exe),
        "engine_path": str(exe) if exe else "", "backend": backend,
    }


def _download(url: str, dest: Path, *, expected_sha256: str = "", expected_bytes: int = 0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "RPTS/8.1 (+local-ai-installer)"}
    req = urllib.request.Request(url, headers=headers)
    hasher = hashlib.sha256()
    downloaded = 0
    last_print = 0.0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or expected_bytes or 0)
            while True:
                from .runtime_control import check_cancelled
                check_cancelled()
                chunk = resp.read(4 * MIB)
                if not chunk:
                    break
                fh.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_print >= 0.8:
                    if total:
                        print(f"    {human_bytes(downloaded)} / {human_bytes(total)} ({downloaded * 100 / total:.1f}%)", end="\r", flush=True)
                    else:
                        print(f"    {human_bytes(downloaded)}", end="\r", flush=True)
                    last_print = now
        print(" " * 90, end="\r")
        if expected_bytes and downloaded < int(expected_bytes * 0.95):
            raise RuntimeError(f"download too small: {downloaded} < expected {expected_bytes}")
        if expected_sha256 and hasher.hexdigest().lower() != expected_sha256.lower():
            raise RuntimeError(f"SHA256 mismatch for {dest.name}")
        part.replace(dest)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _github_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RPTS/8.1"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _read_url_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "RPTS/8.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace").strip()


def _github_latest_release() -> dict[str, Any]:
    return _github_json("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest")


def _release_has_windows_binaries(release: dict[str, Any]) -> bool:
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "").lower()
        if name.startswith("llama-") and "-bin-win-" in name and "x64" in name and name.endswith(".zip"):
            return True
    return False


def _nightly_tag_from_release(release: dict[str, Any], *, read_text=_read_url_text) -> str:
    for asset in release.get("assets") or []:
        if str(asset.get("name") or "").lower() == "nightly-tag.txt":
            url = str(asset.get("browser_download_url") or "")
            if not url:
                break
            tag = read_text(url).strip()
            if re.fullmatch(r"b\d+", tag):
                return tag
            raise RuntimeError(f"Unexpected llama.cpp nightly tag: {tag!r}")
    body = str(release.get("body") or "")
    match = re.search(r"Nightly build:\s*\[?(b\d+)\]?", body, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _resolve_binary_release(release: dict[str, Any], *, fetch_release=None, read_text=_read_url_text) -> dict[str, Any]:
    if _release_has_windows_binaries(release):
        return release
    tag = _nightly_tag_from_release(release, read_text=read_text)
    if not tag:
        name = release.get("tag_name") or release.get("name") or "unknown"
        raise RuntimeError(f"llama.cpp release {name} has no Windows binaries and no nightly build pointer")
    if fetch_release is None:
        fetch_release = lambda t: _github_json(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/" + urllib.parse.quote(t, safe="")
        )
    build = fetch_release(tag)
    if not _release_has_windows_binaries(build):
        raise RuntimeError(f"llama.cpp build release {tag} does not contain Windows x64 binaries")
    return build


def _cuda_version(name: str) -> tuple[int, int] | None:
    match = re.search(r"cuda-(\d+)\.(\d+)", name.lower())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _select_release_assets(release: dict[str, Any], backend: str) -> list[dict[str, Any]]:
    assets = release.get("assets") or []
    def candidates(patterns: list[str], reject: list[str] | None = None):
        reject = reject or []
        result = []
        for a in assets:
            name = str(a.get("name") or "").lower()
            if all(p in name for p in patterns) and not any(x in name for x in reject) and name.endswith(".zip"):
                result.append(a)
        return result
    if backend == "cuda":
        main = candidates(["llama-", "bin-win-cuda-", "x64"], ["cudart", "arm64"])
        runtime = candidates(["cudart", "bin-win-cuda-", "x64"], ["arm64"])
        by_version: dict[tuple[int, int], dict[str, Any]] = {}
        for asset in main:
            version = _cuda_version(str(asset.get("name") or ""))
            if version:
                by_version[version] = asset
        if by_version:
            cuda12 = sorted((v for v in by_version if v[0] == 12), reverse=True)
            chosen = cuda12[0] if cuda12 else sorted(by_version, reverse=True)[0]
            selected = [by_version[chosen]]
            matching_runtime = [a for a in runtime if _cuda_version(str(a.get("name") or "")) == chosen]
            if matching_runtime:
                selected.append(matching_runtime[0])
            return selected
    elif backend == "vulkan":
        found = candidates(["llama-", "bin-win-vulkan-x64"], ["arm64"])
        if found:
            return [found[0]]
    elif backend == "cpu":
        found = candidates(["llama-", "bin-win-cpu-x64"], ["arm64"])
        if found:
            return [found[0]]
    tag = release.get("tag_name") or release.get("name") or "unknown"
    raise RuntimeError(f"llama.cpp build {tag} does not contain Windows {backend} x64 asset")


def _safe_extract_zip(archive: Path, dest: Path, *, max_total_bytes: int = 2 * GIB, max_files: int = 5000) -> None:
    """Extract a trusted release archive without path traversal or expansion bombs."""
    dest = dest.resolve()
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise RuntimeError(f"Archive contains too many files: {len(infos)}")
        for info in infos:
            total += int(info.file_size or 0)
            if total > max_total_bytes:
                raise RuntimeError(f"Archive expands beyond safety limit: {human_bytes(total)}")
            target = (dest / info.filename).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise RuntimeError(f"Unsafe archive path: {info.filename}") from None
        zf.extractall(dest)


def _rotate_log(path: Path, *, max_bytes: int = 8 * MIB, keep: int = 2) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for i in range(keep, 0, -1):
        src = path.with_name(path.name + f".{i}")
        if not src.exists():
            continue
        if i >= keep:
            src.unlink(missing_ok=True)
        else:
            src.replace(path.with_name(path.name + f".{i + 1}"))
    path.replace(path.with_name(path.name + ".1"))


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def prepare_runtime_server(settings: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(settings)
    runtime["port"] = find_free_port()
    runtime["runtime_api_key"] = secrets.token_urlsafe(32)
    runtime["api_key_env"] = "RPTS_LOCAL_AI_KEY"
    runtime["dynamic_port"] = True
    os.environ["RPTS_LOCAL_AI_KEY"] = runtime["runtime_api_key"]
    return runtime


def install_engine(home: str | Path, backend: str) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Automatic llama.cpp binary installation is currently implemented for Windows x64 only")
    if backend not in {"cpu", "cuda", "vulkan"}:
        raise ValueError(backend)
    home = Path(home)
    dest = engine_dir(home, backend)
    staging = dest.with_name(dest.name + ".new")
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    stable_release = _github_latest_release()
    release = _resolve_binary_release(stable_release)
    stable_tag = stable_release.get("tag_name") or stable_release.get("name") or "unknown"
    build_tag = release.get("tag_name") or release.get("name") or "unknown"
    if release is not stable_release:
        print(f"[INFO] llama.cpp {stable_tag}: binaries are published in build {build_tag}")
    selected = _select_release_assets(release, backend)
    downloads = home / "downloads"
    try:
        for asset in selected:
            name = str(asset["name"])
            url = str(asset["browser_download_url"])
            archive = downloads / name
            print(f"[DOWNLOAD] llama.cpp {name}")
            _download(url, archive)
            _safe_extract_zip(archive, staging)
            archive.unlink(missing_ok=True)
        # Validate staging before touching the active engine.
        names = list(staging.rglob("llama-server.exe")) + list(staging.rglob("llama-server"))
        if not names:
            raise RuntimeError("llama-server executable not found after extraction")
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.replace(old)
        staging.replace(dest)
        exe = find_server_executable(home, backend)
        if not exe:
            raise RuntimeError("llama-server executable not found after activation")
        shutil.rmtree(old, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if old.exists():
            shutil.rmtree(dest, ignore_errors=True)
            old.replace(dest)
        raise
    return {
        "release": stable_tag, "build_release": build_tag, "server": str(exe),
    }


def install_model(home: str | Path, spec: ModelSpec) -> Path:
    home = Path(home)
    dest = model_path(home, spec)
    manifest = dest.with_suffix(dest.suffix + ".verified.json")
    if dest.is_file() and dest.stat().st_size >= int(spec.size_bytes * 0.95):
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
            st = dest.stat()
            if (meta.get("sha256") == spec.sha256 and int(meta.get("size") or 0) == st.st_size
                    and int(meta.get("mtime_ns") or 0) == st.st_mtime_ns):
                return dest
            # Verify only when file metadata changed or old installs have no manifest.
            print(f"[VERIFY] SHA-256 {spec.title}...")
            h = hashlib.sha256()
            with dest.open("rb") as fh:
                for chunk in iter(lambda: fh.read(8 * MIB), b""):
                    h.update(chunk)
            if h.hexdigest().lower() == spec.sha256.lower():
                st = dest.stat()
                manifest.write_text(json.dumps({"sha256": spec.sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns}, indent=2), encoding="utf-8")
                return dest
        except Exception:
            pass
        dest.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
    print(f"[DOWNLOAD] {spec.title} ({human_bytes(spec.size_bytes)})")
    _download(spec.url, dest, expected_sha256=spec.sha256, expected_bytes=spec.size_bytes)
    st = dest.stat()
    manifest.write_text(json.dumps({"sha256": spec.sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns}, indent=2), encoding="utf-8")
    return dest


def make_translation_config(
    spec: ModelSpec, *, port: int = 8091, hw: HardwareInfo | None = None, backend: str | None = None,
) -> dict[str, Any]:
    hw = hw or detect_hardware()
    backend = backend or recommended_backend(hw)
    workers = recommended_parallel_capacity(hw, spec, backend)
    tiny = spec.key == "qwen3-1.7b"
    small = spec.key == "qwen3-4b"
    reasoning = spec.thinking
    experimental = spec.key == "translategemma-12b"
    native = model_family({"model": spec.key}) == "hy_mt2"
    story_limit = 1 if experimental or native else 2 if tiny else 4 if small or reasoning else 8
    mode_limits = {
        "story": story_limit, "dialogue": story_limit, "narration": story_limit,
        "ui": 1 if experimental or native else 4 if tiny else 6 if small or reasoning else 10,
    }
    return {
        "source_language": "auto",
        "target_language": "Russian",
        "target_language_code": "ru",
        "renpy_language": "russian",
        "provider": {
            "type": "openai_compatible", "model": "rpts-local",
            "base_url": f"http://127.0.0.1:{port}/v1", "endpoint": "/chat/completions",
            "api_key_env": "RPTS_LOCAL_AI_KEY", "timeout_seconds": 600,
            **task_sampling({"model": spec.key}), "constrain_russian": not (reasoning or native),
            "max_retries": 1, "retry_delay_seconds": 1,
            "structured_json": True, "compact_json": not native, "response_protocol": "single_text" if native else "anchored_keys",
            "model_identity": f"{spec.key}:{spec.sha256}",
            "enable_thinking": False if supports_thinking_switch({"model": spec.key}) else None, "cache_prompt": True,
            "context_limit_tokens": recommended_context_per_slot(spec, workers), "token_preflight": True,
            "max_output_tokens": 512 if experimental else 3072 if reasoning else 2048, "output_token_factor": 2.0,
            "output_token_min": 1536 if reasoning else 256 if native else 160,
        },
        "batch": {
            "max_segments": mode_limits["dialogue"],
            "max_chars": 600 if experimental else 1000 if tiny else 1600 if small or reasoning else 2200,
            "mode_max_segments": mode_limits,
        },
        "context": {"previous": 1 if experimental else 2 if tiny else 4, "next": 1 if experimental or tiny else 3},
        "translation": {
            "use_existing_translation_memory": False,
            "include_statuses": ["pending", "error"],
            "refresh_legacy_ai": True, "skip_nonlinguistic": True,
            "channels": ["semantic", "localization"],
            "stop_on_batch_error": True, "progress_every_batches": 1,
            "split_on_response_error": True,
            "continue_on_single_response_error": True,
            "no_reasoning": not reasoning,
            "concurrent_batches": workers,
        },
        "validation": {"reject_empty": True, "max_length_ratio": 8.0, "min_source_chars_for_ratio": 20},
        "quality": {
            "enabled": True,
            "scene_aware_batches": True,
            "shared_scene_context": True,
            "russian_localization": True,
            "profile_sample_lines": 4 if experimental else 12,
            "profile_max_chars": 1200 if experimental else 7200,
            "profile_gender_min_confidence": 0.85,
            "profile_output_tokens": 768 if experimental else 2048,
            "profile_protocol": "11.7",
            "translation_protocol": PROMPT_REVISION,
            "text_mode": "balanced",
            "native_free_review": False,
            "editorial_pass": False,
            "fidelity_pass": False,
            "combined_review": False,
            "stage_checkpoints": True,
            "stop_on_profile_error": True,
            "scene_context_max_chars": 500 if experimental else 4200,
            "deterministic_gender_fallback": False,
            "repair_attempts": 2,
            "use_english_reference": True,
            "auto_profile_characters": True,
            "repair_validation_errors": True,
            "max_profile_items_per_call": 8,
            "character_profiles_file": "work/quality/character_profiles.json",
            "quality_dir": "work/quality"
        },
        "performance": {
            "profile_version": "11.18",
            "auto_benchmark": True,
            "recommended_workers": workers,
            "runtime_context_size": recommended_runtime_context(spec, workers),
            "runtime_context_per_slot": recommended_context_per_slot(spec, workers),
            "compact_json": not native,
            "shared_scene_context": True,
        },
        "glossary": {},
        "notes": DEFAULT_LOCAL_NOTES,
    }


def server_command(settings: dict[str, Any], hw: HardwareInfo | None = None) -> list[str]:
    status = installation_status(settings)
    if not status.get("ready"):
        raise RuntimeError("Local AI is not fully installed")
    spec = MODEL_SPECS[str(settings["model_key"])]
    exe = str(status["engine_path"])
    mp = str(status["model_path"])
    backend = str(settings["backend"])
    port = int(settings.get("port") or 8091)
    hw = hw or detect_hardware()
    threads = max(2, min(16, hw.logical_cores - 1 if hw.logical_cores > 2 else hw.logical_cores))
    max_capacity = int(settings.get("parallel_capacity") or recommended_parallel_capacity(hw, spec, backend))
    max_capacity = max(1, min(4, max_capacity))
    bench_key = f"11.18:{settings.get('model_key')}:{backend}:{max_capacity}"
    if settings.get("performance_benchmark_key") == bench_key and settings.get("performance_workers"):
        capacity = max(1, min(max_capacity, int(settings.get("performance_workers") or 1)))
    else:
        capacity = max_capacity
    # --ctx-size is shared across --parallel slots. Compute it from the
    # *actual* number of active slots so every request gets enough room.
    per_slot_ctx = recommended_context_per_slot(spec, capacity)
    ctx_size = per_slot_ctx * capacity
    cmd = [
        exe, "--model", mp, "--host", "127.0.0.1", "--port", str(port),
        "--alias", "rpts-local", "--ctx-size", str(ctx_size), "--threads", str(threads),
        "--parallel", str(capacity), "--jinja",
    ]
    if supports_thinking_switch({"model": spec.key}):
        cmd += ["--chat-template-kwargs", '{"enable_thinking":false}']
    if spec.key == "translategemma-12b":
        template = Path(settings["home"]).resolve() / "templates" / "translategemma-rpts-11.16.jinja"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(TRANSLATEGEMMA_TEMPLATE, encoding="utf-8")
        cmd += ["--chat-template-file", str(template)]
    api_key = str(settings.get("runtime_api_key") or "")
    if api_key:
        cmd += ["--api-key", api_key]
    if backend == "cpu":
        cmd += ["--n-gpu-layers", "0"]
    else:
        cmd += ["--n-gpu-layers", "auto"]
    return cmd


def wait_for_server(port: int, timeout: float = 180.0, *, api_key: str = "", proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/v1/models"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ids = [str(x.get("id") or "") for x in (data.get("data") or []) if isinstance(x, dict)]
            if "rpts-local" in ids or any("qwen" in x.lower() or "rpts" in x.lower() for x in ids):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_server(settings: dict[str, Any], log_path: str | Path, hw: HardwareInfo | None = None) -> subprocess.Popen:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(log_path)
    dynamic = bool(settings.get("dynamic_port", True))
    last_error = ""
    for attempt in range(3 if dynamic else 1):
        if int(settings.get("port") or 0) <= 0 or (dynamic and attempt > 0):
            settings["port"] = find_free_port()
        if not settings.get("runtime_api_key"):
            settings["runtime_api_key"] = secrets.token_urlsafe(32)
        env_name = str(settings.get("api_key_env") or "RPTS_LOCAL_AI_KEY")
        os.environ[env_name] = str(settings["runtime_api_key"])
        cmd = server_command(settings, hw=hw)
        diagnostics.remember_secrets([str(settings["runtime_api_key"])])
        diagnostics.event("server_start", attempt=attempt + 1, command=cmd, settings=settings, log=str(log_path))
        log = log_path.open("a", encoding="utf-8", errors="replace")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(Path(cmd[0]).parent), creationflags=flags)
        log.close()
        port = int(settings["port"])
        if wait_for_server(port, api_key=str(settings.get("runtime_api_key") or ""), proc=proc):
            diagnostics.event("server_ready", port=port, server_pid=proc.pid)
            try:
                check_model_capabilities(settings, log_path.parent / "model_capabilities.json", hw=hw)
            except Exception:
                stop_server(proc)
                raise
            return proc
        rc = proc.poll()
        last_error = f"llama-server exited with code {rc}" if rc is not None else "readiness timeout"
        diagnostics.event("server_start_failed", error=last_error, attempt=attempt + 1)
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        if dynamic and attempt < 2:
            continue
        break
    raise RuntimeError(f"llama-server did not become ready ({last_error}). See {log_path}")


def _benchmark_completion(port: int, timeout: float = 60.0, variant: int = 0, *, model_key: str = "qwen3-8b") -> None:
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    sample = [
        "The room became quiet when the door opened.",
        "I knew we should not stay here for long.",
        "Are you sure this is the right way?",
        "He looked away and did not answer.",
        "We can talk about it after we get home.",
        "Something about this place feels strangely familiar.",
        "Please don't make me repeat myself.",
        "The rain finally stopped before sunrise.",
    ]
    if model_key == "translategemma-12b":
        sample = sample[:1]
    schema = {
        "type": "object",
        "properties": {
            "t": {
                "type": "array", "minItems": len(sample), "maxItems": len(sample),
                "items": {"type": "string", "minLength": 1},
            }
        },
        "required": ["t"], "additionalProperties": False,
    }
    reasoning = bool(MODEL_SPECS.get(model_key) and MODEL_SPECS[model_key].thinking)
    body = {
        "model": "rpts-local", **{k:v for k,v in task_sampling({"model": model_key}).items() if v is not None}, "max_tokens": 1536 if reasoning else 384, "cache_prompt": True,
        "messages": chat_messages({"model_identity": model_key, "enable_thinking": None if reasoning else False},
            'Translate each item to natural Russian. Return only JSON {"t":["translation",...]} in the input order.',
            json.dumps({"variant": variant, "items": sample}, ensure_ascii=False, separators=(",", ":"))),
        "response_format": {"type": "json_object", "schema": schema},
    }
    native = model_family({"model": model_key}) == "hy_mt2"
    if native:
        from .native_translation import system_prompt, user_prompt
        body["messages"] = chat_messages({"model": model_key}, system_prompt({}, "story"),
            user_prompt({"text": sample[variant % len(sample)], "speaker": "", "kind": "narration"}))
        body.pop("response_format", None)
    if supports_thinking_switch({"model": model_key}):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers={"Content-Type": "application/json"}; key=os.environ.get("RPTS_LOCAL_AI_KEY", "");
    if key: headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    if not isinstance(payload, dict) or not payload.get("choices"):
        raise RuntimeError("llama-server benchmark returned an unexpected response")
    choice = payload["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("Benchmark output was truncated")
    if native:
        from .providers import _single_text_payload
        _single_text_payload(choice["message"]["content"])
        return
    answer = json.loads(choice["message"]["content"])
    if not isinstance(answer.get("t"), list) or len(answer["t"]) != len(sample) or not all(isinstance(v, str) and v.strip() for v in answer["t"]):
        raise RuntimeError("Benchmark returned incomplete translations")


def benchmark_parallel_workers(settings: dict[str, Any]) -> dict[str, Any]:
    """Measure aggregate local-server throughput for 1..parallel_capacity clients."""
    capacity = max(1, min(4, int(settings.get("parallel_capacity") or 1)))
    port = int(settings.get("port") or 8091)
    model_key = str(settings.get("model_key") or "qwen3-8b")
    if capacity <= 1:
        return {"workers": 1, "results": {"1": {"seconds": 0.0, "requests_per_second": 0.0}}}

    results: dict[str, dict[str, float]] = {}
    best_workers = 1
    best_throughput = 0.0
    # Warm up grammar/model/prompt-cache before timing either candidate.
    _benchmark_completion(port, variant=-1, model_key=model_key)
    for workers in range(1, capacity + 1):
        started = time.perf_counter()
        if workers == 1:
            _benchmark_completion(port, variant=0, model_key=model_key)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rpts-bench") as pool:
                futures = [pool.submit(_benchmark_completion, port, 60.0, i + 1, model_key=model_key) for i in range(workers)]
                for future in futures:
                    future.result()
        elapsed = max(0.001, time.perf_counter() - started)
        throughput = workers / elapsed
        results[str(workers)] = {"seconds": round(elapsed, 3), "requests_per_second": round(throughput, 4)}
        # Require a real aggregate gain, not benchmark noise.
        if workers == 1 or throughput >= best_throughput * 1.08:
            best_workers = workers
            best_throughput = throughput
    return {"workers": best_workers, "results": results}


def stop_server(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def delete_local_ai(settings: dict[str, Any], *, remove_models: bool = True) -> int:
    home_raw = settings.get("home")
    if not home_raw:
        return 0
    home = Path(home_raw)
    if not home.exists():
        return 0
    if remove_models:
        shutil.rmtree(home)
        return 1
    engine = home / "engine"
    if engine.exists():
        shutil.rmtree(engine)
        return 1
    return 0


def check_model_capabilities(settings, report_path, hw=None):
    """Small contract check, not a literary-quality benchmark. No external calls."""
    from .providers import create_provider
    from .translation_engine import _system_prompt, _review_system_prompt, _call_provider_resilient
    from .russian import russian_gender_warning
    spec = MODEL_SPECS[str(settings["model_key"])]
    if model_family({"model": spec.key}) not in {"hy_mt2", "qwen3_5", "translategemma"}:
        return
    report_path = Path(report_path)
    exe = Path(installation_status(settings)["engine_path"])
    signature = [PROMPT_REVISION, spec.sha256, str(exe), exe.stat().st_mtime_ns]
    try:
        old = json.loads(report_path.read_text(encoding="utf-8"))
        if old.get("signature") == signature and old.get("status") == "passed":
            return
    except (OSError, ValueError):
        pass
    cfg = make_translation_config(spec, port=int(settings["port"]), hw=hw, backend=settings["backend"])
    provider = create_provider(cfg["provider"])
    report = {"signature": signature, "model": spec.key, "family": model_family(cfg["provider"]), "status": "running", "checks": [],
              "response_protocol": cfg["provider"]["response_protocol"],
              "scope": "translation response, full response, protected marker, explicit male agreement, JSON profile; not proof of literary quality"}
    print("[MODEL] Проверяю формат ответов, перевод, редактуру и профиль новой модели...", flush=True)
    try:
        item = {"id": "contract", "text": "我回来了，⟪RPT0000⟫。", "kind": "dialogue", "speaker": "hero", "character": {"gender": "male"}, "mode": "dialogue"}
        for stage in ("translation", "combined", "fidelity"):
            prompt = _system_prompt(cfg, "dialogue") if stage == "translation" else _review_system_prompt(cfg, "dialogue", stage)
            result = _call_provider_resilient(provider, system_prompt=prompt, items=[item], split_on_response_error=False)
            answer = result[0]["text"]
            if answer.count("⟪RPT0000⟫") != 1 or not re.search(r"[А-Яа-яЁё]", answer) or re.search(r"[\u3400-\u9fff]", answer) or russian_gender_warning("male", answer):
                raise RuntimeError("Model contract failed: " + stage + ": " + answer)
            report["checks"].append({"task": stage, "answer": answer})
            item["draft"] = answer
            item["review_stage"] = stage
        profile = provider.generate_json(system_prompt="Extract the explicitly stated gender of hero from evidence E0. Return only JSON with gender and evidence_id. Do not infer other facts.",
            user_prompt='E0: hero says "I am a man."', schema={"type":"object", "properties":{"gender":{"type":"string", "enum":["male","female","unknown"]}, "evidence_id":{"type":"string"}}, "required":["gender","evidence_id"], "additionalProperties":False})
        if profile != {"gender": "male", "evidence_id": "E0"}:
            raise RuntimeError("Model did not follow evidence-bound profile contract")
        report["checks"].append({"task": "profile_contract", "answer": profile})
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise RuntimeError("Модель не прошла проверку формата/согласования. Перевод не запущен. Выберите другую модель или обновите llama.cpp; подробности в model_capabilities.json. " + str(exc)) from exc
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostics.event("model_capabilities", **report)
