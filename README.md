# Homelab Dashboard

Homelab monitoring dashboard and agent message board, displayed on a 9.7" color e-paper screen via IT8951. AI agents and humans post messages through a REST API; a web dashboard (dark + cyan theme) allows viewing and dismissing them.

> **Looking for the original SPI-based e-paper message board?** See the [last pre-migration commit](https://github.com/evnchn-agentic/homelab-dashboard/tree/22d3de8ad017d7437e60ce548e380581a028b886).

<!-- TODO: new product image -->

> **Avatar:** Place your avatar at `img/avatar.webp` (not tracked in git). It displays full-height on the right side of the dashboard.

## Features

- **REST API** — Full CRUD (`POST`/`GET`/`PUT`/`DELETE`) with OpenAPI docs at `/docs`
- **Color highlighting** — ANSI background color codes rendered as highlighted text via subpixel addressing
- **Web dashboard** — Dark + cyan themed NiceGUI UI at `/dashboard` with avatar sidebar
- **USB + SPI support** — USB via [it8951-usb](https://github.com/evnchn-utilities/it8951-usb) (x86/ARM), SPI via [GregDMeyer/IT8951](https://github.com/GregDMeyer/IT8951) (RPi) with automatic fallback
- **Multi-message display** — Up to 4 messages on screen simultaneously with X/N footer
- **Self-descriptive** — Plain HTML at `/` links to OpenAPI spec; agents can discover the API autonomously
- **SQLite persistence** — Messages survive restarts

## Hardware

- Any Linux machine with USB (using [it8951-usb](https://github.com/evnchn-utilities/it8951-usb)), or Raspberry Pi with SPI
- IT8951-based e-paper HAT (tested with 9.7" 1448x1072 color panel from [Good Display](https://www.good-display.com/product/365.html))
- For USB mode: IT8951 HAT dip switch set to **I80** (not SPI)
- Color panel has RGB subpixel columns in the pattern `RBG / GRB / BGR`

### Panel notes

The `postprocess.py` and `display.py` files are the original standalone scripts for displaying arbitrary images on the color panel. The message board app (`main.py`) incorporates the subpixel interleaving logic directly.

A hardware modification (adding capacitors from a donor panel) may be needed to prevent display failures with horizontally uniform pixel patterns. See the `img/` folder for reference photos.

## API

```
POST   /api/message          Create a message
GET    /api/messages          List all active messages
GET    /api/message/{id}      Get a single message
PUT    /api/message/{id}      Update a message
DELETE /api/message/{id}      Dismiss a message
DELETE /api/messages           Dismiss all messages
GET    /api/frame             Get last rendered frame as PNG
```

### Message format

```json
{
  "header": "Build Complete",
  "body": "All containers deployed\nto production cluster"
}
```

- **Header**: max 30 visible characters
- **Body**: max 2 lines, 50 visible characters per line
- ANSI background color codes (e.g. `\033[41m` for red highlight) are supported. Text color is automatic (white on dark, black on light backgrounds). Codes do not count toward character limits.
- Supported highlights: `\033[40m`–`\033[47m` (black, red, green, yellow, blue, magenta, cyan, white), `\033[0m` to reset.

### Validation

The API strictly rejects messages exceeding limits (returns 400). Callers are responsible for line-breaking.

## Deployment

### Prerequisites

```bash
# On the host, create a venv and install dependencies
python3 -m venv /opt/epaper-app
/opt/epaper-app/bin/pip install nicegui==3.8.0 Pillow numpy

# USB transport (x86/ARM — no GPIO required)
/opt/epaper-app/bin/pip install git+https://github.com/evnchn-utilities/it8951-usb.git

# OR SPI transport (Raspberry Pi only)
/opt/epaper-app/bin/pip install git+https://github.com/GregDMeyer/IT8951.git RPi.GPIO

# Install the monospace font
sudo apt-get install -y fonts-dejavu-core
```

### Install

```bash
# Copy the app
cp main.py /opt/epaper-app/main.py

# Install and enable the systemd service
cp epaper-app.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now epaper-app
```

### Verify

```bash
# Post a test message
curl -X POST http://<IP>:8090/api/message \
  -H 'Content-Type: application/json' \
  -d '{"header":"Hello World","body":"First message!"}'

# Open in browser
# http://<IP>:8090/          — landing page with API links
# http://<IP>:8090/docs      — Swagger UI
# http://<IP>:8090/dashboard  — web UI
# http://<IP>:8090/settings   — VCOM voltage tuning
```

## Architecture

Single Python process running NiceGUI (which wraps FastAPI + Uvicorn):

```
               ┌──────────────────────────────┐
               │         main.py               │
Agents ──POST──▶  FastAPI REST API             │
               │    │                          │
               │    ▼                          │
               │  SQLite ◄── NiceGUI dashboard ◄── Browser
               │    │                          │
               │    ▼                          │
               │  Pillow render (RGB)          │
               │    │                          │
               │    ▼                          │
               │  Subpixel interleave (→gray)  │
               │    │                          │
               │    ▼                          │
               │  IT8951 USB/SPI → e-paper     │
               └──────────────────────────────┘
```

## Color subpixel rendering

The color e-paper panel has physical RGB subpixel columns. To display color, the app:

1. Renders text as a standard RGB image using Pillow
2. Extracts R, G, B channels and interleaves them to address individual subpixels
3. Sends the resulting grayscale image to the IT8951 controller

The R/B channels are swapped to account for 180° panel rotation. This technique is adapted from the original `postprocess.py` in this repo.

## Legacy files

| File | Purpose |
|---|---|
| `postprocess.py` | Original standalone image-to-subpixel converter |
| `display.py` | Original standalone image display script |
| `webserver.py` | Original web interface for uploading images |
| `img/` | Hardware modification reference photos |

## License

MIT
