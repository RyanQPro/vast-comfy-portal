"""Run the real Instance Portal locally with a fake ComfyUI models folder."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(HERE, "portal-aio", "portal")
DEV = os.path.join(HERE, ".dev")
MODELS = os.path.join(DEV, "ComfyUI", "models")

for folder in ("checkpoints", "loras", "vae", "text_encoders", "diffusion_models",
               "clip_vision", "controlnet", "upscale_models", "embeddings"):
    os.makedirs(os.path.join(MODELS, folder), exist_ok=True)
    open(os.path.join(MODELS, folder, f"put_{folder}_here"), "a").close()

os.environ.setdefault("WORKSPACE", DEV)
os.environ.setdefault("VAST_CONTAINERLABEL", "C.local")
os.chdir(PORTAL)
sys.path.insert(0, PORTAL)

import portal  # noqa: E402

FAKE_APPS = {
    "Instance Portal": {"hostname": "localhost", "external_port": 1111, "internal_port": 11111, "open_path": "/", "name": "Instance Portal"},
    "ComfyUI": {"hostname": "localhost", "external_port": 8188, "internal_port": 18188, "open_path": "/", "name": "ComfyUI"},
}
portal.load_config = lambda: portal.hydrate_applications({k: dict(v) for k, v in FAKE_APPS.items()})


async def _no_tunnel_manager(method, path):
    # No tunnel manager locally: answer like it does when nothing is configured.
    if path.startswith("/get-all-quick-tunnels") or path.startswith("/get-named-tunnels"):
        return {}
    if path.startswith("/get-direct-url/"):
        return {"result": f"http://localhost:{path.rsplit('/', 1)[-1]}"}
    raise portal.httpx.HTTPStatusError("not found", request=portal.httpx.Request(method, "http://x"),
                                       response=portal.httpx.Response(404))


portal._tunnel_proxy = _no_tunnel_manager

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(portal.app, host="127.0.0.1", port=int(os.environ.get("PORT", "11111")), log_level="warning")
