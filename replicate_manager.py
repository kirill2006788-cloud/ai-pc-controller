import os
import json
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path
from typing import Optional, Callable

import psutil
import replicate
from config import REPLICATE_API_TOKEN, LOCAL_IMAGE_API_URL, FOOOCUS_PATH


_GENERATED_DIR = Path(__file__).parent / "generated"
_GENERATED_DIR.mkdir(exist_ok=True)

IMAGE_MODELS = {
    "flux-schnell": {
        "id": "black-forest-labs/flux-schnell",
        "name": "Flux Schnell (быстрый)",
    },
    "flux-dev": {
        "id": "black-forest-labs/flux-dev",
        "name": "Flux Dev (качество)",
    },
    "flux-pro": {
        "id": "black-forest-labs/flux-pro",
        "name": "Flux Pro (премиум)",
    },
    "sdxl": {
        "id": "stability-ai/sdxl:7762fd07cf82c948538e41f63f77d685e02b063e37e496e96eefd46c929f9bdc",
        "name": "Stable Diffusion XL",
    },
    "playground-v2.5": {
        "id": "playgroundai/playground-v2.5-1024px-aesthetic:a45f82a1382bed5c7aeb861dac7c7d191b0fdf74d8d57c4a0e6ed7d4d0bf7d24",
        "name": "Playground v2.5",
    },
    "kandinsky-2.2": {
        "id": "ai-forever/kandinsky-2.2:ad9d7879fbffa2874e1d909d1d37d9bc682889cc65f2f06571b0f44571583401",
        "name": "Kandinsky 2.2",
    },
    "gpt-image": {
        "id": "openai/gpt-image-1.5",
        "name": "GPT Image 1.5 (лучшее качество)",
    },
    "fooocus-local": {
        "id": "",
        "name": "Fooocus (локально, без цензуры)",
        "local": True,
    },
}

VIDEO_MODELS = {
    "text2video-zero": {
        "id": "cjwbw/text2video-zero:854e8727697a057c525cdb45ab037f64ecca770a1769a926571c01f6d6dc5e05",
        "name": "Text2Video-Zero",
    },
    "animate-diff": {
        "id": "lucataco/animate-diff:beecf59c4aee8d81bf04f0381033dfa10dc16e845b4ae00d281e2fa377e48a9f",
        "name": "AnimateDiff",
    },
    "zeroscope-v2": {
        "id": "anotherjesse/zeroscope-v2-xl:9f747673945c62801b13b84701c783929c0ee784e4748ec062204894dda1a351",
        "name": "Zeroscope v2 XL",
    },
    "ltx-video": {
        "id": "lightricks/ltx-video:983ec70a06fd872ef4c29bb6b728556fc2454125ea701c1e9c4c49e0ca8d4af1",
        "name": "LTX Video",
    },
}

AUDIO_MODELS = {
    "musicgen": {
        "id": "meta/musicgen:671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedbb",
        "name": "MusicGen (Meta)",
    },
    "bark": {
        "id": "suno-ai/bark:b76242b40d67c76ab6742e987628a2a9ac019e11d56ab96c4e91ce03b79b2787",
        "name": "Bark (TTS)",
    },
}

OTHER_MODELS = {
    "llama-3": {
        "id": "meta/meta-llama-3-70b-instruct",
        "name": "Llama 3 70B",
    },
    "remove-bg": {
        "id": "cjwbw/rembg:fb8af171cfa1616ddcf1242c093f9c46bcada5ad4cf6f2fbe8b81b330ec5c003",
        "name": "Remove Background",
    },
    "upscale": {
        "id": "nightmareai/real-esrgan:f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa",
        "name": "Real-ESRGAN (upscale)",
    },
}

ALL_MODELS = {**IMAGE_MODELS, **VIDEO_MODELS, **AUDIO_MODELS, **OTHER_MODELS}


def get_image_model_names() -> list[str]:
    return [m["name"] for m in IMAGE_MODELS.values()]


def get_video_model_names() -> list[str]:
    return [m["name"] for m in VIDEO_MODELS.values()]


class ReplicateManager:
    def __init__(self, token: Optional[str] = None):
        self.token = token or REPLICATE_API_TOKEN
        if self.token:
            os.environ["REPLICATE_API_TOKEN"] = self.token
        self.client = replicate.Client(api_token=self.token) if self.token else None
        self._fooocus_local_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _emit_status(self, callback: Optional[Callable[[str, object], None]], stage: str, detail: object):
        if callback is None:
            return
        try:
            callback(stage, detail)
        except Exception:
            pass

    def _check_http_ready(self, url: str, timeout: float = 3.0) -> bool:
        import requests as _rq

        try:
            resp = _rq.get(url.rstrip("/"), timeout=timeout)
            return int(resp.status_code) < 500
        except Exception:
            return False

    def _wait_until_ready(
        self,
        url: str,
        timeout_sec: int,
        status_callback: Optional[Callable[[str, str], None]] = None,
        service_name: str = "service",
    ) -> bool:
        deadline = time.time() + max(3, int(timeout_sec))
        while time.time() < deadline:
            if self._check_http_ready(url, timeout=3.0):
                self._emit_status(status_callback, "healthy", f"{service_name} готов.")
                return True
            self._emit_status(status_callback, "warming", f"Ожидание готовности {service_name}...")
            time.sleep(2)
        return False

    def _fooocus_env_python(self) -> str:
        if not FOOOCUS_PATH:
            return ""
        py = os.path.join(FOOOCUS_PATH, "fooocus_env", "Scripts", "python.exe")
        return py if os.path.isfile(py) else ""

    def _cleanup_stale_fooocus_helpers(self, older_than_sec: int = 90) -> int:
        now = time.time()
        killed = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if not cmdline:
                    continue
                cmd_text = " ".join(str(x) for x in cmdline)
                if "_fooocus_helper.py" not in cmd_text:
                    continue
                age = now - float(proc.info.get("create_time") or now)
                if age < older_than_sec:
                    continue
                proc.kill()
                killed += 1
            except Exception:
                continue
        return killed

    def _get_fooocus_queue_size(self, url: str) -> int | None:
        import requests as _rq

        try:
            resp = _rq.get(f"{url.rstrip('/')}/queue/status", timeout=10)
            data = resp.json()
            val = data.get("queue_size")
            return int(val) if val is not None else None
        except Exception:
            return None

    def _build_fooocus_args_from_config(self, url: str, prompt: str) -> list:
        import requests as _rq

        cfg = _rq.get(f"{url.rstrip('/')}/config", timeout=30).json()
        dependencies = cfg.get("dependencies") or []
        if len(dependencies) <= 67:
            raise RuntimeError("Fooocus config does not expose dependency 67.")
        dep = dependencies[67]
        component_map = {c.get("id"): c for c in (cfg.get("components") or [])}
        values: list = []
        for idx, cid in enumerate(dep.get("inputs") or []):
            comp = component_map.get(cid) or {}
            if comp.get("type") == "state":
                continue
            props = comp.get("props") or {}
            value = props.get("value")
            if len(values) == 1:
                value = prompt
            if props.get("label") == "Image Number":
                value = 1
            values.append(value)
        return values

    def _collect_image_files(self, candidate) -> list[Path]:
        items: list[Path] = []
        try:
            path = Path(str(candidate))
        except Exception:
            return items
        if not path.exists():
            return items
        if path.is_file():
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                items.append(path)
            return items
        if path.is_dir():
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                for child in sorted(path.glob(ext)):
                    if child.is_file():
                        items.append(child)
        return items

    def _run_fooocus_via_venv(
        self,
        prompt: str,
        url: str,
        status_callback: Optional[Callable[[str, object], None]] = None,
    ) -> list[str]:
        py = self._fooocus_env_python()
        if not py:
            return []
        self._emit_status(status_callback, "warming", "Использую Python из Fooocus venv для совместимого API...")
        helper_code = r"""
# Fooocus generation helper.
# Uses raw WebSocket to capture live preview updates from Gradio queue.
import json
import sys
import time
import tempfile
from pathlib import Path
import base64
import hashlib

import requests
import websocket

url = sys.argv[1]
prompt = sys.argv[2]
fooocus_root = Path(sys.argv[3])
fooocus_temp = Path(tempfile.gettempdir()) / "fooocus"
fooocus_temp.mkdir(exist_ok=True)

# Build args for fn67 from live /config (adapts to whatever is installed)
cfg = requests.get(f"{url.rstrip('/')}/config", timeout=30).json()
dep67 = cfg["dependencies"][67]
comp_map = {c["id"]: c for c in cfg.get("components", [])}
values = []
for cid in (dep67.get("inputs") or []):
    comp = comp_map.get(cid) or {}
    if comp.get("type") == "state":
        continue
    props = comp.get("props") or {}
    v = props.get("value")
    if len(values) == 1:       # prompt textbox
        v = prompt
    if props.get("label") == "Image Number":
        v = 1
    values.append(v)

def extract_paths(obj):
    # Recursively pull image file paths from Gradio response
    paths = []
    if obj is None:
        return paths
    if isinstance(obj, dict):
        # Gallery item: {"name": "...", "is_file": True}
        name = obj.get("name") or obj.get("path") or ""
        if isinstance(name, str) and name and Path(name).exists():
            suf = Path(name).suffix.lower()
            if suf in (".png", ".jpg", ".jpeg", ".webp"):
                paths.append(name)
        if "value" in obj:
            paths.extend(extract_paths(obj["value"]))
        return paths
    if isinstance(obj, (list, tuple)):
        for item in obj:
            paths.extend(extract_paths(item))
        return paths
    if isinstance(obj, str) and obj:
        p = Path(obj)
        if p.exists() and p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            paths.append(obj)
    return paths

def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)

def save_preview_from_data(data_obj, iteration):
    # Extract and save preview image from Gradio data payload.
    if not isinstance(data_obj, dict):
        return ""
    val = data_obj.get("value") or []
    if not isinstance(val, list):
        return ""
    for item in val:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if not name or not isinstance(name, str):
            b64_data = item.get("data") or ""
            if isinstance(b64_data, str) and b64_data.startswith("data:image"):
                try:
                    header, b64 = b64_data.split(",", 1)
                    img_bytes = base64.b64decode(b64)
                    h = hashlib.md5(img_bytes).hexdigest()[:12]
                    dest = fooocus_temp / f"ws_preview_{h}_{iteration}.png"
                    dest.write_bytes(img_bytes)
                    emit({"event": "log", "msg": f"[iter {iteration}] saved base64 preview: {dest}"})
                    return str(dest)
                except Exception:
                    pass
        else:
            p = Path(name)
            if p.exists() and p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                emit({"event": "log", "msg": f"[iter {iteration}] found file preview: {name}"})
                return name
    return ""

# fn67 запуск, fn68 блокирующий predict — только результат, без превью
from gradio_client import Client
client = Client(url, serialize=False, verbose=False)
try:
    client.predict(*values, fn_index=67)
    emit({"event": "log", "msg": "fn67 done"})
except Exception as exc:
    emit({"event": "log", "msg": f"fn67 error: {exc}"})

emit({"event": "log", "msg": "Starting fn68 (blocking)..."})
paths = []
try:
    result = client.predict(fn_index=68)
    emit({"event": "log", "msg": f"fn68 done, result type={type(result).__name__}"})
    if result:
        paths = extract_paths(result)
    emit({"event": "log", "msg": f"Extracted {len(paths)} file(s) from result"})
except Exception as exc:
    emit({"event": "log", "msg": f"fn68 error: {exc}"})

if not paths:
    emit({"event": "log", "msg": "Fallback scan of fooocus temp..."})
    if fooocus_temp.exists():
        try:
            candidates = []
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                for f in sorted(fooocus_temp.rglob(ext)):
                    if not f.is_file():
                        continue
                    try:
                        stat = f.stat()
                        if stat.st_mtime < start_ts - 1:
                            continue
                        if stat.st_size < 8 * 1024:
                            continue
                        candidates.append((stat.st_mtime, str(f)))
                    except Exception:
                        continue
            if candidates:
                candidates.sort(reverse=True)
                paths = [p for _, p in candidates[:5]]
            emit({"event": "log", "msg": f"Fallback found {len(paths)} file(s)"})
        except Exception as exc:
            emit({"event": "log", "msg": f"Fallback error: {exc}"})
    if not paths and fooocus_root.exists():
        emit({"event": "log", "msg": "Fallback scan of fooocus outputs..."})
        try:
            outputs_dir = fooocus_root / "outputs"
            if outputs_dir.exists():
                candidates = []
                for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                    for f in sorted(outputs_dir.rglob(ext)):
                        if not f.is_file():
                            continue
                        try:
                            stat = f.stat()
                            if stat.st_mtime < start_ts - 1:
                                continue
                            if stat.st_size < 8 * 1024:
                                continue
                            candidates.append((stat.st_mtime, str(f)))
                        except Exception:
                            continue
                if candidates:
                    candidates.sort(reverse=True)
                    paths = [p for _, p in candidates[:5]]
                emit({"event": "log", "msg": f"Outputs dir fallback found {len(paths)} file(s)"})
        except Exception as exc:
            emit({"event": "log", "msg": f"Outputs dir error: {exc}"})

emit({"event": "result", "paths": paths})
"""
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix="_fooocus_helper.py", delete=False) as tmp:
                tmp.write(helper_code)
                temp_path = tmp.name
            self._emit_status(status_callback, "running", "Fooocus генерирует изображение через совместимый helper...")
            proc = subprocess.Popen(
                [py, temp_path, url, prompt, FOOOCUS_PATH or ""],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=FOOOCUS_PATH or None,
            )
            items = []
            stderr_lines: list[str] = []
            try:
                while True:
                    line = proc.stdout.readline() if proc.stdout else ""
                    if line:
                        payload = None
                        try:
                            payload = json.loads(line.strip())
                        except Exception:
                            payload = None
                        if isinstance(payload, dict):
                            event = payload.get("event")
                            if event == "log":
                                print(f"[Fooocus helper] {payload.get('msg', '')}", file=sys.stderr)
                            elif event == "result":
                                items = payload.get("paths") or []
                    elif proc.poll() is not None:
                        break
                if proc.stderr:
                    stderr_lines = [x.rstrip() for x in proc.stderr.readlines() if x.strip()]
            finally:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            if int(proc.returncode or 0) != 0:
                err_text = "\n".join(stderr_lines).strip()
                raise RuntimeError(err_text or f"helper exit={proc.returncode}")
            saved: list[str] = []
            for idx, item in enumerate(items):
                for file_path in self._collect_image_files(item):
                    ts = int(time.time())
                    dest = _GENERATED_DIR / f"img_fooocus_{ts}_{len(saved)}{file_path.suffix.lower() or '.png'}"
                    dest.write_bytes(file_path.read_bytes())
                    saved.append(str(dest))
            if saved:
                self._emit_status(status_callback, "completed", f"Fooocus завершил генерацию: {len(saved)} файл(ов).")
            return saved
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    def _save_output(self, output, prefix: str = "out", ext: str = "png") -> list[str]:
        """Universal output saver — handles FileOutput, URLs, lists, strings."""
        saved: list[str] = []
        ts = int(time.time())

        items = output if isinstance(output, (list, tuple)) else [output]
        for idx, item in enumerate(items):
            fname = f"{prefix}_{ts}_{idx}.{ext}"
            fpath = _GENERATED_DIR / fname

            if hasattr(item, "read"):
                fpath.write_bytes(item.read())
                saved.append(str(fpath))
            elif isinstance(item, bytes):
                fpath.write_bytes(item)
                saved.append(str(fpath))
            elif isinstance(item, str) and item.startswith("http"):
                import requests as _rq
                resp = _rq.get(item, timeout=120)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "video" in ct or "mp4" in ct:
                    fpath = fpath.with_suffix(".mp4")
                elif "audio" in ct or "wav" in ct or "mpeg" in ct:
                    fpath = fpath.with_suffix(".wav")
                fpath.write_bytes(resp.content)
                saved.append(str(fpath))
            elif isinstance(item, str):
                saved.append(item)

        return saved

    def generate_image(self, prompt: str, model_key: str = "flux-schnell", **kwargs) -> list[str]:
        status_callback = kwargs.pop("status_callback", None)
        info = IMAGE_MODELS.get(model_key, list(IMAGE_MODELS.values())[0])
        if info.get("local", False) or model_key == "fooocus-local":
            self._emit_status(status_callback, "started", "Подключаюсь к локальному Fooocus...")
            return self.generate_image_local(prompt, LOCAL_IMAGE_API_URL or "", status_callback=status_callback)
        if not self.available:
            raise RuntimeError("Replicate API token not configured")
        self._emit_status(status_callback, "running", f"Генерирую изображение через {info.get('name') or model_key}...")
        inp: dict = {"prompt": prompt}

        if model_key == "gpt-image":
            inp["quality"] = "high"
            inp["number_of_images"] = kwargs.get("num_outputs", 1)
            inp["output_format"] = "png"
        else:
            if kwargs.get("negative_prompt"):
                inp["negative_prompt"] = kwargs["negative_prompt"]
            if kwargs.get("width"):
                inp["width"] = kwargs["width"]
            if kwargs.get("height"):
                inp["height"] = kwargs["height"]
            if kwargs.get("num_outputs"):
                inp["num_outputs"] = kwargs["num_outputs"]

        output = self.client.run(info["id"], input=inp)
        return self._save_output(output, prefix="img", ext="png")

    def generate_image_local(
        self,
        prompt: str,
        url: str,
        status_callback: Optional[Callable[[str, str], None]] = None,
    ) -> list[str]:
        """Generate image via local Fooocus (Gradio). Uses Fooocus 2.5.5 API: fn_index 67 (run) + 68 (get result). Returns list of saved file paths."""
        import re
        import requests as _rq
        # If prompt looks like a Cursor/IDE reference or file path, Gradio may treat it as URL and raise "Unknown protocol: ws"
        _prompt = (prompt or "").strip()
        if not _prompt or _prompt.startswith("ws@") or re.match(r"^[A-Za-z]:\\", _prompt) or ("\\" in _prompt and ".txt" in _prompt and "terminals" in _prompt):
            _prompt = "image"  # safe default so gradio_client does not try to open as URL/path
        url = (url or "").rstrip("/")
        if not url:
            raise RuntimeError("LOCAL_IMAGE_API_URL не задан. Запусти Fooocus и укажи в .env: LOCAL_IMAGE_API_URL=http://127.0.0.1:7865")
        self._emit_status(status_callback, "warming", "Проверяю доступность локального Fooocus...")
        if not self._check_http_ready(url, timeout=2.0):
            if not self._wait_until_ready(url, timeout_sec=90, status_callback=status_callback, service_name="Fooocus"):
                raise RuntimeError("Fooocus не отвечает по локальному URL. Убедись, что сервис запущен и полностью загрузился.")
        with self._fooocus_local_lock:
            killed = self._cleanup_stale_fooocus_helpers()
            if killed:
                self._emit_status(status_callback, "warming", f"Очищено зависших локальных задач Fooocus: {killed}.")
            queue_size = self._get_fooocus_queue_size(url)
            if queue_size and queue_size > 0:
                self._emit_status(status_callback, "warming", f"В очереди Fooocus уже есть задач: {queue_size}. Жду освобождения...")
        # Prefer Fooocus's own venv Python/client to avoid gradio_client websocket incompatibility.
            try:
                saved = self._run_fooocus_via_venv(_prompt, url, status_callback=status_callback)
                if saved:
                    return saved
                raise RuntimeError(
                    "Fooocus принял задачу, но не вернул готовые файлы. Проверь локальный интерфейс http://127.0.0.1:7865: "
                    "возможно, модель ещё грузится, очередь занята, или генерация зависла внутри Fooocus."
                )
            except Exception as e:
                self._emit_status(status_callback, "failed", f"Fooocus helper error: {e}")
                raise RuntimeError(
                    f"Локальный Fooocus не смог завершить генерацию через совместимый helper. Ошибка: {e}"
                )
        try:
            from pathlib import Path as _P
            from gradio_client import Client
            client = Client(url, serialize=False, verbose=False)
            self._emit_status(status_callback, "running", "Отправляю запрос в Fooocus...")
            args_67 = self._build_fooocus_args_from_config(url, _prompt)
            client.predict(*args_67, fn_index=67)
            result = None
            out = []
            self._emit_status(status_callback, "running", "Fooocus генерирует изображение...")
            for _ in range(180):
                try:
                    result = client.predict(fn_index=68)
                except Exception:
                    result = None
                out = (result if isinstance(result, (list, tuple)) else [result]) if result is not None else []
                if out:
                    probe_saved = []
                    for item in out:
                        candidates = list(item) if isinstance(item, (list, tuple)) else [item]
                        for candidate in candidates:
                            if isinstance(candidate, dict) and "value" in candidate:
                                val = candidate.get("value")
                                vals = val if isinstance(val, (list, tuple)) else [val]
                                for vv in vals:
                                    probe_saved.extend(self._collect_image_files(vv))
                            else:
                                probe_saved.extend(self._collect_image_files(candidate))
                    if probe_saved:
                        break
                time.sleep(2)
            saved = []
            for item in out:
                if item is None:
                    continue
                candidates = list(item) if isinstance(item, (list, tuple)) else [item]
                for candidate in candidates:
                    if isinstance(candidate, dict) and "value" in candidate:
                        val = candidate.get("value")
                        vals = val if isinstance(val, (list, tuple)) else [val]
                        for vv in vals:
                            for p in self._collect_image_files(vv):
                                ts = int(time.time())
                                dest = _GENERATED_DIR / f"img_fooocus_{ts}_{len(saved)}{p.suffix.lower() or '.png'}"
                                dest.write_bytes(p.read_bytes())
                                saved.append(str(dest))
                    else:
                        path_str = candidate if isinstance(candidate, str) else (getattr(candidate, "path", None) or str(candidate))
                        if not path_str:
                            continue
                        for p in self._collect_image_files(path_str):
                            ts = int(time.time())
                            dest = _GENERATED_DIR / f"img_fooocus_{ts}_{len(saved)}{p.suffix.lower() or '.png'}"
                            dest.write_bytes(p.read_bytes())
                            saved.append(str(dest))
            if saved:
                self._emit_status(status_callback, "completed", f"Fooocus завершил генерацию: {len(saved)} файл(ов).")
                return saved
            # If no files extracted, try parsing result as Gradio file dict
            for item in out:
                if isinstance(item, dict) and "path" in item:
                    path_str = item["path"]
                    if path_str:
                        for p in self._collect_image_files(path_str):
                            ts = int(time.time())
                            dest = _GENERATED_DIR / f"img_fooocus_{ts}_{len(saved)}{p.suffix.lower() or '.png'}"
                            dest.write_bytes(p.read_bytes())
                            saved.append(str(dest))
            if saved:
                self._emit_status(status_callback, "completed", f"Fooocus завершил генерацию: {len(saved)} файл(ов).")
                return saved
        except Exception as e:
            if "api_name" in str(e).lower() or "fn_index" in str(e).lower():
                pass
            else:
                raise
        # Fallback: raw /api/predict often returns 500 for Fooocus (wrong fn_index/args)
        try:
            self._emit_status(status_callback, "running", "Пробую резервный HTTP-вызов Fooocus...")
            resp = _rq.post(
                f"{url}/api/predict",
                json={"data": [_prompt], "fn_index": 0},
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            out_data = (data or {}).get("data") or []
            saved = self._save_output(out_data, prefix="img_fooocus", ext="png")
            if saved:
                self._emit_status(status_callback, "completed", f"Fooocus завершил генерацию: {len(saved)} файл(ов).")
            return saved
        except Exception as e:
            raise RuntimeError(
                f"Fooocus не ответил. Убедись, что Fooocus запущен (http://127.0.0.1:7865), затем повтори. Ошибка: {e}"
            )

    def generate_video(self, prompt: str, model_key: str = "text2video-zero", **kwargs) -> list[str]:
        status_callback = kwargs.pop("status_callback", None)
        if not self.available:
            raise RuntimeError("Replicate API token not configured")
        info = VIDEO_MODELS.get(model_key, list(VIDEO_MODELS.values())[0])
        self._emit_status(status_callback, "running", f"Генерирую видео через {info.get('name') or model_key}...")
        inp = {"prompt": prompt}
        for k, v in kwargs.items():
            inp[k] = v

        output = self.client.run(info["id"], input=inp)
        return self._save_output(output, prefix="vid", ext="mp4")

    def run_any_model(self, model_id: str, prompt: str, **extra) -> list[str]:
        """Run any Replicate model by its full ID."""
        status_callback = extra.pop("status_callback", None)
        if not self.available:
            raise RuntimeError("Replicate API token not configured")
        self._emit_status(status_callback, "running", f"Запускаю кастомную модель {model_id}...")
        inp = {"prompt": prompt, **extra}
        output = self.client.run(model_id, input=inp)
        return self._save_output(output, prefix="custom", ext="png")

    def generate_async(
        self,
        kind: str,
        prompt: str,
        callback: Callable[[list[str], Optional[str]], None],
        model_key: str = "",
        status_callback: Optional[Callable[[str, str], None]] = None,
        **kwargs,
    ):
        def _worker():
            try:
                self._emit_status(status_callback, "started", "Готовлю задачу генерации...")
                if kind == "image":
                    paths = self.generate_image(
                        prompt,
                        model_key=model_key or "flux-schnell",
                        status_callback=status_callback,
                        **kwargs,
                    )
                elif kind == "video":
                    paths = self.generate_video(
                        prompt,
                        model_key=model_key or "text2video-zero",
                        status_callback=status_callback,
                        **kwargs,
                    )
                else:
                    raise ValueError(f"Unknown kind: {kind}")
                self._emit_status(status_callback, "completed", f"Генерация завершена. Получено файлов: {len(paths)}.")
                callback(paths, None)
            except Exception as exc:
                self._emit_status(status_callback, "failed", str(exc))
                callback([], str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def run_any_async(
        self,
        model_id: str,
        prompt: str,
        callback: Callable[[list[str], Optional[str]], None],
        status_callback: Optional[Callable[[str, str], None]] = None,
        **extra,
    ):
        def _worker():
            try:
                self._emit_status(status_callback, "started", "Готовлю кастомную модель...")
                paths = self.run_any_model(model_id, prompt, status_callback=status_callback, **extra)
                self._emit_status(status_callback, "completed", f"Генерация завершена. Получено файлов: {len(paths)}.")
                callback(paths, None)
            except Exception as exc:
                self._emit_status(status_callback, "failed", str(exc))
                callback([], str(exc))

        threading.Thread(target=_worker, daemon=True).start()
