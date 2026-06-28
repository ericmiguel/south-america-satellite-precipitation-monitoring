# South America Satellite Precipitation Monitoring

Automated collection and processing of GOES-19 satellite imagery from [INMET/CPTEC](https://satelite.cptec.inpe.br/home/index.jsp) for precipitation monitoring.

This project is an automation utility. It is designed to be as simple and easy to use as possible. You can clone, install dependencies, and run everything in two minutes.

## Satellite & Channels

Images are sourced from the **GOES-19** geostationary satellite, operated by NOAA, and redistributed by CPTEC/INPE through the public INMET API.

Two spectral channels are collected, each targeting different atmospheric features relevant to convective storm analysis:

| Channel | Band | Wavelength | What it reveals                     |
| ------- | ---- | ---------- | ----------------------------------- |
| `ch08`  | WV   | 6.19 µm    | Mid/upper-tropospheric water vapor. |
| `ch13`  | IR   | 10.35 µm   | Clean longwave infrared window.     |

Both channels are served as grayscale JPEGs at 10-minute intervals during the satellite operational day.

### Interpreting colors

On `ch08` post-processed imagery, brightness represents moisture: bright areas indicate moist columns; dark areas indicate dry air intrusions often associated with storm environments.

On `ch13` post-processed imagery, brightness represents cloud-top temperature: colder (brighter) tops indicate deeper convection and stronger updrafts.

## How It Works

Each run performs a single-pass sync against the current satellite day (INMET day starts at 03:00 UTC):

1. Queries available hours via the INMET API for each channel.
2. Skips hours already present on disk.
3. Downloads missing images and applies a per-channel color palette.
4. Persists download progress so consecutive runs are incremental.
5. Generates a looping animated GIF from the most recent frames for each channel.

## Installation & usage

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/your-org/satmon.git
cd satmon
uv sync
uv run satmon
```

Run periodically (e.g. via cron every 10 minutes) to maintain a continuous archive and fresh animations. Output is written to `data/` by default:

```
data/
├── ch08/
│   └── inmet_YYYYMMDD_HHMMSS_ch08.jpg
├── ch13/
│   └── inmet_YYYYMMDD_HHMMSS_ch13.jpg
├── ch08.gif
├── ch13.gif
└── .state.json
```

## Configuration

The tool is designed to run out of the box with sensible defaults, but you can control almost every aspect if you want to. All behavior is controlled by constants at the top of `src/satmon/__main__.py`. There is no external config file. 

| Constant                     | Default  | Purpose                                                             |
| ---------------------------- | -------- | ------------------------------------------------------------------- |
| `SATMON_DATA_DIR`            | `./data` | Root directory for all downloaded imagery.                          |
| `SATMON_MAX_INITIAL_HOURS`   | 240      | Max historical hours to fetch per run (~10 days at 10-min cadence). |
| `SATELLITE_DAY_OFFSET_HOURS` | 3        | UTC hour at which the satellite "day" begins.                       |
| `HTTP_TIMEOUT_SECONDS`       | 30       | Per-request timeout for INMET API calls.                            |
| `HTTP_MAX_RETRIES`           | 3        | Retry attempts before failing a request.                            |
| `DOWNLOAD_DELAY_SECONDS`     | 0.5      | Pause between image downloads to avoid rate-limiting.               |
| `JPEG_QUALITY`               | 95       | Output JPEG quality (1–100).                                        |
| `SATMON_HEADER_ROWS`         | 22       | Top pixel rows containing the INMET text header left in grayscale.  |
| `GIF_FRAME_DURATION_MS`      | 60       | Milliseconds per animation frame (~16.7 fps).                       |
| `GIF_MAX_FRAMES`             | 60       | Newest N frames included in the animated GIF.                       |
| `GIF_MIN_FRAMES`             | 3        | Minimum frames required to generate a GIF.                          |
| `GIF_OUTPUT_DIR`             | `NULL`   | Subdirectory inside `data/` for GIF output.                         |

Channel definitions (palettes, INMET parameter codes) are in the `SATMON_CHANNELS` dict in the same file.

The entire project represents about 450 lines (without docstrings) of code condensed into a single module, so it's easy to alter anything as needed.