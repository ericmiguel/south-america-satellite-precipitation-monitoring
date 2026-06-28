"""satmon - GOES-19 satellite image collection and processing."""

from __future__ import annotations

import base64
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
import io
import itertools
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any
from typing import Literal
from typing import TypedDict

import httpx
from loguru import logger
from PIL import Image


# ── Paths ─────────────────────────────────────────────────────────────────────────────

# Persisted channel state (latest downloaded hour per channel).
STATE_FILENAME = ".state.json"
# Root directory for all downloaded satellite data.
SATMON_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
# Full path to the state file.
SATMON_STATE_FILE = SATMON_DATA_DIR / STATE_FILENAME

# ── INMET API ─────────────────────────────────────────────────────────────────────────

INMET_API = "https://apisat.inmet.gov.br"  # INMET satellite API base URL.
# INMET homepage used to refresh session cookies.
INMET_HOME = "https://satelite.inmet.gov.br"
# Per-request timeout for all HTTP calls to INMET.
HTTP_TIMEOUT_SECONDS = 30
# Maximum retry attempts before raising SatmonAPIError.
HTTP_MAX_RETRIES = 3
# Pause between image downloads to avoid rate-limiting.
DOWNLOAD_DELAY_SECONDS = 0.5
# INMET day starts at 03:00 UTC; shift "today" accordingly.
SATELLITE_DAY_OFFSET_HOURS = 3
# Expected successful HTTP status code from INMET responses.
HTTP_STATUS_OK = 200

# ── Download window ───────────────────────────────────────────────────────────────────

# Cap on how many historical hours to download per run (10 days).
SATMON_MAX_INITIAL_HOURS = 240

# ── Image processing ─────────────────────────────────────────────────

# Top pixel rows of each INMET image containing the text header (kept grayscale).
SATMON_HEADER_ROWS = 22
# JPEG re-encoding quality (1-100); 95 preserves detail with minimal artifacts.
JPEG_QUALITY = 95

# ── GIF animation ─────────────────────────────────────────────────────────────────────

# Milliseconds per frame in the animated GIF (~16 fps).
GIF_FRAME_DURATION_MS = 60
# Number of loops: 0 = infinite.
GIF_LOOP_COUNT = 0
# Minimum frames required to generate a GIF (skip otherwise).
GIF_MIN_FRAMES = 3
# Maximum frames to include in the GIF (newest N images).
GIF_MAX_FRAMES = 60
# Subdirectory name for generated GIFs inside the data folder.
GIF_OUTPUT_DIR = ""  # void = SATMON_DATA_DIR
# Consecutive hours differ by 100 in HHMM; used to detect gaps.
HOUR_GAP_STEP = 100
# Max number of "extra" (orphan) files shown in the coverage report.
EXTRA_DISPLAY_LIMIT = 5


class SatmonAPIError(Exception):
    """API request failed after all retries."""


class SatmonImageError(Exception):
    """Image download or decode failed."""


# ── Types ─────────────────────────────────────────────────────────────────────────────

ChannelPalette = Literal["ir", "wv"]
InmetParam = Literal["VA", "IV"]
ChannelKey = Literal["ch08", "ch13"]


class ChannelInfo(TypedDict):
    """Configuration for a single satellite channel.

    Fields
    ------
    name : str
        Human-readable label, e.g. "WV 6.19um".
    inmet_param : InmetParam
        INMET API parameter for this channel: "VA" or "IV".
    palette : ChannelPalette
        Color palette key: "ir" or "wv".
    """

    name: str
    inmet_param: InmetParam
    palette: ChannelPalette


class ChannelCoverage(TypedDict):
    """Coverage statistics for a single channel.

    Fields
    ------
    name : str
        Human-readable channel label.
    api_set : set[str]
        Set of HHMM slots returned by the INMET API.
    downloaded_today : set[str]
        Set of HHMM slots already on disk for the current satellite day.
    total_downloaded : int
        Total number of image files on disk across all days.
    """

    name: str
    api_set: set[str]
    downloaded_today: set[str]
    total_downloaded: int


# ── Channels ──────────────────────────────────────────────────────────────────────────

SATMON_CHANNELS: dict[ChannelKey, ChannelInfo] = {
    "ch08": {
        "name": "WV 6.19um",
        "inmet_param": "VA",
        "palette": "wv",
    },
    "ch13": {
        "name": "IR 10.35um",
        "inmet_param": "IV",
        "palette": "ir",
    },
}


# Paths


# ── State ─────────────────────────────────────────────────────────────────────────────


def load_satmon_state() -> dict[str, str]:
    """Load persisted state from the state file.

    Returns
    -------
    dict[str, str]
        Channel-to-latest-hour mapping, e.g. ``{"inmet_ch08_latest": "11:40"}`.
        Returns an empty dict if the state file does not exist.
    """
    if SATMON_STATE_FILE.exists():
        return json.loads(SATMON_STATE_FILE.read_text())
    return {}


def save_satmon_state(state: dict) -> None:
    """Persist the given state dict to the state file as JSON.

    Parameters
    ----------
    state : dict
        Dictionary of state keys and values to persist.
    """
    SATMON_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SATMON_STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Palettes ──────────────────────────────────────────────────────────────────────────
# LUTs applied only to the image body; the header stays in grayscale.
# 0 = hot/dark, 255 = cold/white


def _build_lut(points: list[tuple[int, tuple[int, int, int]]]) -> list[int]:
    """Build a 256-entry flat RGB lookup table by interpolating between control points.

    Each control point is a ``(index, (R, G, B))`` tuple. Values outside any
    defined interval fall back to black (0, 0, 0).

    Parameters
    ----------
    points : list of (int, (int, int, int))
        Control points sorted by index. Each point is ``(i, (r, g, b))`` where
        ``i`` is the 0-255 index and ``(r, g, b)`` are the RGB values at that index.

    Returns
    -------
    list[int]
        Flat 768-element list ``[R0, G0, B0, R1, G1, B1, ...]`` ready for
        :meth:`PIL.Image.Image.putpalette`.
    """
    flat: list[int] = []
    for i in range(256):
        for a, b in itertools.pairwise(points):
            if a[0] <= i <= b[0]:
                t = (i - a[0]) / max(b[0] - a[0], 1)
                r = round(a[1][0] + (b[1][0] - a[1][0]) * t)
                g = round(a[1][1] + (b[1][1] - a[1][1]) * t)
                b_ = round(a[1][2] + (b[1][2] - a[1][2]) * t)
                flat.extend([r, g, b_])
                break
        else:
            flat.extend([0, 0, 0])
    return flat


_PAL_IR = _build_lut([
    (0, (35, 28, 20)),  # brown (warm land)
    (40, (10, 10, 70)),  # dark blue
    (80, (0, 80, 200)),  # blue
    (110, (0, 200, 255)),  # cyan
    (140, (50, 220, 80)),  # green
    (170, (200, 230, 0)),  # yellow
    (200, (255, 120, 0)),  # orange
    (225, (255, 0, 0)),  # red
    (240, (220, 50, 150)),  # magenta
    (255, (255, 255, 255)),  # white (intense convection)
])

_PAL_WV = _build_lut([
    (0, (50, 35, 20)),  # brown (dry air)
    (60, (80, 60, 50)),
    (120, (90, 100, 110)),  # bluish gray (mid humidity)
    (180, (160, 170, 180)),
    (220, (200, 220, 240)),  # bluish white
    (255, (255, 255, 255)),  # white (deep clouds)
])


def apply_satmon_palette(img: Image.Image, palette: ChannelPalette) -> Image.Image:
    """Apply a color lookup table to a grayscale satellite image.

    If the image is not in grayscale mode (``"L"``), it is returned as-is
    converted to RGB.

    Parameters
    ----------
    img : Image.Image
        PIL Image, expected in mode ``"L"`` (grayscale) for palette application.
    palette : ChannelPalette
        Palette key: ``"ir"`` or ``"wv"``.

    Returns
    -------
    Image.Image
        RGB image with the selected color palette applied.
    """
    if img.mode != "L":
        return img.convert("RGB")
    pal = {"ir": _PAL_IR, "wv": _PAL_WV}
    lut = pal.get(palette, _PAL_IR)
    p = img.convert("P")
    p.putpalette(lut)
    return p.convert("RGB")


# ── INMET API ─────────────────────────────────────────────────────────────────────────


class InmetClient:
    """HTTP client for the INMET satellite image API.

    Handles cookie-based authentication, retry logic, and provides
    methods to list available hours and download individual images
    for each satellite channel.

    Parameters
    ----------
    (none required) — all configuration comes from module-level constants.
    """

    def __init__(self) -> None:
        """Set up the HTTP client, default headers, and initial cookie refresh."""
        self._http = httpx.Client(verify=True, timeout=HTTP_TIMEOUT_SECONDS)
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        self._refresh_cookies()

    def _refresh_cookies(self) -> None:
        """Refresh session cookies by hitting the INMET homepage."""
        self._http.get(INMET_HOME, headers=self._headers)

    def _get(self, path: str) -> Any:
        """GET an INMET API endpoint with retry and cookie refresh on failure.

        Retries up to ``HTTP_MAX_RETRIES`` times. On each failure (timeout,
        HTTP error, invalid JSON, or non-200 status) the session cookies are
        refreshed before the next attempt.

        Parameters
        ----------
        path : str
            API path fragment appended to :data:`INMET_API`.

        Returns
        -------
        dict[str, Any]
            Parsed JSON response body.

        Raises
        ------
        SatmonAPIError
            If all retries are exhausted.
        """
        delay = 1
        for _ in range(HTTP_MAX_RETRIES):
            try:
                r = self._http.get(f"{INMET_API}{path}", headers=self._headers)
            except httpx.TimeoutException:
                self._refresh_cookies()
                time.sleep(delay)
                delay *= 2
                continue
            except httpx.HTTPError:
                self._refresh_cookies()
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == HTTP_STATUS_OK:
                try:
                    return r.json()
                except json.JSONDecodeError:
                    self._refresh_cookies()
                    time.sleep(delay)
                    delay *= 2
                    continue
            self._refresh_cookies()
            time.sleep(delay)
            delay *= 2
            continue
        raise SatmonAPIError(
            f"request to {path} failed after {HTTP_MAX_RETRIES} retries"
        )

    def available_hours(self, param: InmetParam, day: date) -> list[str]:
        """Query INMET for all available satellite image hours on a given day.

        Parameters
        ----------
        param : InmetParam
            INMET channel parameter: ``"VA"`` (WV) or ``"IV"`` (IR).
        day : date
            Satellite day to query (INMET day starts at 03:00 UTC).

        Returns
        -------
        list[str]
            List of hour strings in ``"HH:MM"`` format, newest-first after
            the caller sorts them. Empty list if the API returns no data.
        """
        path = f"/horas/GOES/AS/{param}/{day.isoformat()}T03:00:00.000Z"
        data = self._get(path)
        if not data:
            return []
        return [h["sigla"] for h in data]

    def download_image(self, param: InmetParam, day: date, hour: str) -> bytes:
        """Download a single satellite image for the given hour and channel.

        The raw response contains a base64-encoded JPEG. This method decodes
        it and returns the raw bytes. Raises on missing data or decode failure.

        Parameters
        ----------
        param : InmetParam
            INMET channel parameter: ``"VA"`` (WV) or ``"IV"`` (IR).
        day : date
            Satellite day for the image.
        hour : str
            Hour string in ``"HH:MM"`` format.

        Returns
        -------
        bytes
            Raw JPEG bytes of the downloaded image.

        Raises
        ------
        SatmonImageError
            If the API response lacks base64 data or the base64 string
            cannot be decoded.
        """
        path = f"/GOES/AS/{param}/{day.isoformat()}T03:00:00.000Z/{hour}"
        data = self._get(path)
        if not data or "base64" not in data:
            raise SatmonImageError(f"no base64 in response for {path}")
        b64 = data["base64"]
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        pad = len(b64) % 4
        if pad:
            b64 += "=" * (4 - pad)
        try:
            return base64.b64decode(b64)
        except Exception as exc:
            raise SatmonImageError(f"base64 decode failed for {path}") from exc


# ── Download ──────────────────────────────────────────────────────────────────────────
# Preserve the original header (SATMON_HEADER_ROWS px) for visual timestamp
# identification. The color palette is applied only to the body.


def process_inmet(img_bytes: bytes, palette: ChannelPalette) -> bytes:
    """Preserve original header, apply colour palette to body, return RGB JPEG.

    The top ``SATMON_HEADER_ROWS`` pixels (text header from INMET) are kept
    in grayscale. The remaining body is coloured using the selected LUT.

    Parameters
    ----------
    img_bytes : bytes
        Raw JPEG bytes as received from the INMET API.
    palette : ChannelPalette
        Palette key: ``"ir"`` or ``"wv"``.

    Returns
    -------
    bytes
        Re-encoded RGB JPEG bytes with the palette applied to the image body.
    """
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size

    header = img.crop((0, 0, w, SATMON_HEADER_ROWS))
    body = img.crop((0, SATMON_HEADER_ROWS, w, h))

    body_color = apply_satmon_palette(body, palette)
    header_gray = header.convert("RGB") if header.mode != "RGB" else header

    final = Image.new("RGB", (w, h))
    final.paste(header_gray, (0, 0))
    final.paste(body_color, (0, SATMON_HEADER_ROWS))

    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _list_downloaded(channel_dir: Path) -> set[str]:
    """Glob ``inmet_*.jpg`` files and return their date+hour keys.

    Parameters
    ----------
    channel_dir : Path
        Directory to scan for ``inmet_*.jpg`` files.

    Returns
    -------
    set[str]
        Set of ``{YYYYMMDD}_{HHMM}`` strings extracted from filenames.
    """
    return {
        f"{p.stem.split('_')[1]}_{p.stem.split('_')[2][:4]}"
        for p in channel_dir.glob("inmet_*.jpg")
    }


def _window_hours(client: InmetClient, param: InmetParam, day: date) -> list[str]:
    """Fetch available hours from the API and apply the sliding window.

    The result is sorted newest-first and truncated to
    ``SATMON_MAX_INITIAL_HOURS`` entries. Returns an empty list on any
    API error so the caller can skip the channel gracefully.

    Parameters
    ----------
    client : InmetClient
        Authenticated INMET API client.
    param : InmetParam
        Channel parameter: ``"VA"`` or ``"IV"``.
    day : date
        Satellite day to query.

    Returns
    -------
    list[str]
        Newest-first hour strings, capped to the configured window size.
    """
    try:
        hours = client.available_hours(param, day)
    except Exception:
        return []
    if not hours:
        return []
    hours.sort(reverse=True)
    if len(hours) > SATMON_MAX_INITIAL_HOURS:
        hours = hours[:SATMON_MAX_INITIAL_HOURS]
    return hours


def _download_one_hour(
    raw: bytes,
    palette: ChannelPalette,
    fname: Path,
) -> bool:
    """Process a raw satellite image and save it to disk.

    Parameters
    ----------
    raw : bytes
        Raw JPEG bytes from the INMET API.
    palette : ChannelPalette
        Palette key: ``"ir"`` or ``"wv"``.
    fname : Path
        Full destination path for the output JPEG.

    Returns
    -------
    bool
        ``True``.
    """
    clean = process_inmet(raw, palette)
    fname.write_bytes(clean)
    time.sleep(DOWNLOAD_DELAY_SECONDS)
    return True


def _build_coverage(
    name: str, hours: list[str], downloaded: set[str], today_num: str
) -> ChannelCoverage:
    """Build a coverage dict for one channel from API hours and local files.

    Parameters
    ----------
    name : str
        Human-readable channel name.
    hours : list[str]
        API hour strings in ``"HH:MM"`` format.
    downloaded : set[str]
        Set of ``{YYYYMMDD}_{HHMM}`` from files on disk.
    today_num : str
        Today's date as ``YYYYMMDD``, used to filter today's entries.

    Returns
    -------
    ChannelCoverage
        Coverage statistics for this channel.
    """
    return {
        "name": name,
        "api_set": {h.replace(":", "") for h in hours},
        "downloaded_today": {
            item.split("_")[1]
            for item in downloaded
            if item.startswith(f"{today_num}_")
        },
        "total_downloaded": len(downloaded),
    }


def download_inmet(
    client: InmetClient,
) -> tuple[int, dict[ChannelKey, ChannelCoverage], dict[str, str]]:
    """Download new satellite images for all configured channels.

    For each channel, lists hours from the INMET API, skips those already
    on disk, downloads missing ones, and builds coverage statistics.

    Parameters
    ----------
    client : InmetClient
        Authenticated INMET API client.

    Returns
    -------
    tuple[int, dict[ChannelKey, ChannelCoverage], dict[str, str]]
        ``(new_count, coverage, latest)`` where ``new_count`` is the number
        of new downloads, ``coverage`` maps each channel to its coverage
        statistics, and ``latest`` maps ``"inmet_{key}_latest"`` to the
        newest hour string for each channel.
    """
    today = (datetime.now(UTC) - timedelta(hours=SATELLITE_DAY_OFFSET_HOURS)).date()
    today_num = today.strftime("%Y%m%d")
    new_count = 0
    coverage: dict[ChannelKey, ChannelCoverage] = {}
    latest: dict[str, str] = {}
    for key, info in SATMON_CHANNELS.items():
        param = info["inmet_param"]
        palette = info["palette"]
        channel_dir = SATMON_DATA_DIR / key
        channel_dir.mkdir(parents=True, exist_ok=True)

        downloaded = _list_downloaded(channel_dir)
        hours = _window_hours(client, param, today)
        if not hours:
            continue

        for h in hours:
            hhmm = h.replace(":", "")
            if f"{today_num}_{hhmm}" in downloaded:
                continue
            try:
                raw = client.download_image(param, today, h)
            except httpx.HTTPError:
                continue
            stem = f"{today_num}_{hhmm}00"
            fname = channel_dir / f"inmet_{stem}_{key}.jpg"
            if _download_one_hour(raw, palette, fname):
                new_count += 1

        latest[f"inmet_{key}_latest"] = hours[0]
        coverage[key] = _build_coverage(info["name"], hours, downloaded, today_num)
    return new_count, coverage, latest


# ── Coverage diagnosis ────────────────────────────────────────────────────────────────


def diagnose_satmon_coverage(
    coverage: dict[ChannelKey, ChannelCoverage],
) -> list[str]:
    """Build a coverage report comparing API hours against downloaded files.

    For each channel, produces lines with counts of API slots, local files,
    present matches, missing gaps, and extra files. Missing hours are grouped
    into consecutive ranges (e.g. ``"1010-1040"``).

    Parameters
    ----------
    coverage : dict[ChannelKey, ChannelCoverage]
        Per-channel coverage data produced by :func:`download_inmet`.
        Each value must contain ``api_set``, ``downloaded_today``,
        ``name``, and ``total_downloaded`` keys.

    Returns
    -------
    list[str]
        Lines of the coverage report, one per metric.
    """
    lines: list[str] = []
    for key, data in coverage.items():
        api_set: set[str] = data["api_set"]
        downloaded_today: set[str] = data["downloaded_today"]
        missing = sorted(api_set - downloaded_today)
        extra = sorted(downloaded_today - api_set)
        common = sorted(api_set & downloaded_today)

        lines.extend([
            f"[{key}] {data['name']}",
            f"API: {len(api_set)} slots "
            f"Downloaded: {data['total_downloaded']} "
            f"Present: {len(common)} "
            f"Missing: {len(missing)} "
            f"Extra: {len(extra)}",
        ])

        if missing:
            groups: list[tuple[str, str]] = []
            start = end = missing[0]
            for h in missing[1:]:
                if int(h) - int(end) == HOUR_GAP_STEP:
                    end = h
                else:
                    groups.append((start, end))
                    start = end = h
            groups.append((start, end))

            parts = []
            for a, b in groups:
                if a == b:
                    parts.append(a)
                else:
                    parts.append(f"{a}-{b}")
            lines.append(f"gaps: {' '.join(parts)}")

        if extra:
            displayed = " ".join(extra[:EXTRA_DISPLAY_LIMIT])
            suffix = "..." if len(extra) > EXTRA_DISPLAY_LIMIT else ""
            lines.append(f"extra: {displayed}{suffix}")

    return lines


# ── Animations ────────────────────────────────────────────────────────────────────────


def generate_satmon_gif(
    channel_dir: Path, key: ChannelKey, max_frames: int = GIF_MAX_FRAMES
) -> None:
    """Build an animated GIF from the most recent satellite frames.

    Takes the newest ``max_frames`` JPEGs on disk, sorts them chronologically,
    and writes a looping GIF to ``{GIF_OUTPUT_DIR}/{key}.gif``. Does nothing if fewer
    than 3 frames are available.

    Parameters
    ----------
    channel_dir : Path
        Directory containing ``inmet_*.jpg`` files for this channel.
    key : ChannelKey
        Channel identifier, used as the GIF filename stem (e.g. ``"ch08"``).
    max_frames : int
        Maximum number of frames to include. Defaults to 60.
    """
    imgs = sorted(channel_dir.glob("inmet_*.jpg"))[-max_frames:]
    if len(imgs) < GIF_MIN_FRAMES:
        return
    frames = [Image.open(p).convert("P", palette=Image.Palette.ADAPTIVE) for p in imgs]
    gif_dir = SATMON_DATA_DIR / GIF_OUTPUT_DIR
    gif_dir.mkdir(parents=True, exist_ok=True)
    path = gif_dir / f"{key}.gif"
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=GIF_FRAME_DURATION_MS,
        loop=GIF_LOOP_COUNT,
    )


def generate_all_satmon_gifs() -> None:
    """Generate animated GIFs for every configured channel.

    Iterates over :data:`SATMON_CHANNELS` and delegates to
    :func:`generate_satmon_gif` for each one.
    """
    for key in SATMON_CHANNELS:
        generate_satmon_gif(SATMON_DATA_DIR / key, key)


# ── Main ──────────────────────────────────────────────────────────────────────────────


def run_satmon() -> None:
    """Orchestrate the full pipeline: download, coverage report, GIF generation.

    Steps
    -----
    1. Load persisted state from ``.state.json``.
    2. Download new satellite images for all configured channels.
    3. Persist the updated state (latest hour per channel).
    4. Build a coverage report (present, missing, and extra hours).
    5. Generate animated GIFs from the most recent frames.
    """
    state = load_satmon_state()

    logger.info("Starting satellite image download")
    inmet = InmetClient()
    n, coverage, latest = download_inmet(inmet)
    state.update(latest)

    if n:
        logger.info("Downloaded {} new images", n)
    else:
        logger.info("All images up to date")

    save_satmon_state(state)

    for line in diagnose_satmon_coverage(coverage):
        logger.info(line)

    logger.info("Generating animations")
    generate_all_satmon_gifs()
    logger.info("Pipeline complete")


def main() -> None:
    """Entry point with SIGINT handler. Calls :func:`run_satmon`."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    run_satmon()


if __name__ == "__main__":
    main()
