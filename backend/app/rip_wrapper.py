"""`rip`, with Tidal's true hi-res FLAC support added.

streamrip 2.1.0 only knows Tidal's MQA tier ("HI_RES") and can't read the DASH manifests Tidal uses
for real hi-res FLAC ("HI_RES_LOSSLESS", up to 24-bit/192 kHz), so asking for its top tier gets MQA
or quietly drops to 16-bit. This wrapper patches the Tidal client's `get_downloadable` before
running the normal `rip` command: at the top quality it asks for hi-res FLAC, downloads the DASH
segments and remuxes them into a .flac with ffmpeg.

Anything unexpected falls back to what streamrip would have done on its own, so a download is never
worse than without this. Run it as: python rip_wrapper.py <the usual rip arguments>.
"""

import asyncio
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any, Callable, Optional

# Run as a script, this folder is first on the import path, and Lucid's own deezer.py would shadow
# the `deezer` package streamrip imports. Take the folder off the path before streamrip loads.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

HIRES = "HI_RES_LOSSLESS"
_NS = {"d": "urn:mpeg:dash:schema:mpd:2011"}
_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def parse_dash(manifest: bytes) -> Optional[dict]:
    """Segment URLs of a FLAC DASH manifest, or None if it isn't one we can handle
    (other codecs, encrypted, or a layout we don't recognise)."""
    root = ET.fromstring(manifest)
    if root.find(".//d:ContentProtection", _NS) is not None:
        return None
    rep = root.find(".//d:Representation", _NS)
    if rep is None or "flac" not in (rep.get("codecs") or "").lower():
        return None
    tmpl = rep.find("d:SegmentTemplate", _NS)
    if tmpl is None:
        tmpl = root.find(".//d:SegmentTemplate", _NS)
    if tmpl is None or not tmpl.get("initialization") or not tmpl.get("media"):
        return None
    base = (root.findtext("d:BaseURL", default="", namespaces=_NS) or "").strip()

    def absolute(url: str) -> str:
        return url if url.startswith("http") or not base else base.rstrip("/") + "/" + url.lstrip("/")

    number = int(tmpl.get("startNumber") or 1)
    count = 0
    for s in tmpl.findall("d:SegmentTimeline/d:S", _NS):
        count += 1 + int(s.get("r") or 0)
    if count == 0:
        return None
    media = tmpl.get("media")
    segments = [absolute(media.replace("$Number$", str(number + i))) for i in range(count)]

    size = None
    m = _DURATION.fullmatch(root.get("mediaPresentationDuration") or "")
    if m and rep.get("bandwidth"):
        seconds = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
        size = int(seconds * int(rep.get("bandwidth")) / 8)
    return {"init": absolute(tmpl.get("initialization")), "segments": segments, "size": size}


def _install() -> None:
    from streamrip.client import tidal
    from streamrip.client.downloadable import Downloadable, TidalDownloadable

    original = tidal.TidalClient.get_downloadable

    class DashFlac(Downloadable):
        """A hi-res FLAC track delivered as DASH segments."""

        def __init__(self, session: Any, plan: dict, fallback: Callable[[], Any]):
            self.session = session
            self.url = plan["init"]
            self.extension = "flac"
            self.source = "tidal"
            self._size = plan["size"]
            self._plan = plan
            self._fallback = fallback

        async def _fetch(self, url: str, out, callback) -> None:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(1 << 17):
                    out.write(chunk)
                    callback(len(chunk))

        async def _download(self, path: str, callback) -> None:
            tmp = f"{path}.segments.mp4"
            try:
                with open(tmp, "wb") as out:
                    for url in [self._plan["init"], *self._plan["segments"]]:
                        await self._fetch(url, out, callback)
                # Re-encoding FLAC to FLAC is lossless (identical samples, same bit depth and rate) and,
                # unlike a plain copy, writes the track length into the file, so players and Navidrome
                # show the right duration.
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-vn", "-c:a", "flac", "-f", "flac", path,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode != 0 or not os.path.getsize(path):
                    raise RuntimeError(err.decode(errors="replace")[:300])
            except Exception as exc:  # noqa: BLE001
                print(f"[hi-res] Couldn't fetch hi-res FLAC ({exc}); using the regular quality instead.", flush=True)
                for p in (path,):
                    if os.path.exists(p):
                        os.remove(p)
                await (await self._fallback())._download(path, lambda n: None)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    async def get_downloadable(self, track_id: str, quality: int):
        if quality < 3:
            return await original(self, track_id, quality)
        try:
            resp = await self._api_request(
                f"tracks/{track_id}/playbackinfopostpaywall",
                {"audioquality": HIRES, "playbackmode": "STREAM", "assetpresentation": "FULL"},
            )
            raw = base64.b64decode(resp["manifest"])
            if (resp.get("manifestMimeType") or "").endswith("dash+xml") or raw.lstrip().startswith(b"<"):
                plan = parse_dash(raw)
                if plan:
                    return DashFlac(self.session, plan, lambda: original(self, track_id, quality))
            else:
                manifest = json.loads(raw.decode("utf-8"))
                if manifest.get("urls") and "flac" in (manifest.get("codecs") or "").lower():
                    key = None if manifest.get("encryptionType") == "NONE" else manifest.get("keyId")
                    return TidalDownloadable(self.session, manifest["urls"][0], manifest["codecs"], key, manifest.get("restrictions"))
        except Exception as exc:  # noqa: BLE001 - never worse than stock streamrip
            print(f"[hi-res] Hi-res lookup failed ({type(exc).__name__}); using the regular quality.", flush=True)
        return await original(self, track_id, quality)

    tidal.TidalClient.get_downloadable = get_downloadable


def main() -> None:
    _install()
    from streamrip.rip import rip

    sys.argv[0] = re.sub(r"(-script\.pyw|\.exe)?$", "", sys.argv[0])
    sys.exit(rip())


if __name__ == "__main__":
    main()
