import os
import re
import base64
import shutil
import tempfile
import asyncio
import logging
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

app = FastAPI(title="HSdownloder College Project", version="7.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hsdownloader")

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Settings (all optional, set them as environment variables on Render)
#   PROXY_URL       e.g. http://user:pass@host:port  (residential proxy)
#   COOKIES_B64     base64 of your cookies.txt (alternative to Secret Files)
#   COOKIE_BROWSER  browser name for local use, e.g. firefox
#   DENO_PATH       custom path to the deno binary
# ---------------------------------------------------------------------------
PROXY_URL = os.environ.get("PROXY_URL") or None

COOKIE_BROWSER = os.environ.get("COOKIE_BROWSER") or (
    None if os.environ.get("RENDER") else "chrome"
)

# If COOKIES_B64 is set, write it to a file once at startup
_B64_COOKIE_PATH = Path(tempfile.gettempdir()) / "hs_cookies_from_env.txt"
if os.environ.get("COOKIES_B64"):
    try:
        _B64_COOKIE_PATH.write_bytes(base64.b64decode(os.environ["COOKIES_B64"]))
    except Exception as exc:
        logger.warning("Could not decode COOKIES_B64: %s", exc)

COOKIE_FILE_CANDIDATES = [
    BASE_DIR / "cookies.txt",
    Path("/etc/secrets/cookies.txt"),  # Render Secret Files
    _B64_COOKIE_PATH,
]


def find_cookies_file():
    for path in COOKIE_FILE_CANDIDATES:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def get_deno_path():
    """Find the Deno runtime (needed by yt-dlp to solve YouTube challenges)."""
    candidates = [
        os.environ.get("DENO_PATH"),
        str(BASE_DIR / ".deno" / "bin" / "deno"),
        shutil.which("deno"),
        "/opt/render/.deno/bin/deno",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DownloadRequest(BaseModel):
    url: str
    format: str
    quality: str = "720p"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        value = value.strip()
        if not value.startswith(("https://", "http://")):
            raise ValueError("A valid HTTP or HTTPS URL is required.")
        return value

    @field_validator("format")
    @classmethod
    def validate_format(cls, value):
        value = value.strip().lower()
        if value not in ("mp3", "mp4"):
            raise ValueError("Format must be mp3 or mp4.")
        return value


def cleanup_file(path: str):
    shutil.rmtree(path, ignore_errors=True)


def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def get_video_height(quality: str) -> int:
    match = re.search(r"(\d{3,4})", quality or "")
    return int(match.group(1)) if match else 720


def build_ydl_opts(req, temp_dir, use_browser_cookies, stop_event):
    def progress_hook(data):
        if stop_event.is_set():
            raise RuntimeError("Download cancelled.")

    height = get_video_height(req.quality)

    opts = {
        "outtmpl": os.path.join(temp_dir, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "continuedl": True,
        "overwrites": True,
        "windowsfilenames": True,
        "progress_hooks": [progress_hook],
        "format": "bestaudio/best" if req.format == "mp3" else "bestvideo+bestaudio/best",
        # Lets yt-dlp fetch the challenge solver if the yt-dlp-ejs package is missing
        "remote_components": ["ejs:github"],
    }

    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    deno = get_deno_path()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    else:
        logger.warning("Deno not found; YouTube challenge solving may fail.")

    if req.format != "mp3":
        opts["format_sort"] = [f"res:{height}"]

    cookies_file = find_cookies_file()
    if cookies_file:
        # Copy it: yt-dlp writes to the cookie file and secret files are read-only.
        cookie_copy = os.path.join(temp_dir, "cookies.txt")
        shutil.copyfile(cookies_file, cookie_copy)
        opts["cookiefile"] = cookie_copy
    elif use_browser_cookies and COOKIE_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIE_BROWSER,)

    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path

    if req.format == "mp3":
        match = re.search(r"\d+", req.quality or "")
        bitrate = int(match.group(0)) if match else 192
        if bitrate not in (64, 96, 128, 160, 192, 256, 320):
            bitrate = 192

        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(bitrate),
        }]
    else:
        opts["merge_output_format"] = "mp4"

    return opts


def run_download(url, opts, fmt, temp_dir):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title") or "media"
    video_id = info.get("id") or ""

    extensions = (
        (".mp3",) if fmt == "mp3" else (".mp4", ".mkv", ".webm", ".mov")
    )

    candidates = []
    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)
        if (
            os.path.isfile(path)
            and name.lower().endswith(extensions)
            and name != "cookies.txt"
        ):
            candidates.append(path)

    if video_id:
        for path in candidates:
            if video_id in os.path.basename(path):
                return path, title

    if candidates:
        return max(candidates, key=os.path.getsize), title

    return None, title


def friendly_error(exc: Exception) -> str:
    """Turn the raw yt-dlp error into a clearer message."""
    text = str(exc)
    lowered = text.lower()

    if "not a bot" in lowered or "sign in to confirm" in lowered:
        if find_cookies_file() and PROXY_URL:
            return (
                "YouTube still blocked the request even with cookies and a proxy. "
                "Export fresh cookies, or try a different residential proxy."
            )
        if find_cookies_file():
            return (
                "YouTube blocked this request even with cookies. The cookies may "
                "have expired, or this server's IP is blocked by YouTube. Export "
                "fresh cookies, or set PROXY_URL to a residential proxy."
            )
        return (
            "YouTube blocked this request (bot check) and no cookies.txt was "
            "found on the server. Add a valid cookies.txt and try again."
        )

    if "proxy" in lowered and ("connect" in lowered or "tunnel" in lowered):
        return "Could not connect through the proxy. Check PROXY_URL."

    return text


@app.get("/", response_class=HTMLResponse)
async def serve_homepage():
    index_path = BASE_DIR / "index.html"

    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")

    return HTMLResponse(
        "<h1>index.html not found. Place it beside main.py.</h1>",
        status_code=404,
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": get_ffmpeg_path() is not None,
        "deno_found": get_deno_path() is not None,
        "cookies_file_found": find_cookies_file() is not None,
        "proxy_configured": PROXY_URL is not None,
    }


@app.post("/api/download")
async def download_media(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    raw_request: Request,
):
    temp_dir = tempfile.mkdtemp(prefix="hsdownloader_")
    stop_event = asyncio.Event()

    if find_cookies_file():
        cookie_attempts = [False]
    elif COOKIE_BROWSER:
        cookie_attempts = [True, False]
    else:
        cookie_attempts = [False]

    async def worker():
        last_error = None

        for use_browser_cookies in cookie_attempts:
            if stop_event.is_set():
                raise RuntimeError("Download cancelled.")

            try:
                opts = build_ydl_opts(req, temp_dir, use_browser_cookies, stop_event)

                filename, title = await asyncio.to_thread(
                    run_download, req.url, opts, req.format, temp_dir
                )

                if filename and os.path.isfile(filename):
                    return filename, title

                raise RuntimeError("No output media file was generated.")

            except Exception as exc:
                last_error = exc
                message = str(exc).lower()

                if stop_event.is_set():
                    raise RuntimeError("Download cancelled.") from exc

                logger.warning(
                    "Attempt failed: browser_cookies=%s error=%s",
                    use_browser_cookies,
                    exc,
                )

                cookie_load_error = "cookie" in message and any(
                    term in message
                    for term in (
                        "database", "decrypt", "permission",
                        "could not", "failed to read", "find",
                    )
                )

                # If loading browser cookies failed, retry WITHOUT them.
                if use_browser_cookies and cookie_load_error:
                    continue

                raise

        raise RuntimeError(f"Download failed: {last_error}")

    task = asyncio.create_task(worker())

    try:
        while not task.done():
            if await raw_request.is_disconnected():
                stop_event.set()
                task.cancel()
                raise HTTPException(
                    status_code=499,
                    detail="Client disconnected; download cancelled.",
                )

            await asyncio.sleep(0.3)

        filename, title = await task

    except HTTPException:
        stop_event.set()
        task.cancel()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    except asyncio.CancelledError:
        stop_event.set()
        task.cancel()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=499, detail="Download cancelled.")

    except Exception as exc:
        stop_event.set()
        task.cancel()
        shutil.rmtree(temp_dir, ignore_errors=True)

        logger.exception("Download failed")
        raise HTTPException(
            status_code=500,
            detail=f"Download error: {friendly_error(exc)}",
        )

    if not filename or not os.path.isfile(filename):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to generate the download file.",
        )

    safe_title = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]', "_", title
    ).strip(" .")[:180] or "media"

    extension = os.path.splitext(filename)[1].lower()

    media_types = {
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }

    background_tasks.add_task(cleanup_file, temp_dir)

    return FileResponse(
        path=filename,
        filename=f"{safe_title}{extension}",
        media_type=media_types.get(extension, "application/octet-stream"),
        background=background_tasks,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
