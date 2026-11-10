"""On-demand model downloads for ComfyUI, served by the Instance Portal.

Instead of provisioning every model at boot, the portal's Models page lets the
user paste links (Hugging Face, CivitAI or any direct URL), see what each one
is before fetching it, and download only what they need into
``<models dir>/<folder>/<file>``.

All routes live under ``/models`` and sit behind the same Caddy auth edge as
the rest of the portal (like ``/supervisor/*`` and ``/capabilities/provision``).

Environment (read at call time, so a restart is not needed for most of them):
    MODEL_MANAGER_DIR          models root (default ${WORKSPACE:-/workspace}/ComfyUI/models)
    MODEL_MANAGER_CONCURRENCY  parallel downloads (default 2, read once at import)
    MODEL_LIST_FILE            the editable "My list" (default ${WORKSPACE:-/workspace}/model-list.yaml)
    MODEL_CATALOG              extra read-only lists: comma-separated URLs or local paths.
                               A catalog can also define `groups` (a base setup per model
                               family + its LoRAs), shown at the top of the Models page.
    HF_TOKEN / CIVITAI_TOKEN   sent only to their own hosts over https
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("model_manager")

USER_AGENT = "vast-portal-model-manager/1.0"
CHUNK_SIZE = 4 * 1024 * 1024
DISK_RESERVE = 256 * 1024 * 1024      # keep this much free after a download
MAX_PROBE_URLS = 50
CATALOG_CACHE_TTL = 60                # seconds a remote MODEL_CATALOG stays cached
STATE_FILENAME = ".model-manager.json"

# ComfyUI's model folders, in the order the UI offers them.
KNOWN_FOLDERS = [
    "checkpoints", "diffusion_models", "loras", "vae", "text_encoders",
    "clip_vision", "controlnet", "upscale_models", "embeddings",
    "style_models", "model_patches", "ipadapter", "audio_encoders",
    "latent_upscale_models", "hypernetworks", "photomaker", "gligen", "vae_approx",
]
# Legacy or common folder names seen in download URLs, mapped to the canonical one.
FOLDER_ALIASES = {
    "unet": "diffusion_models", "clip": "text_encoders", "lora": "loras",
    "esrgan": "upscale_models", "upscalers": "upscale_models",
    "controlnets": "controlnet", "embedding": "embeddings",
    "textual_inversion": "embeddings",
}
CIVITAI_TYPES = {
    "Checkpoint": "checkpoints", "LORA": "loras", "LoCon": "loras", "DoRA": "loras",
    "TextualInversion": "embeddings", "VAE": "vae", "Controlnet": "controlnet",
    "Upscaler": "upscale_models", "Hypernetwork": "hypernetworks",
}

HF_HOSTS = {"huggingface.co", "hf.co"}
_HF_FILE_RE = re.compile(
    r"^/(?P<repo>(?:(?:datasets|spaces)/)?[^/]+/[^/]+)/(?:resolve|blob)/(?P<rev>[^/]+)/(?P<path>.+)$"
)
_CIVITAI_DL_RE = re.compile(r"^/api/download/models/(\d+)/?$")
_CIVITAI_PAGE_RE = re.compile(r"^/models/(\d+)(?:/[^/]*)?/?$")
_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]*(?:/[A-Za-z0-9][A-Za-z0-9_. -]*)*$")
_TEXT_ENCODER_RE = re.compile(r"(^|[_.-])(t5xxl|umt5|t5|clip_l|clip_g|llava|qwen_?2\.5_vl|gemma|text_?encoder)")
_VAE_RE = re.compile(r"(^|[_.-])(vae|ae)([_.-]|$)")
_UPSCALE_RE = re.compile(r"(^|[_.-])([248]x|x[248])([_.-]|$)|esrgan|upscal|ultrasharp|swinir")


class UrlError(ValueError):
    """The link can't be downloaded as given; the message is shown to the user."""


class DownloadError(Exception):
    """Permanent failure: retrying will not help."""


class _Retryable(Exception):
    """Transient failure: retry, resuming from the .part file."""


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Paths and validation                                                         #
# --------------------------------------------------------------------------- #

def _workspace() -> str:
    return os.environ.get("WORKSPACE") or "/workspace"


def models_dir() -> str:
    return os.environ.get("MODEL_MANAGER_DIR") or os.path.join(_workspace(), "ComfyUI", "models")


def list_file() -> str:
    return os.environ.get("MODEL_LIST_FILE") or os.path.join(_workspace(), "model-list.yaml")


def validate_folder(folder: str) -> str:
    folder = (folder or "").strip().strip("/")
    if not _FOLDER_RE.match(folder):
        raise UrlError(f"Invalid folder: {folder!r}")
    return folder


def safe_filename(name: Optional[str]) -> Optional[str]:
    """Reduce a server- or user-supplied name to a plain file name, or None."""
    if not name:
        return None
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip('"').strip()
    if not name or name in (".", "..") or name.startswith("."):
        return None
    return name[:200]


def resolve_target(folder: str, relpath: str) -> str:
    """Absolute path of folder/relpath, refusing anything outside the models dir."""
    root = os.path.abspath(models_dir())
    path = os.path.abspath(os.path.join(root, folder, relpath))
    if os.path.commonpath([root, path]) != root or path == root:
        raise UrlError("Path escapes the models directory")
    return path


def _fmt_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# URL handling                                                                 #
# --------------------------------------------------------------------------- #

def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_hf(url: str) -> bool:
    host = _host(url)
    return host in HF_HOSTS or host.endswith(".huggingface.co")


def is_civitai(url: str) -> bool:
    host = _host(url)
    return host == "civitai.com" or host.endswith(".civitai.com")


def normalize_url(raw: str) -> str:
    """Turn what a user pastes into a direct download link.

    Hugging Face page links (``/blob/``) become ``/resolve/`` links, and CivitAI
    model pages that name a version become API download links. CivitAI pages
    without a version are kept as-is and resolved against the API later.
    """
    url = (raw or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise UrlError("Only http(s) links are supported")
    parts = urlsplit(url)
    if is_hf(url):
        m = _HF_FILE_RE.match(parts.path)
        if not m:
            raise UrlError(
                "This Hugging Face link is a repository or folder, not a file. "
                "Open the file on huggingface.co and copy that link."
            )
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "download"])
        path = f"/{m['repo']}/resolve/{m['rev']}/{m['path']}"
        return urlunsplit(("https", parts.netloc, path, query, ""))
    if is_civitai(url):
        if _CIVITAI_DL_RE.match(parts.path):
            return url
        m = _CIVITAI_PAGE_RE.match(parts.path)
        if not m:
            raise UrlError("Unsupported CivitAI link: use a model page or a download link")
        version = dict(parse_qsl(parts.query)).get("modelVersionId")
        if version and version.isdigit():
            return f"https://civitai.com/api/download/models/{version}"
        return f"https://civitai.com/models/{m.group(1)}"
    return url


def auth_headers(url: str) -> dict:
    """Bearer token for the host's own token only, never over plain http.

    httpx drops Authorization when a redirect leaves the origin, so the token
    never reaches the CDN or presigned storage that HF and CivitAI redirect to.
    """
    if urlsplit(url).scheme != "https":
        return {}
    token = None
    if is_hf(url):
        token = os.environ.get("HF_TOKEN")
    elif is_civitai(url):
        token = os.environ.get("CIVITAI_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def filename_from_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([\w-]*)'[^']*'([^;]+)", value, re.I)
    if m:
        return safe_filename(unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8"))
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I) or re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    return safe_filename(m.group(1).strip()) if m else None


def _url_basename(url: str) -> Optional[str]:
    name = safe_filename(unquote(urlsplit(url).path.rstrip("/").split("/")[-1]))
    return name if name and "." in name else None


def guess_folder(url: str, filename: Optional[str]) -> Optional[str]:
    """Best guess at the ComfyUI folder, or None when it can't be told apart.

    Repackaged repos put files under their folder name (``split_files/loras/x``),
    which is the strongest signal; otherwise the file and repo names are used.
    """
    segments = [s.lower() for s in unquote(urlsplit(url).path).split("/")]
    for segment in reversed(segments[:-1]):
        if segment in KNOWN_FOLDERS:
            return segment
        if segment in FOLDER_ALIASES:
            return FOLDER_ALIASES[segment]
    name = (filename or segments[-1] or "").lower()
    repo = segments[2] if is_hf(url) and len(segments) > 2 else ""
    if "lora" in name or "lora" in repo:
        return "loras"
    if "clip_vision" in name or "sigclip" in name:
        return "clip_vision"
    if "controlnet" in name or "controlnet" in repo:
        return "controlnet"
    if _TEXT_ENCODER_RE.search(name):
        return "text_encoders"
    if _VAE_RE.search(name):
        return "vae"
    if _UPSCALE_RE.search(name):
        return "upscale_models"
    return None


def _auth_message(url: str, status: int) -> str:
    if is_hf(url):
        if not os.environ.get("HF_TOKEN"):
            return ("Gated or private model: set HF_TOKEN in your Vast account environment "
                    "variables and accept the license on the model page.")
        return ("Access denied: accept the license on the model page with the "
                "Hugging Face account that owns HF_TOKEN.")
    if is_civitai(url):
        if not os.environ.get("CIVITAI_TOKEN"):
            return "CivitAI requires a login for this file: set CIVITAI_TOKEN in your Vast account."
        return "CivitAI refused CIVITAI_TOKEN for this file."
    return f"Access denied (HTTP {status})."


def civitai_lookup(url: str, client: httpx.Client) -> dict:
    """Resolve a CivitAI download or model link through the public API."""
    parts = urlsplit(url)
    headers = auth_headers("https://civitai.com/")
    m = _CIVITAI_DL_RE.match(parts.path)
    if m:
        version_id = m.group(1)
    else:
        page = _CIVITAI_PAGE_RE.match(parts.path)
        if not page:
            raise UrlError("Unsupported CivitAI link")
        r = client.get(f"https://civitai.com/api/v1/models/{page.group(1)}", headers=headers)
        r.raise_for_status()
        versions = r.json().get("modelVersions") or []
        if not versions:
            raise UrlError("This CivitAI model has no downloadable version")
        version_id = str(versions[0]["id"])
    r = client.get(f"https://civitai.com/api/v1/model-versions/{version_id}", headers=headers)
    r.raise_for_status()
    data = r.json()
    files = data.get("files") or []
    primary = next((f for f in files if f.get("primary")), files[0] if files else {})
    model = data.get("model") or {}
    size_kb = primary.get("sizeKB")
    return {
        "download_url": url if m else f"https://civitai.com/api/download/models/{version_id}",
        "filename": safe_filename(primary.get("name")),
        "size": int(size_kb * 1024) if size_kb else None,
        "folder": CIVITAI_TYPES.get(model.get("type")),
        "name": " — ".join(n for n in (model.get("name"), data.get("name")) if n) or None,
    }


def default_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=120.0),
        headers={"User-Agent": USER_AGENT},
    )


def probe(raw_url: str, client: httpx.Client) -> dict:
    """Describe a link without downloading it: file name, size, folder, access."""
    result = {"input": raw_url, "url": None, "filename": None, "folder": None,
              "name": None, "size": None, "status": "ok", "message": "", "exists": False}
    try:
        url = normalize_url(raw_url)
    except UrlError as e:
        result.update(status="error", message=str(e))
        return result
    result["url"] = url
    try:
        if is_civitai(url):
            info = civitai_lookup(url, client)
            url = result["url"] = info["download_url"]
            result.update(filename=info["filename"], size=info["size"],
                          folder=info["folder"], name=info["name"])
            r = client.head(url, headers=auth_headers(url), follow_redirects=False)
        elif is_hf(url):
            r = client.head(url, headers=auth_headers(url), follow_redirects=False)
            # A renamed repo answers with a same-host redirect before the CDN one.
            hops = 0
            while (r.is_redirect and "x-linked-size" not in r.headers
                   and is_hf(str(r.next_request.url) if r.next_request else "") and hops < 3):
                r = client.head(r.next_request.url, headers=auth_headers(url), follow_redirects=False)
                hops += 1
            result["filename"] = _url_basename(url)
            size = r.headers.get("x-linked-size") or (
                r.headers.get("content-length") if r.status_code == 200 else None)
            result["size"] = int(size) if size and size.isdigit() else None
        else:
            r = client.head(url, follow_redirects=True)
            if r.status_code in (403, 405) or r.status_code >= 500:
                # Some servers refuse HEAD; ask for the headers of a real GET.
                with client.stream("GET", url) as streamed:
                    r = streamed
            result["filename"] = filename_from_disposition(r.headers.get("content-disposition")) \
                or _url_basename(str(r.url)) or _url_basename(url)
            size = r.headers.get("content-length")
            result["size"] = int(size) if size and size.isdigit() else None
    except UrlError as e:
        result.update(status="error", message=str(e))
        return result
    except httpx.HTTPStatusError as e:
        r = e.response
    except httpx.HTTPError as e:
        result.update(status="error", message=f"Could not reach the server: {e}")
        return result

    if r.status_code in (401, 403):
        result.update(status="auth_required", message=_auth_message(url, r.status_code))
    elif r.status_code == 404:
        result.update(status="not_found", message="Not found (404): check the link.")
    elif r.status_code >= 400:
        result.update(status="error", message=f"The server answered HTTP {r.status_code}.")
    if not result["folder"]:
        result["folder"] = guess_folder(url, result["filename"])
    if result["folder"] and result["filename"]:
        try:
            result["exists"] = os.path.exists(resolve_target(result["folder"], result["filename"]))
        except UrlError:
            pass
    return result


# --------------------------------------------------------------------------- #
# Download state: which URL produced which file                                #
# --------------------------------------------------------------------------- #

_state_lock = threading.Lock()


def _state_path() -> str:
    return os.path.join(models_dir(), STATE_FILENAME)


def read_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def record_download(url: str, folder: str, filename: str) -> None:
    with _state_lock:
        state = read_state()
        state[url] = f"{folder}/{filename}"
        _write_json_atomic(_state_path(), state)


def forget_path(relpath: str) -> None:
    with _state_lock:
        state = read_state()
        kept = {u: p for u, p in state.items() if p != relpath}
        if kept != state:
            _write_json_atomic(_state_path(), kept)


# --------------------------------------------------------------------------- #
# Download manager                                                             #
# --------------------------------------------------------------------------- #

ACTIVE_STATES = ("queued", "downloading")


@dataclass
class Job:
    url: str
    folder: str
    filename: Optional[str] = None
    name: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"      # queued|downloading|completed|skipped|failed|cancelled|interrupted
    total: Optional[int] = None
    downloaded: int = 0
    speed: float = 0.0
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    part_path: Optional[str] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "folder": self.folder,
            "filename": self.filename, "name": self.name, "status": self.status,
            "total": self.total, "downloaded": self.downloaded,
            "speed": round(self.speed), "error": self.error,
            "created": self.created, "finished": self.finished,
        }


class DownloadManager:
    def __init__(self, client_factory: Callable[[], httpx.Client] = default_client,
                 concurrency: int = 2, retries: int = 3, backoff: float = 2.0):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._client_factory = client_factory
        self._executor = ThreadPoolExecutor(max_workers=max(1, concurrency),
                                            thread_name_prefix="model-dl")
        self._retries = retries
        self._backoff = backoff
        self._stopping = False

    # -- public API ---------------------------------------------------------- #

    def submit(self, url: str, folder: str, filename: Optional[str] = None,
               name: Optional[str] = None) -> Job:
        folder = validate_folder(folder)
        if filename is not None and filename.strip():
            clean = safe_filename(filename)
            if not clean:
                raise UrlError(f"Invalid file name: {filename!r}")
            filename = clean
        else:
            filename = None
        with self._lock:
            for job in self._jobs.values():
                if job.status not in ACTIVE_STATES or job.folder != folder:
                    continue
                if job.url == url:
                    return job
                if filename and job.filename == filename:
                    raise UrlError(f"{folder}/{filename} is already downloading from another link")
            job = Job(url=url, folder=folder, filename=filename, name=name)
            self._jobs[job.id] = job
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def jobs(self) -> List[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.created, reverse=True)]

    def cancel(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        job.cancel.set()
        if job.status == "queued":
            self._finish(job, "cancelled")
        return job

    def retry(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job.status in ACTIVE_STATES:
            return job
        with self._lock:
            del self._jobs[job_id]
        return self.submit(job.url, job.folder, job.filename, job.name)

    def clear_finished(self) -> int:
        with self._lock:
            done = [i for i, j in self._jobs.items() if j.status not in ACTIVE_STATES]
            for i in done:
                del self._jobs[i]
        return len(done)

    def active_targets(self) -> set:
        targets = set()
        for job in list(self._jobs.values()):
            if job.status in ACTIVE_STATES and job.filename:
                targets.add(f"{job.folder}/{job.filename}")
        return targets

    def shutdown(self) -> None:
        """Stop on portal exit, keeping .part files so a retry resumes them."""
        self._stopping = True
        for job in list(self._jobs.values()):
            job.cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- worker --------------------------------------------------------------- #

    def _finish(self, job: Job, status: str, error: Optional[str] = None) -> None:
        job.status = status
        job.error = error
        job.speed = 0.0
        job.finished = time.time()
        label = job.filename or job.url
        if error:
            logger.warning("%s: %s (%s)", status, label, error)
        else:
            logger.info("%s: %s/%s", status, job.folder, label)

    def _run(self, job: Job) -> None:
        if job.cancel.is_set():
            if job.status in ACTIVE_STATES:
                self._finish(job, "cancelled")
            return
        job.status = "downloading"
        logger.info("downloading %s -> %s/", job.url, job.folder)
        attempt = 0
        while True:
            try:
                self._download(job)
                return
            except _Cancelled:
                if self._stopping:
                    self._finish(job, "interrupted", "Portal restarted: retry to resume")
                else:
                    self._discard_part(job)
                    self._finish(job, "cancelled")
                return
            except (DownloadError, UrlError) as e:
                self._finish(job, "failed", str(e))
                return
            except OSError as e:
                self._finish(job, "failed", f"Disk error: {e.strerror or e}")
                return
            except httpx.HTTPStatusError as e:  # CivitAI API lookup
                status = e.response.status_code
                message = (_auth_message(job.url, status) if status in (401, 403)
                           else f"CivitAI API answered HTTP {status}.")
                self._finish(job, "failed", message)
                return
            except (_Retryable, httpx.TransportError) as e:
                attempt += 1
                if attempt > self._retries:
                    self._finish(job, "failed", f"Network error after {self._retries} retries: {e}")
                    return
                logger.info("retry %d/%d for %s: %s", attempt, self._retries, job.url, e)
                if job.cancel.wait(self._backoff * (2 ** (attempt - 1))):
                    continue  # the next attempt raises _Cancelled straight away
            except Exception as e:  # noqa: BLE001 - never let a worker die silently
                logger.exception("unexpected error downloading %s", job.url)
                self._finish(job, "failed", f"Unexpected error: {e}")
                return

    def _discard_part(self, job: Job) -> None:
        if job.part_path and os.path.exists(job.part_path):
            try:
                os.remove(job.part_path)
            except OSError:
                pass

    def _download(self, job: Job) -> None:
        if job.cancel.is_set():
            raise _Cancelled()
        with self._client_factory() as client:
            url = job.url
            if is_civitai(url) and not _CIVITAI_DL_RE.match(urlsplit(url).path):
                info = civitai_lookup(url, client)
                url = info["download_url"]
                job.filename = job.filename or info["filename"]
                job.name = job.name or info["name"]

            headers = {"User-Agent": USER_AGENT, **auth_headers(url)}
            offset = 0
            if job.filename:
                if self._already_installed(job):
                    return
                offset = self._resume_offset(job)
                if offset:
                    headers["Range"] = f"bytes={offset}-"

            with client.stream("GET", url, headers=headers) as r:
                if r.status_code == 416 and offset:
                    self._discard_part(job)
                    raise _Retryable("stale partial file, restarting")
                if r.status_code in (401, 403):
                    raise DownloadError(_auth_message(url, r.status_code))
                if r.status_code == 404:
                    raise DownloadError("Not found (404): check the link.")
                if r.status_code in (408, 429) or r.status_code >= 500:
                    raise _Retryable(f"server answered HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise DownloadError(f"The server answered HTTP {r.status_code}.")

                if not job.filename:
                    job.filename = (filename_from_disposition(r.headers.get("content-disposition"))
                                    or _url_basename(str(r.url)) or _url_basename(url))
                    if not job.filename:
                        raise DownloadError("Could not tell the file name: enter one and retry.")
                    if self._already_installed(job):
                        return
                    offset = 0  # no Range was sent, so any old .part is overwritten
                if r.status_code == 200 and offset:
                    offset = 0  # the server ignored Range: start over

                length = r.headers.get("content-length")
                remaining = int(length) if length and length.isdigit() else None
                job.total = offset + remaining if remaining is not None else None
                job.downloaded = offset
                dest_dir = os.path.dirname(job.part_path)
                os.makedirs(dest_dir, exist_ok=True)
                if remaining is not None:
                    free = shutil.disk_usage(dest_dir).free
                    if remaining > free - DISK_RESERVE:
                        raise DownloadError(
                            f"Not enough disk space: needs {_fmt_bytes(remaining)}, "
                            f"{_fmt_bytes(free)} free.")

                self._stream_to_part(job, r, append=bool(offset))

        if job.total is not None and job.downloaded != job.total:
            raise _Retryable("connection closed before the end of the file")
        dest = job.part_path[: -len(".part")]
        os.replace(job.part_path, dest)
        record_download(job.url, job.folder, job.filename)
        self._finish(job, "completed")

    def _already_installed(self, job: Job) -> bool:
        dest = resolve_target(job.folder, job.filename)
        job.part_path = dest + ".part"
        if os.path.exists(dest):
            record_download(job.url, job.folder, job.filename)
            self._finish(job, "skipped", None)
            return True
        return False

    def _resume_offset(self, job: Job) -> int:
        try:
            return os.path.getsize(job.part_path)
        except OSError:
            return 0

    def _stream_to_part(self, job: Job, response: httpx.Response, append: bool) -> None:
        last_t, last_bytes = time.monotonic(), job.downloaded
        with open(job.part_path, "ab" if append else "wb") as f:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                if job.cancel.is_set():
                    raise _Cancelled()
                f.write(chunk)
                job.downloaded += len(chunk)
                now = time.monotonic()
                if now - last_t >= 0.5:
                    rate = (job.downloaded - last_bytes) / (now - last_t)
                    job.speed = rate if not job.speed else 0.7 * job.speed + 0.3 * rate
                    last_t, last_bytes = now, job.downloaded


# --------------------------------------------------------------------------- #
# "My list": saved links (local, editable) + MODEL_CATALOG (read-only)         #
# --------------------------------------------------------------------------- #

_catalog_cache: dict[str, tuple[float, list]] = {}


def _parse_entries(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("models") or []
    entries = []
    for raw in data if isinstance(data, list) else []:
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        try:
            url = normalize_url(str(raw["url"]))
            folder = validate_folder(str(raw.get("folder") or guess_folder(url, raw.get("filename")) or ""))
        except UrlError as e:
            logger.warning("skipping list entry %s: %s", raw.get("url"), e)
            continue
        entries.append({
            "url": url, "folder": folder,
            "filename": safe_filename(raw.get("filename")),
            "name": raw.get("name") or None,
            "note": raw.get("note") or None,
        })
    return entries


def read_local_list() -> list[dict]:
    try:
        with open(list_file(), "r", encoding="utf-8") as f:
            return _parse_entries(yaml.safe_load(f))
    except FileNotFoundError:
        return []


def write_local_list(entries: list[dict]) -> None:
    path = list_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    clean = [{k: v for k, v in e.items() if v} for e in entries]
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Models saved from the Instance Portal. Host a copy and point\n"
                "# MODEL_CATALOG at it to get the same list on a new instance.\n")
        yaml.safe_dump({"models": clean}, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def catalog_sources() -> list[str]:
    return [s.strip() for s in os.environ.get("MODEL_CATALOG", "").split(",") if s.strip()]


def _http_error_message(url: str, status: int, what: str) -> str:
    if status in (401, 403):
        return _auth_message(url, status)
    if status == 404:
        return f"{what} not found (404): check the link, or HF_TOKEN if the repo is private."
    return f"{what}: the server answered HTTP {status}."


def fetch_catalog(source: str, client_factory: Callable[[], httpx.Client]):
    """The parsed YAML of a MODEL_CATALOG source (URL or local path).

    Remote sources are cached for CATALOG_CACHE_TTL seconds. A catalog hosted
    in a private Hugging Face repo is fetched with HF_TOKEN.
    """
    if not re.match(r"^https?://", source, re.I):
        with open(source, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    cached = _catalog_cache.get(source)
    if cached and time.time() - cached[0] < CATALOG_CACHE_TTL:
        return cached[1]
    with client_factory() as client:
        r = client.get(source, headers=auth_headers(source))
        if r.status_code >= 400:
            raise UrlError(_http_error_message(source, r.status_code, "Catalog"))
        data = yaml.safe_load(r.text)
    _catalog_cache[source] = (time.time(), data)
    return data


def _load_catalog_source(source: str, client_factory: Callable[[], httpx.Client]) -> list[dict]:
    return _parse_entries(fetch_catalog(source, client_factory))


def annotate(items: list[dict], manager: DownloadManager) -> None:
    """Add path / installed / job_id to list entries, in place."""
    state = read_state()
    active = {(j["url"], j["folder"]): j for j in manager.jobs() if j["status"] in ACTIVE_STATES}
    for item in items:
        rel = f"{item['folder']}/{item['filename']}" if item.get("filename") else state.get(item["url"])
        installed = False
        if rel:
            try:
                installed = os.path.exists(resolve_target(*rel.split("/", 1)))
            except (UrlError, TypeError):
                installed = False
        job = active.get((item["url"], item["folder"]))
        item.update(path=rel, installed=installed, job_id=job["id"] if job else None)


def build_list(manager: DownloadManager, client_factory: Callable[[], httpx.Client]) -> dict:
    items: list[dict] = []
    seen: set = set()
    sources = []
    local = read_local_list()
    for e in local:
        key = (e["url"], e["folder"])
        if key not in seen:
            seen.add(key)
            items.append({**e, "source": "local"})
    for source in catalog_sources():
        try:
            entries = _load_catalog_source(source, client_factory)
            sources.append({"source": source, "ok": True, "count": len(entries)})
        except Exception as e:  # noqa: BLE001 - report, don't break the page
            sources.append({"source": source, "ok": False, "error": str(e)})
            continue
        for e in entries:
            key = (e["url"], e["folder"])
            if key not in seen:
                seen.add(key)
                items.append({**e, "source": source})

    annotate(items, manager)
    return {"items": items, "sources": sources, "list_file": list_file()}


# --------------------------------------------------------------------------- #
# Groups: a base setup per model family, plus its LoRAs, from MODEL_CATALOG    #
# --------------------------------------------------------------------------- #
#
#   groups:
#     - name: Krea 2
#       setup:                         # installed together, with one button
#         diffusion_models: https://huggingface.co/.../model.safetensors
#         text_encoders: [https://..., https://...]
#       extras:                        # same format, picked one by one
#         diffusion_models: https://...
#       loras: loras/krea2             # a folder of the catalog's HF repo, listed live
#
# `loras` also accepts a Hugging Face folder link (.../tree/main/path), file
# links, or a list mixing both. Files from a folder go flat into `loras/`.

MODEL_EXTENSIONS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
_HF_TREE_RE = re.compile(
    r"^/(?P<repo>(?:(?:datasets|spaces)/)?[^/]+/[^/]+)/tree/(?P<rev>[^/]+)(?:/(?P<path>.*))?$"
)
_tree_cache: dict[str, tuple[float, list]] = {}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "group"


def catalog_repo(source: str) -> Optional[tuple[str, str]]:
    """(repo, revision) of a catalog file hosted on Hugging Face, else None."""
    if not re.match(r"^https?://", source, re.I) or not is_hf(source):
        return None
    m = _HF_FILE_RE.match(unquote(urlsplit(source).path))
    return (m["repo"], m["rev"]) if m else None


def _is_folder_ref(value: str) -> bool:
    if not re.match(r"^https?://", value, re.I):
        return True
    return is_hf(value) and bool(_HF_TREE_RE.match(unquote(urlsplit(value).path)))


def _folder_ref(value: str, base: Optional[tuple[str, str]]) -> tuple[str, str, str]:
    """(repo, revision, path) for a folder link or a path relative to the catalog."""
    if re.match(r"^https?://", value, re.I):
        m = _HF_TREE_RE.match(unquote(urlsplit(value).path))
        return m["repo"], m["rev"], (m["path"] or "").strip("/")
    if base is None:
        raise UrlError("A relative folder needs the catalog to be hosted on Hugging Face; "
                       "use a full https://huggingface.co/<repo>/tree/main/<folder> link.")
    path = value.strip().strip("/")
    if any(p in (".", "..") for p in path.split("/")):
        raise UrlError("Invalid folder path")
    return base[0], base[1], path


def hf_tree(repo: str, rev: str, path: str, client_factory: Callable[[], httpx.Client]) -> list[dict]:
    """Every file under a Hugging Face repo folder (recursive): path, size, url."""
    key = f"{repo}@{rev}:{path}"
    cached = _tree_cache.get(key)
    if cached and time.time() - cached[0] < CATALOG_CACHE_TTL:
        return cached[1]
    api_repo = repo if repo.startswith(("datasets/", "spaces/")) else f"models/{repo}"
    api = (f"https://huggingface.co/api/{api_repo}/tree/{quote(rev, safe='')}"
           + (f"/{quote(path)}" if path else ""))
    files: list[dict] = []
    next_url: Optional[str] = f"{api}?recursive=true"
    with client_factory() as client:
        pages = 0
        while next_url and pages < 20:
            r = client.get(next_url, headers=auth_headers(api))
            if r.status_code >= 400:
                raise UrlError(_http_error_message(api, r.status_code, "Folder"))
            for item in r.json():
                if item.get("type") != "file":
                    continue
                lfs = item.get("lfs") or {}
                files.append({
                    "path": item["path"],
                    "size": lfs.get("size") or item.get("size"),
                    "url": f"https://huggingface.co/{repo}/resolve/{quote(rev, safe='')}/{quote(item['path'])}",
                })
            next_url = r.links.get("next", {}).get("url")
            pages += 1
    files.sort(key=lambda f: f["path"].lower())
    _tree_cache[key] = (time.time(), files)
    return files


def _model_entry(file: dict, folder: str) -> dict:
    return {"url": file["url"], "folder": folder, "filename": safe_filename(file["path"]),
            "name": None, "note": None, "size": file["size"]}


def list_hf_folder(repo: str, rev: str, path: str, folder: str,
                   client_factory: Callable[[], httpx.Client]) -> list[dict]:
    """Model files under a Hugging Face repo folder (recursive), as list entries."""
    entries = [_model_entry(f, folder) for f in hf_tree(repo, rev, path, client_factory)
               if f["path"].lower().endswith(MODEL_EXTENSIONS)]
    return sorted(entries, key=lambda e: (e["filename"] or "").lower())


# --------------------------------------------------------------------------- #
# Characters: one folder per character, one subfolder per model group          #
# --------------------------------------------------------------------------- #
#
#   characters: loras/characters        (top level of the catalog)
#
#   loras/characters/
#   ├── Alice/
#   │   ├── cover.jpg                   # any image directly in the folder: the card picture
#   │   ├── krea/alice_krea.safetensors # subfolder named after a group id
#   │   └── moss/alice_moss.safetensors
#   └── Bob/ ...
#
# Each group shows the characters that have a subfolder named after its id.
# Images can sit in a private repo: the portal serves them through
# /models/image/<key>, fetched with HF_TOKEN, and only for images it listed.

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_images: dict[str, str] = {}                              # key -> image URL
_image_cache: dict[str, tuple[float, str, bytes]] = {}    # key -> (time, type, bytes)


def _image_key(url: str) -> str:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    _images[key] = url
    return key


def list_characters(repo: str, rev: str, path: str,
                    client_factory: Callable[[], httpx.Client]) -> list[dict]:
    """Characters under a folder: name, image URL, and model files by subfolder."""
    chars: dict[str, dict] = {}
    prefix = f"{path}/" if path else ""
    for f in hf_tree(repo, rev, path, client_factory):
        parts = f["path"][len(prefix):].split("/") if f["path"].startswith(prefix) else []
        if len(parts) < 2:
            continue
        char = chars.setdefault(parts[0], {"name": parts[0], "image_url": None, "files": {}})
        ext = os.path.splitext(parts[-1])[1].lower()
        if len(parts) == 2 and ext in IMAGE_TYPES:
            char["image_url"] = char["image_url"] or f["url"]
        elif len(parts) >= 3 and parts[-1].lower().endswith(MODEL_EXTENSIONS):
            char["files"].setdefault(parts[1].lower(), []).append(f)
    return sorted(chars.values(), key=lambda c: c["name"].lower())


def fetch_image(key: str, client_factory: Callable[[], httpx.Client]) -> tuple[str, bytes]:
    url = _images.get(key)
    if not url:
        raise KeyError(key)
    cached = _image_cache.get(key)
    if cached and time.time() - cached[0] < 3600:
        return cached[1], cached[2]
    with client_factory() as client:
        r = client.get(url, headers=auth_headers(url))
    if r.status_code >= 400:
        raise UrlError(_http_error_message(url, r.status_code, "Image"))
    if len(r.content) > MAX_IMAGE_BYTES:
        raise UrlError("Image too large (8 MB max)")
    ctype = IMAGE_TYPES.get(os.path.splitext(urlsplit(url).path)[1].lower(), "application/octet-stream")
    _image_cache[key] = (time.time(), ctype, r.content)
    return ctype, r.content


def _parse_folder_map(value) -> list[dict]:
    """``{folder: url | [url, ...]}`` (or a plain list of entries) to list entries."""
    if not isinstance(value, dict):
        return _parse_entries(value or [])
    raw = []
    for folder, urls in value.items():
        for u in urls if isinstance(urls, list) else [urls]:
            if isinstance(u, dict):
                raw.append({**u, "folder": u.get("folder") or folder})
            elif u:
                raw.append({"url": u, "folder": folder})
    return _parse_entries(raw)


def _expand_loras(value, base: Optional[tuple[str, str]],
                  client_factory: Callable[[], httpx.Client]) -> tuple[list[dict], list[str]]:
    items, errors, seen = [], [], set()
    for v in value if isinstance(value, list) else [value]:
        found: list[dict] = []
        if isinstance(v, dict):
            found = _parse_entries([{**v, "folder": v.get("folder") or "loras"}])
        elif isinstance(v, str) and v.strip():
            v = v.strip()
            if not _is_folder_ref(v):
                found = _parse_entries([{"url": v, "folder": "loras"}])
            else:
                try:
                    found = list_hf_folder(*_folder_ref(v, base), "loras", client_factory)
                except UrlError as e:
                    errors.append(f"{v}: {e}")
                except (httpx.HTTPError, ValueError) as e:
                    errors.append(f"{v}: could not list the folder ({e})")
        for e in found:
            if (e["url"], e["folder"]) not in seen:
                seen.add((e["url"], e["folder"]))
                items.append(e)
    return items, errors


def _load_characters(value, base, client_factory, errors: list) -> list[dict]:
    chars = []
    for v in value if isinstance(value, list) else [value]:
        if not isinstance(v, str) or not v.strip():
            continue
        try:
            if not _is_folder_ref(v.strip()):
                raise UrlError("expected a folder (e.g. loras/characters), not a file link")
            chars += list_characters(*_folder_ref(v.strip(), base), client_factory)
        except UrlError as e:
            errors.append(f"characters {v}: {e}")
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"characters {v}: could not list the folder ({e})")
    return chars


def _group_characters(chars: list[dict], gid: str) -> list[dict]:
    out = []
    for c in chars:
        files = c["files"].get(gid.lower())
        if files:
            out.append({
                "name": c["name"],
                "image": f"/models/image/{_image_key(c['image_url'])}" if c["image_url"] else None,
                "loras": [_model_entry(f, "loras") for f in files],
            })
    return out


def build_groups(manager: DownloadManager, client_factory: Callable[[], httpx.Client]) -> dict:
    groups, sources, seen, errors = [], [], set(), []
    for source in catalog_sources():
        try:
            doc = fetch_catalog(source, client_factory)
        except Exception as e:  # noqa: BLE001 - report, don't break the page
            sources.append({"source": source, "ok": False, "error": str(e)})
            continue
        raw_groups = doc.get("groups") if isinstance(doc, dict) else None
        raw_groups = raw_groups if isinstance(raw_groups, list) else []
        sources.append({"source": source, "ok": True, "count": len(raw_groups)})
        base = catalog_repo(source)
        chars = _load_characters(doc.get("characters"), base, client_factory, errors) \
            if raw_groups and doc.get("characters") else []
        for raw in raw_groups:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            gid = _slug(str(raw.get("id") or raw["name"]))
            if gid in seen:
                continue
            seen.add(gid)
            loras, group_errors = _expand_loras(raw.get("loras") or [], base, client_factory)
            groups.append({
                "id": gid,
                "name": str(raw["name"]),
                "description": str(raw["description"]) if raw.get("description") else None,
                "source": source,
                "setup": _parse_folder_map(raw.get("setup")),
                "characters": _group_characters(chars, gid),
                "extras": _parse_folder_map(raw.get("extras")),
                "loras": loras,
                "errors": group_errors,
            })
    items = [i for g in groups for k in ("setup", "extras", "loras") for i in g[k]]
    items += [i for g in groups for c in g["characters"] for i in c["loras"]]
    for item in items:
        # A Hugging Face link names its file: knowing it shows what is already on
        # disk, and the download skips it without a request.
        if not item.get("filename") and is_hf(item["url"]):
            item["filename"] = _url_basename(item["url"])
    annotate(items, manager)
    return {"groups": groups, "sources": sources, "errors": errors}


# --------------------------------------------------------------------------- #
# Installed files                                                              #
# --------------------------------------------------------------------------- #

def _is_placeholder(name: str) -> bool:
    return name.startswith("put_") and name.endswith("_here")


def scan_installed() -> dict:
    root = models_dir()
    folders = []
    if not os.path.isdir(root):
        return {"folders": folders, "total_size": 0}
    seen_real = set()
    entries = sorted(os.scandir(root), key=lambda e: (e.is_symlink(), e.name))
    total = 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        real = os.path.realpath(entry.path)
        if real in seen_real:
            continue  # e.g. ComfyUI's ckpt -> checkpoints symlink
        seen_real.add(real)
        files = []
        for dirpath, dirnames, filenames in os.walk(entry.path):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or _is_placeholder(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue  # broken symlink
                rel = os.path.relpath(full, entry.path).replace(os.sep, "/")
                files.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime,
                              "link": os.path.islink(full), "partial": name.endswith(".part")})
                total += st.st_size
        folders.append({"folder": entry.name, "files": files,
                        "size": sum(f["size"] for f in files)})
    return {"folders": folders, "total_size": total}


# --------------------------------------------------------------------------- #
# HTTP routes                                                                  #
# --------------------------------------------------------------------------- #

def _setup_logging() -> None:
    log_dir = "/var/log/portal"
    if logger.handlers or not os.path.isdir(log_dir):
        return
    handler = logging.FileHandler(os.path.join(log_dir, "model-manager.log"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


_setup_logging()
manager = DownloadManager(concurrency=int(os.environ.get("MODEL_MANAGER_CONCURRENCY", "2") or 2))
router = APIRouter(prefix="/models", tags=["models"])


class ProbeRequest(BaseModel):
    urls: List[str]


class DownloadItem(BaseModel):
    url: str
    folder: str
    filename: Optional[str] = None
    name: Optional[str] = None


class DownloadRequest(BaseModel):
    items: List[DownloadItem]
    save_to_list: bool = False


class ListEntry(BaseModel):
    url: str
    folder: str
    filename: Optional[str] = None
    name: Optional[str] = None


class FileRef(BaseModel):
    folder: str
    path: str


def _folders() -> List[str]:
    folders = list(KNOWN_FOLDERS)
    root = models_dir()
    if os.path.isdir(root):
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") \
                    and entry.name not in folders:
                folders.append(entry.name)
    return folders


@router.get("/status")
async def models_status():
    root = models_dir()
    enabled = os.path.isdir(root)
    disk = None
    if enabled:
        usage = shutil.disk_usage(root)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    return {
        "enabled": enabled,
        "models_dir": root,
        "folders": _folders() if enabled else KNOWN_FOLDERS,
        "disk": disk,
        "tokens": {"hf": bool(os.environ.get("HF_TOKEN")),
                   "civitai": bool(os.environ.get("CIVITAI_TOKEN"))},
        "list_file": list_file(),
        "catalog": catalog_sources(),
    }


def _probe_one(url: str) -> dict:
    with default_client() as client:
        return probe(url, client)


@router.post("/probe")
async def models_probe(req: ProbeRequest):
    urls = [u.strip() for u in req.urls if u.strip()][:MAX_PROBE_URLS]
    return await asyncio.gather(*(asyncio.to_thread(_probe_one, u) for u in urls))


@router.post("/download")
async def models_download(req: DownloadRequest):
    jobs, errors = [], []
    saved = read_local_list() if req.save_to_list else None
    for item in req.items:
        try:
            url = normalize_url(item.url)
            job = manager.submit(url, item.folder, item.filename, item.name)
            jobs.append(job.to_dict())
            if saved is not None and not any(e["url"] == url and e["folder"] == job.folder for e in saved):
                saved.append({"url": url, "folder": job.folder,
                              "filename": safe_filename(item.filename), "name": item.name})
        except UrlError as e:
            errors.append({"url": item.url, "error": str(e)})
    if saved is not None:
        await asyncio.to_thread(write_local_list, saved)
    return {"jobs": jobs, "errors": errors}


@router.get("/jobs")
async def models_jobs():
    return manager.jobs()


@router.post("/jobs/clear")
async def models_jobs_clear():
    return {"cleared": manager.clear_finished()}


@router.post("/jobs/{job_id}/cancel")
async def models_job_cancel(job_id: str):
    try:
        return manager.cancel(job_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown download")


@router.post("/jobs/{job_id}/retry")
async def models_job_retry(job_id: str):
    try:
        return manager.retry(job_id).to_dict()
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown download")


@router.get("/list")
async def models_list():
    return await asyncio.to_thread(build_list, manager, default_client)


@router.get("/groups")
async def models_groups():
    return await asyncio.to_thread(build_groups, manager, default_client)


@router.get("/image/{key}")
async def models_image(key: str):
    """A character picture listed by /groups, fetched with HF_TOKEN if needed."""
    try:
        ctype, data = await asyncio.to_thread(fetch_image, key, default_client)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown image")
    except UrlError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch the image: {e}")
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/list")
async def models_list_add(entry: ListEntry):
    try:
        url = normalize_url(entry.url)
        folder = validate_folder(entry.folder)
    except UrlError as e:
        raise HTTPException(status_code=400, detail=str(e))
    entries = read_local_list()
    if not any(e["url"] == url and e["folder"] == folder for e in entries):
        entries.append({"url": url, "folder": folder,
                        "filename": safe_filename(entry.filename), "name": entry.name})
        await asyncio.to_thread(write_local_list, entries)
    return {"ok": True}


@router.post("/list/remove")
async def models_list_remove(entry: ListEntry):
    entries = read_local_list()
    kept = [e for e in entries if not (e["url"] == entry.url and e["folder"] == entry.folder)]
    if len(kept) == len(entries):
        raise HTTPException(status_code=404, detail="Not in the local list (entries from MODEL_CATALOG are read-only)")
    await asyncio.to_thread(write_local_list, kept)
    return {"ok": True}


@router.get("/list/export")
async def models_list_export():
    data = await asyncio.to_thread(build_list, manager, default_client)
    keys = ("url", "folder", "filename", "name", "note")
    models = [{k: item[k] for k in keys if item.get(k)} for item in data["items"]]
    body = yaml.safe_dump({"models": models}, sort_keys=False, allow_unicode=True)
    return Response(content=body, media_type="application/x-yaml",
                    headers={"Content-Disposition": 'attachment; filename="model-list.yaml"'})


@router.get("/installed")
async def models_installed():
    return await asyncio.to_thread(scan_installed)


@router.post("/installed/delete")
async def models_installed_delete(ref: FileRef):
    try:
        folder = validate_folder(ref.folder)
        parts = [p for p in ref.path.replace("\\", "/").split("/")]
        if any(p in ("", ".", "..") for p in parts):
            raise UrlError("Invalid path")
        rel = "/".join(parts)
        target = resolve_target(folder, rel)
    except UrlError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if f"{folder}/{rel}" in manager.active_targets():
        raise HTTPException(status_code=409, detail="This file is downloading: cancel it first")
    if not (os.path.islink(target) or os.path.isfile(target)):
        raise HTTPException(status_code=404, detail="File not found")
    os.unlink(target)  # removes a symlink itself, never what it points to
    forget_path(f"{folder}/{rel}")
    logger.info("deleted %s/%s", folder, rel)
    return {"ok": True}
