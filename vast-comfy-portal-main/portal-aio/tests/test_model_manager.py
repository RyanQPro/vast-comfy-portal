"""Tests for the portal's on-demand model downloads (portal/model_manager.py).

Network is simulated with httpx.MockTransport, so these run offline.
"""

import os
import sys
import time

import httpx
import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portal"))

import model_manager as mm  # noqa: E402

HF_URL = "https://huggingface.co/org/repo/resolve/main/split_files/loras/style.safetensors"
CDN_HOST = "cas-bridge.xethub.hf.co"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setenv("MODEL_MANAGER_DIR", str(models))
    monkeypatch.setenv("MODEL_LIST_FILE", str(tmp_path / "model-list.yaml"))
    for var in ("HF_TOKEN", "CIVITAI_TOKEN", "MODEL_CATALOG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mm, "_catalog_cache", {})
    monkeypatch.setattr(mm, "_tree_cache", {})
    monkeypatch.setattr(mm, "_images", {})
    monkeypatch.setattr(mm, "_image_cache", {})
    return models


def make_manager(handler, **kwargs):
    factory = lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    kwargs.setdefault("backoff", 0)
    return mm.DownloadManager(client_factory=factory, **kwargs)


def wait(job, timeout=5):
    deadline = time.time() + timeout
    while job.status in mm.ACTIVE_STATES and time.time() < deadline:
        time.sleep(0.01)
    assert job.status not in mm.ACTIVE_STATES, f"job still {job.status}"
    return job


def hf_handler(payload, seen, status=None):
    """huggingface.co 302s to a CDN host, which serves the bytes (honouring Range)."""
    def handler(request):
        seen.append(request)
        if request.url.host == "huggingface.co":
            if status:
                return httpx.Response(status)
            return httpx.Response(302, headers={"Location": f"https://{CDN_HOST}/blob?sig=1",
                                                "x-linked-size": str(len(payload))})
        rng = request.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            body = payload[start:]
            return httpx.Response(206, content=body, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})
    return handler


# --- URL handling ------------------------------------------------------------ #

def test_normalize_hf_blob_link_becomes_resolve():
    url = "https://huggingface.co/org/repo/blob/main/sub/model.safetensors?download=true"
    assert mm.normalize_url(url) == "https://huggingface.co/org/repo/resolve/main/sub/model.safetensors"


def test_normalize_hf_dataset_link():
    url = "https://huggingface.co/datasets/org/data/blob/v1/a.bin"
    assert mm.normalize_url(url) == "https://huggingface.co/datasets/org/data/resolve/v1/a.bin"


@pytest.mark.parametrize("url", [
    "https://huggingface.co/org/repo",
    "https://huggingface.co/org/repo/tree/main/split_files",
])
def test_normalize_hf_repo_link_is_rejected(url):
    with pytest.raises(mm.UrlError, match="not a file"):
        mm.normalize_url(url)


def test_normalize_civitai_links():
    page = "https://civitai.com/models/4384/dreamshaper?modelVersionId=128713"
    assert mm.normalize_url(page) == "https://civitai.com/api/download/models/128713"
    assert mm.normalize_url("https://civitai.com/models/4384/dreamshaper") == "https://civitai.com/models/4384"
    dl = "https://civitai.com/api/download/models/14856?type=Model&format=SafeTensor"
    assert mm.normalize_url(dl) == dl
    with pytest.raises(mm.UrlError):
        mm.normalize_url("https://civitai.com/images/123")


def test_normalize_rejects_non_http():
    with pytest.raises(mm.UrlError):
        mm.normalize_url("file:///etc/passwd")


@pytest.mark.parametrize("url,expected", [
    (HF_URL, "loras"),
    ("https://huggingface.co/Comfy-Org/Wan/resolve/main/split_files/diffusion_models/wan.safetensors", "diffusion_models"),
    ("https://huggingface.co/a/b/resolve/main/unet/x.safetensors", "diffusion_models"),
    ("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp16.safetensors", "text_encoders"),
    ("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors", "text_encoders"),
    ("https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors", "vae"),
    ("https://huggingface.co/stabilityai/sdxl-vae/resolve/main/sdxl_vae.safetensors", "vae"),
    ("https://huggingface.co/XLabs-AI/flux-RealismLora/resolve/main/model.safetensors", "loras"),
    ("https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth", "upscale_models"),
    ("https://huggingface.co/Comfy-Org/sigclip_vision_384/resolve/main/sigclip_vision_patch14_384.safetensors", "clip_vision"),
    ("https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors", None),
])
def test_guess_folder(url, expected):
    assert mm.guess_folder(url, None) == expected


@pytest.mark.parametrize("header,expected", [
    ('attachment; filename="model.safetensors"', "model.safetensors"),
    ("attachment; filename=plain.bin", "plain.bin"),
    ("attachment; filename*=UTF-8''caf%C3%A9.safetensors", "café.safetensors"),
    ('attachment; filename="../../etc/passwd"', "passwd"),
    ('attachment; filename=".bashrc"', None),
    (None, None),
])
def test_filename_from_disposition(header, expected):
    assert mm.filename_from_disposition(header) == expected


@pytest.mark.parametrize("folder", ["../etc", "/../x", "loras/../../x", ""])
def test_validate_folder_rejects_traversal(folder):
    with pytest.raises(mm.UrlError):
        mm.validate_folder(folder)


def test_resolve_target_stays_inside(env):
    assert mm.resolve_target("loras", "a.safetensors") == str(env / "loras" / "a.safetensors")
    with pytest.raises(mm.UrlError):
        mm.resolve_target("loras", "../../outside")


def test_auth_headers_only_for_matching_https_host(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("CIVITAI_TOKEN", "cv_secret")
    assert mm.auth_headers(HF_URL) == {"Authorization": "Bearer hf_secret"}
    assert mm.auth_headers("https://civitai.com/api/download/models/1") == {"Authorization": "Bearer cv_secret"}
    assert mm.auth_headers("http://huggingface.co/a/b/resolve/main/c") == {}
    assert mm.auth_headers("https://example.com/huggingface.co/x") == {}
    assert mm.auth_headers("https://huggingface.co.evil.com/x") == {}


# --- Downloads --------------------------------------------------------------- #

def test_download_writes_file_and_never_sends_token_to_cdn(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    payload = os.urandom(10_000)
    seen = []
    manager = make_manager(hf_handler(payload, seen))
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))

    assert job.status == "completed", job.error
    assert (env / "loras" / "style.safetensors").read_bytes() == payload
    assert not (env / "loras" / "style.safetensors.part").exists()
    hf_req = [r for r in seen if r.url.host == "huggingface.co"]
    cdn_req = [r for r in seen if r.url.host == CDN_HOST]
    assert hf_req[0].headers["authorization"] == "Bearer hf_secret"
    assert "authorization" not in cdn_req[0].headers
    assert mm.read_state()[HF_URL] == "loras/style.safetensors"


def test_download_resumes_from_part_file(env):
    payload = os.urandom(10_000)
    (env / "loras").mkdir()
    (env / "loras" / "style.safetensors.part").write_bytes(payload[:4000])
    seen = []
    manager = make_manager(hf_handler(payload, seen))
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))

    assert job.status == "completed", job.error
    assert (env / "loras" / "style.safetensors").read_bytes() == payload
    assert [r.headers.get("range") for r in seen if r.url.host == CDN_HOST] == ["bytes=4000-"]


def test_gated_model_reports_token_hint(env):
    manager = make_manager(hf_handler(b"", [], status=401))
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))
    assert job.status == "failed"
    assert "HF_TOKEN" in job.error


def test_filename_from_content_disposition_when_unknown(env):
    def handler(request):
        if request.url.host == "civitai.com":
            return httpx.Response(307, headers={"Location": "https://storage.example/obj/abc?X-Sig=1"})
        return httpx.Response(200, content=b"weights", headers={
            "content-length": "7", "content-disposition": 'attachment; filename="myLora_v2.safetensors"'})
    manager = make_manager(handler)
    job = wait(manager.submit("https://civitai.com/api/download/models/42", "loras"))
    assert job.status == "completed", job.error
    assert job.filename == "myLora_v2.safetensors"
    assert (env / "loras" / "myLora_v2.safetensors").read_bytes() == b"weights"


def test_existing_file_is_skipped_without_request(env):
    (env / "loras").mkdir()
    (env / "loras" / "style.safetensors").write_bytes(b"x")
    seen = []
    manager = make_manager(hf_handler(b"new", seen))
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))
    assert job.status == "skipped"
    assert seen == []


def test_not_enough_disk_space(env, monkeypatch):
    usage = type("U", (), {"total": 10**9, "used": 0, "free": 1000})
    monkeypatch.setattr(mm.shutil, "disk_usage", lambda path: usage)
    manager = make_manager(hf_handler(os.urandom(5000), []))
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))
    assert job.status == "failed"
    assert "disk space" in job.error


def test_server_error_is_retried(env):
    calls = {"n": 0}
    payload = b"abc" * 100
    good = hf_handler(payload, [])

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return good(request)
    manager = make_manager(handler)
    job = wait(manager.submit(HF_URL, "loras", "style.safetensors"))
    assert job.status == "completed", job.error
    assert (env / "loras" / "style.safetensors").read_bytes() == payload


def test_cancel_removes_partial_file(env):
    holder = {}

    def chunks():
        yield b"a" * 1000
        holder["job"].cancel.set()
        yield b"b" * 1000

    def handler(request):
        return httpx.Response(200, content=chunks(), headers={"content-length": "2000"})
    manager = make_manager(handler)
    holder["job"] = job = manager.submit("https://example.com/f.safetensors", "loras", "f.safetensors")
    wait(job)
    assert job.status == "cancelled"
    assert not (env / "loras" / "f.safetensors.part").exists()
    assert not (env / "loras" / "f.safetensors").exists()


def test_duplicate_submit_returns_same_job_and_name_clash_is_refused(env):
    def handler(request):
        def slow():
            time.sleep(0.3)
            yield b"x"
        return httpx.Response(200, content=slow(), headers={"content-length": "1"})
    manager = make_manager(handler)
    first = manager.submit("https://example.com/a.bin", "loras", "a.bin")
    assert manager.submit("https://example.com/a.bin", "loras", "a.bin") is first
    with pytest.raises(mm.UrlError, match="already downloading"):
        manager.submit("https://other.example/a.bin", "loras", "a.bin")
    wait(first)


# --- Probe ------------------------------------------------------------------- #

def test_probe_hf_file_reports_size_and_folder(env):
    client = httpx.Client(transport=httpx.MockTransport(hf_handler(b"z" * 1234, [])))
    result = mm.probe(HF_URL.replace("/resolve/", "/blob/"), client)
    assert result["status"] == "ok"
    assert result["url"] == HF_URL
    assert result["filename"] == "style.safetensors"
    assert result["folder"] == "loras"
    assert result["size"] == 1234


def test_probe_gated_hf_file(env):
    client = httpx.Client(transport=httpx.MockTransport(hf_handler(b"", [], status=401)))
    result = mm.probe(HF_URL, client)
    assert result["status"] == "auth_required"
    assert "HF_TOKEN" in result["message"]


def test_probe_civitai_uses_api_metadata(env):
    def handler(request):
        if request.url.path == "/api/v1/model-versions/128713":
            return httpx.Response(200, json={
                "name": "v8", "model": {"name": "DreamShaper", "type": "LORA"},
                "files": [{"name": "dreamshaper_8.safetensors", "sizeKB": 2048, "primary": True}]})
        return httpx.Response(307, headers={"Location": "https://storage.example/x"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = mm.probe("https://civitai.com/models/4384/dreamshaper?modelVersionId=128713", client)
    assert result["status"] == "ok", result["message"]
    assert result["url"] == "https://civitai.com/api/download/models/128713"
    assert result["filename"] == "dreamshaper_8.safetensors"
    assert result["folder"] == "loras"
    assert result["size"] == 2048 * 1024
    assert result["name"] == "DreamShaper — v8"


# --- HTTP routes ------------------------------------------------------------- #

@pytest.fixture
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mm.router)
    return TestClient(app)


def test_status_route(client, env):
    data = client.get("/models/status").json()
    assert data["enabled"] is True
    assert data["models_dir"] == str(env)
    assert "loras" in data["folders"]
    assert data["tokens"] == {"hf": False, "civitai": False}


def test_list_add_remove_and_export(client, env, tmp_path):
    blob = "https://huggingface.co/org/repo/blob/main/x.safetensors"
    assert client.post("/models/list", json={"url": blob, "folder": "loras"}).status_code == 200
    items = client.get("/models/list").json()["items"]
    assert [(i["url"], i["folder"], i["installed"]) for i in items] == [
        ("https://huggingface.co/org/repo/resolve/main/x.safetensors", "loras", False)]
    saved = yaml.safe_load((tmp_path / "model-list.yaml").read_text())
    assert saved["models"][0]["folder"] == "loras"

    exported = yaml.safe_load(client.get("/models/list/export").text)
    assert exported["models"][0]["url"].endswith("/resolve/main/x.safetensors")

    resp = client.post("/models/list/remove", json={"url": items[0]["url"], "folder": "loras"})
    assert resp.status_code == 200
    assert client.get("/models/list").json()["items"] == []


def test_list_merges_catalog_file(client, env, tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(yaml.safe_dump({"models": [
        {"url": "https://huggingface.co/a/b/resolve/main/vae/ae.safetensors"},
        {"url": "https://example.com/unknown.bin"},  # no folder, can't guess: skipped
    ]}))
    monkeypatch.setenv("MODEL_CATALOG", str(catalog))
    data = client.get("/models/list").json()
    assert [(i["folder"], i["source"]) for i in data["items"]] == [("vae", str(catalog))]
    assert data["sources"] == [{"source": str(catalog), "ok": True, "count": 1}]


def test_installed_lists_files_and_skips_placeholders(client, env):
    (env / "loras").mkdir()
    (env / "loras" / "a.safetensors").write_bytes(b"12345")
    (env / "loras" / "put_loras_here").write_bytes(b"")
    (env / "vae").mkdir()
    (env / "vae" / "big.safetensors.part").write_bytes(b"12")
    data = client.get("/models/installed").json()
    by_folder = {f["folder"]: f["files"] for f in data["folders"]}
    assert [f["path"] for f in by_folder["loras"]] == ["a.safetensors"]
    assert by_folder["vae"][0]["partial"] is True
    assert data["total_size"] == 7


def test_delete_installed_file_and_refuse_traversal(client, env, tmp_path):
    (env / "loras").mkdir()
    (env / "loras" / "a.safetensors").write_bytes(b"x")
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")

    resp = client.post("/models/installed/delete", json={"folder": "loras", "path": "../../keep.txt"})
    assert resp.status_code == 400
    assert outside.exists()

    resp = client.post("/models/installed/delete", json={"folder": "loras", "path": "a.safetensors"})
    assert resp.status_code == 200
    assert not (env / "loras" / "a.safetensors").exists()
    assert client.post("/models/installed/delete",
                       json={"folder": "loras", "path": "a.safetensors"}).status_code == 404


# --- Groups (setups + LoRA folders) ------------------------------------------ #

CATALOG_URL = "https://huggingface.co/me/comfy/resolve/main/catalog.yaml"
TREE_PREFIX = "/api/models/me/comfy/tree/main/"


def factory_for(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def private_repo_handler(catalog, tree, seen):
    """A private HF repo holding the catalog and LoRA folders: HF_TOKEN required."""
    def handler(request):
        seen.append(request)
        if request.headers.get("authorization") != "Bearer hf_secret":
            return httpx.Response(401)
        path = request.url.path
        if path == "/me/comfy/resolve/main/catalog.yaml":
            return httpx.Response(200, text=yaml.safe_dump(catalog, sort_keys=False))
        if path.startswith(TREE_PREFIX) and path[len(TREE_PREFIX):] in tree:
            return httpx.Response(200, json=tree[path[len(TREE_PREFIX):]])
        return httpx.Response(404)
    return handler


def test_groups_from_private_hf_catalog(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    catalog = {"groups": [{
        "name": "Krea 2",
        "setup": {
            "diffusion_models": "https://huggingface.co/me/comfy/resolve/main/diffusion_models/krast.safetensors",
            "vae": "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_fp32.safetensors",
            "text_encoders": ["https://huggingface.co/o/r/resolve/main/te_a.safetensors",
                              "https://huggingface.co/o/r/resolve/main/te_b.safetensors"],
        },
        "extras": {"diffusion_models": "https://huggingface.co/o/r/resolve/main/turbo.safetensors"},
        "loras": ["loras/krea2", "https://huggingface.co/o/r/blob/main/extra_style.safetensors"],
    }]}
    tree = {"loras/krea2": [
        {"type": "file", "path": "loras/krea2/b.safetensors", "size": 1, "lfs": {"size": 2048}},
        {"type": "file", "path": "loras/krea2/A.safetensors", "size": 1024},
        {"type": "file", "path": "loras/krea2/README.md", "size": 10},
        {"type": "directory", "path": "loras/krea2/old"},
    ]}
    (env / "vae").mkdir()
    (env / "vae" / "Wan2_1_VAE_fp32.safetensors").write_bytes(b"x")
    seen = []

    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(private_repo_handler(catalog, tree, seen)))

    assert data["sources"] == [{"source": CATALOG_URL, "ok": True, "count": 1}]
    [g] = data["groups"]
    assert (g["id"], g["name"], g["errors"]) == ("krea-2", "Krea 2", [])
    assert [(i["folder"], i["filename"], i["installed"]) for i in g["setup"]] == [
        ("diffusion_models", "krast.safetensors", False),
        ("vae", "Wan2_1_VAE_fp32.safetensors", True),
        ("text_encoders", "te_a.safetensors", False),
        ("text_encoders", "te_b.safetensors", False),
    ]
    assert [(i["folder"], i["filename"]) for i in g["extras"]] == [("diffusion_models", "turbo.safetensors")]
    assert [i["filename"] for i in g["loras"]] == ["A.safetensors", "b.safetensors", "extra_style.safetensors"]
    assert g["loras"][0]["url"] == "https://huggingface.co/me/comfy/resolve/main/loras/krea2/A.safetensors"
    assert g["loras"][1]["size"] == 2048
    assert g["loras"][2]["url"] == "https://huggingface.co/o/r/resolve/main/extra_style.safetensors"
    assert {i["folder"] for i in g["loras"]} == {"loras"}
    assert seen and all(r.headers["authorization"] == "Bearer hf_secret" for r in seen)


def test_private_catalog_without_token_explains_why(env, monkeypatch):
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(private_repo_handler({}, {}, [])))
    [source] = data["sources"]
    assert source["ok"] is False and "HF_TOKEN" in source["error"]
    assert data["groups"] == []


def test_missing_lora_folder_keeps_the_group(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    catalog = {"groups": [{"name": "Moss", "setup": {"vae": "https://huggingface.co/o/r/resolve/main/vae.safetensors"},
                           "loras": "loras/moss"}]}
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(private_repo_handler(catalog, {}, [])))
    [g] = data["groups"]
    assert len(g["setup"]) == 1 and g["loras"] == []
    assert g["errors"] and "loras/moss" in g["errors"][0] and "not found" in g["errors"][0]


def test_relative_lora_folder_needs_a_catalog_on_hf(env, monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(yaml.safe_dump({"groups": [{"name": "Moss", "loras": "loras/moss"}]}))
    monkeypatch.setenv("MODEL_CATALOG", str(catalog))
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(lambda r: httpx.Response(500)))
    [g] = data["groups"]
    assert g["loras"] == [] and "hosted on Hugging Face" in g["errors"][0]


def test_lora_folder_link_is_listed_across_pages(env, monkeypatch, tmp_path):
    page2 = "https://huggingface.co/api/models/o/r/tree/main/loras?recursive=true&cursor=abc"

    def handler(request):
        assert request.url.path == "/api/models/o/r/tree/main/loras"
        if request.url.params.get("cursor") == "abc":
            return httpx.Response(200, json=[{"type": "file", "path": "loras/z.safetensors", "size": 5}])
        return httpx.Response(200, json=[{"type": "file", "path": "loras/a.safetensors", "size": 5}],
                              headers={"Link": f'<{page2}>; rel="next"'})

    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(yaml.safe_dump({"groups": [
        {"name": "Flux", "loras": "https://huggingface.co/o/r/tree/main/loras"},
        {"name": "flux"},  # same id as above: ignored
    ]}))
    monkeypatch.setenv("MODEL_CATALOG", str(catalog))
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(handler))
    [g] = data["groups"]
    assert [i["filename"] for i in g["loras"]] == ["a.safetensors", "z.safetensors"]


def test_groups_route_and_list_ignores_groups(client, env, tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(yaml.safe_dump({
        "groups": [{"name": "Krea 2", "description": "base",
                    "setup": {"vae": ["https://huggingface.co/o/r/blob/main/vae.safetensors"]}}],
        "models": [{"url": "https://huggingface.co/a/b/resolve/main/loras/x.safetensors"}],
    }))
    monkeypatch.setenv("MODEL_CATALOG", str(catalog))
    [g] = client.get("/models/groups").json()["groups"]
    assert g["description"] == "base"
    assert [(i["url"], i["folder"], i["installed"], i["job_id"]) for i in g["setup"]] == [
        ("https://huggingface.co/o/r/resolve/main/vae.safetensors", "vae", False, None)]
    assert [i["folder"] for i in client.get("/models/list").json()["items"]] == ["loras"]


# --- Characters -------------------------------------------------------------- #

def characters_repo(seen):
    tree = {"loras/characters": [
        {"type": "file", "path": "loras/characters/Alice/cover.jpg", "size": 10},
        {"type": "file", "path": "loras/characters/Alice/krea/alice_krea.safetensors", "size": 100},
        {"type": "file", "path": "loras/characters/Alice/moss/alice_moss.safetensors", "size": 200},
        {"type": "file", "path": "loras/characters/Bob/krea/bob_v1.safetensors", "size": 300},
        {"type": "file", "path": "loras/characters/Bob/krea/bob_v2.safetensors", "size": 400},
        {"type": "file", "path": "loras/characters/Bob/notes.txt", "size": 1},
        {"type": "file", "path": "loras/characters/README.md", "size": 1},
    ]}
    catalog = {"characters": "loras/characters", "groups": [
        {"id": "krea", "name": "Krea 2"}, {"id": "moss", "name": "MOSS"}, {"id": "flux", "name": "Flux"}]}

    def handler(request):
        seen.append(request)
        if request.headers.get("authorization") != "Bearer hf_secret":
            return httpx.Response(401)
        if request.url.path == "/me/comfy/resolve/main/loras/characters/Alice/cover.jpg":
            return httpx.Response(200, content=b"JPEGDATA")
        return private_repo_handler(catalog, tree, [])(request)
    return handler


def test_characters_are_shown_in_the_groups_that_have_their_subfolder(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    (env / "loras").mkdir()
    (env / "loras" / "bob_v1.safetensors").write_bytes(b"x")
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(characters_repo([])))
    assert data["errors"] == []
    krea, moss, flux = data["groups"]
    assert [c["name"] for c in krea["characters"]] == ["Alice", "Bob"]
    alice, bob = krea["characters"]
    assert alice["image"].startswith("/models/image/") and bob["image"] is None
    assert [(i["folder"], i["filename"], i["size"]) for i in alice["loras"]] == [("loras", "alice_krea.safetensors", 100)]
    assert [(i["filename"], i["installed"]) for i in bob["loras"]] == [
        ("bob_v1.safetensors", True), ("bob_v2.safetensors", False)]
    assert [(c["name"], [i["filename"] for i in c["loras"]]) for c in moss["characters"]] == [
        ("Alice", ["alice_moss.safetensors"])]
    assert flux["characters"] == []


def test_character_image_is_proxied_with_the_token_and_only_if_listed(client, env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    seen = []
    monkeypatch.setattr(mm, "default_client", factory_for(characters_repo(seen)))
    image = client.get("/models/groups").json()["groups"][0]["characters"][0]["image"]
    resp = client.get(image)
    assert resp.status_code == 200 and resp.content == b"JPEGDATA"
    assert resp.headers["content-type"] == "image/jpeg"
    assert seen[-1].headers["authorization"] == "Bearer hf_secret"
    assert client.get("/models/image/0123456789abcdef0123").status_code == 404


def test_missing_characters_folder_is_reported(env, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("MODEL_CATALOG", CATALOG_URL)
    catalog = {"characters": "loras/chars", "groups": [{"name": "Krea 2"}]}
    data = mm.build_groups(mm.DownloadManager(concurrency=1), factory_for(private_repo_handler(catalog, {}, [])))
    assert data["groups"][0]["characters"] == []
    assert len(data["errors"]) == 1 and "loras/chars" in data["errors"][0]
