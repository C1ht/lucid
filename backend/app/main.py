import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config_store, navidrome, paths
from .monitor import monitor
from .browse import BrowseError, browser
from .jobs import manager
from .schemas import (
    DownloadByIdRequest,
    DownloadByUrlRequest,
    NavidromeSettings,
    SearchRequest,
    Settings,
)
from .security import check_header, require_auth
from .streamrip_cli import search as rip_search
from .url_resolve import resolve_url_label

FRONTEND_DIR = Path(os.environ.get("LUCID_FRONTEND_DIR", "/app/frontend"))

app = FastAPI(title="Lucid")


@app.on_event("startup")
async def on_startup() -> None:
    paths.ensure_dirs()
    config_store.ensure_streamrip_config()
    manager.start()
    monitor.start()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings() -> dict:
    return config_store.redact(config_store.load_settings())


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def put_settings(settings: Settings) -> dict:
    try:
        saved = config_store.save_settings(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return config_store.redact(saved)


@app.post("/api/settings/navidrome/test", dependencies=[Depends(require_auth)])
async def test_navidrome(nav: NavidromeSettings) -> dict:
    ok, message = await navidrome.test_connection(nav)
    return {"ok": ok, "message": message}


@app.post("/api/search", dependencies=[Depends(require_auth)])
async def search_endpoint(req: SearchRequest) -> list[dict]:
    try:
        results = await rip_search(req.source, req.media_type, req.query)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [r.model_dump() for r in results]


def _browse(call):
    async def handler(*args) -> dict:
        try:
            return await call(*args)
        except BrowseError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    return handler


@app.get("/api/browse/search", dependencies=[Depends(require_auth)])
async def browse_search(q: str) -> dict:
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Type at least 2 characters")
    return await _browse(browser.search)(q)


@app.get("/api/browse/album/{album_id}", dependencies=[Depends(require_auth)])
async def browse_album(album_id: str) -> dict:
    return await _browse(browser.album)(album_id)


@app.get("/api/browse/artist/{artist_id}", dependencies=[Depends(require_auth)])
async def browse_artist(artist_id: str) -> dict:
    return await _browse(browser.artist)(artist_id)


@app.get("/api/browse/track/{track_id}", dependencies=[Depends(require_auth)])
async def browse_track(track_id: str) -> dict:
    return await _browse(browser.track)(track_id)


@app.get("/api/browse/playlist/{playlist_id}", dependencies=[Depends(require_auth)])
async def browse_playlist(playlist_id: str) -> dict:
    return await _browse(browser.playlist)(playlist_id)


@app.post("/api/download", dependencies=[Depends(require_auth)])
async def download_by_id(req: DownloadByIdRequest) -> dict:
    label = req.desc or f"{req.source} {req.media_type} {req.id}"
    job = manager.enqueue("id", label, req.model_dump())
    return job.model_dump()


@app.post("/api/download/url", dependencies=[Depends(require_auth)])
async def download_by_url(req: DownloadByUrlRequest) -> dict:
    label, _meta = await resolve_url_label(req.url)
    job = manager.enqueue("url", label, req.model_dump())
    return job.model_dump()


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def list_jobs() -> list[dict]:
    return [j.model_dump() for j in manager.list_jobs()]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: str) -> dict:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.model_dump()


class MonitorArtistRequest(BaseModel):
    id: str


class MonitorScheduleRequest(BaseModel):
    enabled: bool
    time: str


@app.get("/api/monitor", dependencies=[Depends(require_auth)])
async def get_monitor() -> dict:
    return monitor.state()


@app.put("/api/monitor/schedule", dependencies=[Depends(require_auth)])
async def put_monitor_schedule(req: MonitorScheduleRequest) -> dict:
    try:
        return monitor.set_schedule(req.enabled, req.time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/monitor/artists", dependencies=[Depends(require_auth)])
async def add_monitored_artist(req: MonitorArtistRequest) -> dict:
    try:
        return await monitor.add_artist(req.id)
    except BrowseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.delete("/api/monitor/artists/{artist_id}", dependencies=[Depends(require_auth)])
async def remove_monitored_artist(artist_id: str) -> dict:
    return monitor.remove_artist(artist_id)


@app.post("/api/monitor/check", dependencies=[Depends(require_auth)])
async def check_monitor_now() -> dict:
    await monitor.run_check()
    return monitor.state()


@app.websocket("/api/ws")
async def ws_events(websocket: WebSocket) -> None:
    if not check_header(websocket.headers.get("authorization")):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = manager.subscribe()
    try:
        await websocket.send_json({"type": "job_list", "jobs": [j.model_dump() for j in manager.list_jobs()]})
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(queue)


def _serve(filename: str, media_type: str):
    async def handler() -> FileResponse:
        path = FRONTEND_DIR / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        # The desktop app always navigates to this same URL, including across
        # app updates with new frontend code -- without this, WebView2 (and
        # browsers) can keep serving a stale cached copy indefinitely.
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    return handler


@app.get("/inter.woff2")
async def font() -> FileResponse:
    path = FRONTEND_DIR / "inter.woff2"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Unlike the pages, the font never changes between app versions, so it can be cached.
    return FileResponse(path, media_type="font/woff2", headers={"Cache-Control": "public, max-age=604800"})


app.get("/", dependencies=[Depends(require_auth)])(_serve("index.html", "text/html"))
app.get("/styles.css", dependencies=[Depends(require_auth)])(_serve("styles.css", "text/css"))
app.get("/app.js", dependencies=[Depends(require_auth)])(_serve("app.js", "application/javascript"))
app.get("/monitor.js", dependencies=[Depends(require_auth)])(_serve("monitor.js", "application/javascript"))
app.get("/browse.js", dependencies=[Depends(require_auth)])(_serve("browse.js", "application/javascript"))
