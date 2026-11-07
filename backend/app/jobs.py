import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config_store, library, metadata_fetch, navidrome, paths, progress, streamrip_cli, url_resolve, youtube
from .schemas import DownloadByIdRequest, DownloadByUrlRequest, JobKind, JobOut, JobStatus
from .tagging import retag
from .tagging import staging as tagging_staging

MAX_LOG_LINES = 2000
MAX_HISTORY = 300
CANCELED_BY_USER = "Canceled"
INTERRUPTED = "Interrupted by restart"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: JobKind
    label: str
    payload: dict[str, Any]
    status: JobStatus = "queued"
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    bytes_downloaded: int = 0
    speed_mbps: float = 0.0
    percent: Optional[float] = None
    # Files this job put in the library. None means not recorded (job predates
    # this), so there's nothing safe to delete.
    files: Optional[list[str]] = None
    library_root: Optional[str] = None
    track_ids: list[str] = field(default_factory=list)
    # Runtime only. Once the download itself is done the job is moving files
    # into the library, which must not be cut off halfway.
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    past_download: bool = False

    def to_out(self) -> JobOut:
        return JobOut(
            id=self.id,
            kind=self.kind,
            label=self.label,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            log=self.log,
            error=self.error,
            bytes_downloaded=self.bytes_downloaded,
            speed_mbps=self.speed_mbps,
            percent=self.percent,
            removable=self.status == "success" and self.files is not None and bool(self.library_root),
            redownloadable=self.status in ("failed", "canceled", "removed") and bool(self.payload),
            cancelable=self.status == "queued" or (self.status == "running" and not self.past_download),
        )

    def to_record(self) -> dict:
        return {
            **self.to_out().model_dump(),
            "payload": self.payload,
            "files": self.files,
            "library_root": self.library_root,
            "track_ids": self.track_ids,
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers: set[asyncio.Queue] = set()
        self._worker_task: Optional[asyncio.Task] = None

    def load_history(self) -> None:
        if not paths.JOBS_HISTORY_PATH.exists():
            return
        try:
            raw = json.loads(paths.JOBS_HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw:
            job = Job(
                id=item["id"],
                kind=item["kind"],
                label=item["label"],
                payload=item.get("payload") or {},
                status=item["status"],
                created_at=item["created_at"],
                started_at=item.get("started_at"),
                finished_at=item.get("finished_at"),
                log=item.get("log", []),
                error=item.get("error"),
                bytes_downloaded=item.get("bytes_downloaded", 0),
                speed_mbps=item.get("speed_mbps", 0.0),
                percent=item.get("percent"),
                files=item.get("files"),
                library_root=item.get("library_root"),
                track_ids=item.get("track_ids", []),
            )
            if job.status in ("queued", "running"):
                job.status = "canceled"
                job.error = INTERRUPTED
                job.finished_at = job.finished_at or _now()
            self.jobs[job.id] = job

    def _persist(self) -> None:
        items = [j.to_record() for j in list(self.jobs.values())[-MAX_HISTORY:]]
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        paths.JOBS_HISTORY_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def start(self) -> None:
        self.load_history()
        self._worker_task = asyncio.create_task(self._worker())

    def list_jobs(self) -> list[JobOut]:
        return [j.to_out() for j in reversed(list(self.jobs.values()))]

    def get_job(self, job_id: str) -> Optional[JobOut]:
        job = self.jobs.get(job_id)
        return job.to_out() if job else None

    def enqueue(self, kind: JobKind, label: str, payload: dict[str, Any]) -> JobOut:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, payload=payload)
        self.jobs[job.id] = job
        self.queue.put_nowait(job.id)
        self._persist()
        self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})
        return job.to_out()

    def files_status(self, job_id: str) -> dict:
        job = self._removable(job_id)
        return library.file_status(job.files or [], job.library_root or "")

    def _removable(self, job_id: str) -> "Job":
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError("Job not found")
        if not job.to_out().removable:
            raise ValueError("This download can't be deleted from here")
        return job

    async def delete_job_files(self, job_id: str) -> dict:
        """Delete what a finished job put in the library and let streamrip
        download it again. Files that are already gone are simply skipped."""
        job = self._removable(job_id)
        status = library.file_status(job.files or [], job.library_root or "")
        deleted = library.delete_files(job.files or [], job.library_root or "")
        library.forget_ids(job.track_ids)
        job.status = "removed"
        job.files, job.track_ids = [], []
        note = f"[delete] Removed {deleted} file(s)" + (f", {status['missing']} were already gone." if status["missing"] else ".")
        job.log.append(note)
        self._persist()
        self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})
        if deleted:
            await navidrome.trigger_scan(config_store.load_settings().navidrome)
        return {"deleted": deleted, "missing": status["missing"]}

    def requeue(self, job_id: str) -> JobOut:
        """Queue a failed, canceled or deleted job again, replacing its row."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError("Job not found")
        if not job.to_out().redownloadable:
            raise ValueError("This download can't be queued again")
        del self.jobs[job_id]
        self._broadcast({"type": "job_removed", "job_id": job_id})
        return self.enqueue(job.kind, job.label, job.payload)

    def cancel(self, job_id: str) -> JobOut:
        """Stop a queued job before it starts, or a running one mid-download.
        Whatever a running job fetched so far is thrown away (it only ever
        lived in its staging folder) and the worker moves on to the next job."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError("Job not found")
        if not job.to_out().cancelable:
            raise ValueError("This download is already finishing or finished")
        if job.status == "queued":
            job.status = "canceled"
            job.error = CANCELED_BY_USER
            job.finished_at = _now()
            self._persist()
            self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})
        elif job.task is not None:
            job.task.cancel()
        return job.to_out()

    def _broadcast(self, event: dict) -> None:
        for sub in list(self.subscribers):
            sub.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":  # removed or canceled while waiting
                continue
            job.task = asyncio.create_task(self._run_job(job))
            await job.task

    async def _run_job(self, job: Job) -> None:
        job.status = "running"
        job.started_at = _now()
        self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})

        async def on_line(line: str) -> None:
            job.log.append(line)
            if len(job.log) > MAX_LOG_LINES:
                del job.log[: len(job.log) - MAX_LOG_LINES]
            self._broadcast({"type": "job_log", "job_id": job.id, "line": line})

        try:
            returncode = await self._execute(job, on_line)
            job.status = "success" if returncode == 0 else "failed"
            if returncode != 0:
                job.error = f"Download failed (exit code {returncode}), see the log"
                if any("Sign in to confirm" in line for line in job.log[-20:]):
                    job.error = "YouTube wants a signed-in session. Add cookies under Settings > YouTube."
        except asyncio.CancelledError:
            # Only this job's own task is cancelled; the worker carries on.
            job.status = "canceled"
            job.error = CANCELED_BY_USER
            job.log.append("[cancel] Stopped. Partial downloads were discarded.")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job.status = "failed"
            job.error = str(exc)
        job.task = None

        job.finished_at = _now()
        self._persist()
        self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})

        if job.status == "success":
            settings = config_store.load_settings()
            ok, message = await navidrome.trigger_scan(settings.navidrome)
            if settings.navidrome.enabled:
                note = f"Navidrome: {message}" if ok else f"Navidrome scan failed: {message}"
                job.log.append(note)
                self._persist()
                self._broadcast({"type": "job_log", "job_id": job.id, "line": note})

    async def _execute(self, job: Job, on_line) -> int:
        settings = config_store.load_settings()
        staging_dir = tagging_staging.create_staging_dir(job.id)

        try:
            source: Optional[str] = None
            media_type: Optional[str] = None
            item_id: Optional[str] = None
            tagging_pref: Optional[bool] = None

            yt_url: Optional[str] = None
            if job.kind == "id":
                req: Any = DownloadByIdRequest.model_validate(job.payload)
                source, media_type, item_id = req.source, req.media_type, req.id
                tagging_pref = req.navidrome_tagging
                if source == "youtube":
                    yt_url = youtube.watch_url(req.id)
            else:
                req = DownloadByUrlRequest.model_validate(job.payload)
                if youtube.is_youtube_url(req.url):
                    source, yt_url = "youtube", req.url
                parsed = url_resolve.parse_generic_url(req.url)
                if parsed:
                    source, media_type, item_id = parsed
                tagging_pref = req.navidrome_tagging

            tagging_wanted = tagging_pref if tagging_pref is not None else settings.tagging.enabled
            do_retag = source == "qobuz" and media_type in ("album", "track") and tagging_wanted

            # Fetch Qobuz metadata once, reused for both the size estimate
            # (progress bar) and retagging -- never fails the job if it errors.
            meta: Optional[dict] = None
            estimated_total: Optional[int] = None
            if source == "qobuz" and media_type and item_id:
                try:
                    meta = await metadata_fetch.fetch_metadata("qobuz", item_id, media_type)
                    estimated_total = progress.estimate_total_bytes(meta, settings.qobuz.quality)
                except Exception:  # noqa: BLE001 - progress/tagging are both best-effort
                    meta = None

            async def on_progress(bytes_so_far: int, speed_mbps: float, percent: Optional[float]) -> None:
                job.bytes_downloaded = bytes_so_far
                job.speed_mbps = speed_mbps
                job.percent = percent
                self._broadcast(
                    {
                        "type": "job_progress",
                        "job_id": job.id,
                        "bytes": bytes_so_far,
                        "speed_mbps": speed_mbps,
                        "percent": percent,
                    }
                )

            ids_before = library.downloaded_ids()
            tracker = progress.ProgressTracker(staging_dir, estimated_total, on_progress)
            tracker.start()
            try:
                try:
                    if yt_url:
                        returncode = await youtube.download(yt_url, staging_dir, on_line)
                    elif job.kind == "id":
                        returncode = await streamrip_cli.download_by_id(req, on_line, folder_override=str(staging_dir))
                    else:
                        returncode = await streamrip_cli.download_by_url(req, on_line, folder_override=str(staging_dir))
                finally:
                    await tracker.stop()
            except asyncio.CancelledError:
                # Tracks streamrip already logged as done were never kept, so
                # they must not be skipped when this is queued again.
                library.forget_ids(sorted(library.downloaded_ids() - ids_before))
                raise

            job.past_download = True
            self._broadcast({"type": "job_update", "job": job.to_out().model_dump()})
            job.track_ids = sorted(library.downloaded_ids() - ids_before)
            if returncode != 0:
                # Staging is thrown away on failure, so tracks streamrip already
                # marked as downloaded would be skipped (and lost) on a retry.
                library.forget_ids(job.track_ids)
                job.track_ids = []
                return returncode

            if do_retag:
                if meta is None:
                    await on_line("[tagging] Couldn't fetch Qobuz metadata, files kept as streamrip wrote them.")
                else:
                    try:
                        tier = settings.tagging.navidrome_version_tier
                        if media_type == "album":
                            notes = retag.retag_qobuz_album(staging_dir, meta, tier)
                        else:
                            notes = retag.retag_qobuz_track(staging_dir, meta, tier)
                        for note in notes:
                            await on_line(f"[tagging] {note}")
                    except Exception as exc:  # noqa: BLE001 - never let tagging sink the download
                        await on_line(
                            f"[tagging] Couldn't apply Navidrome-friendly tags, files kept as "
                            f"streamrip wrote them: {exc}"
                        )

            music_dir = config_store.resolve_download_folder(settings)
            moves = tagging_staging.move_into_library(staging_dir, music_dir, job.id)
            job.files = [m.dest for m in moves if m.action in ("moved", "overwritten")]
            job.library_root = str(music_dir)
            moved_count = sum(1 for m in moves if m.action == "moved")
            overwritten = [m for m in moves if m.action == "overwritten"]
            skipped_lower = [m for m in moves if m.action == "skipped_lower_quality"]
            for m in overwritten:
                await on_line(f"[move] Overwrote {m.dest}: {m.detail}")
            for m in skipped_lower:
                await on_line(f"[move] Skipped overwrite of {m.dest}: {m.detail}")
            await on_line(
                f"[move] SUMMARY: moved {moved_count} new, overwrote {len(overwritten)}, "
                f"skipped {len(skipped_lower)} (existing file was higher quality)."
            )
            return returncode
        finally:
            tagging_staging.cleanup_staging_dir(job.id)


manager = JobManager()
