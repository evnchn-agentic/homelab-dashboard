#!/usr/bin/env python3
"""Homelab Dashboard — NiceGUI + IT8951

Agent message board and homelab monitoring dashboard.
Web UI for viewing and dismissing messages, with e-paper display output.
"""

import re
import sqlite3
import threading
import logging
from datetime import datetime
from pathlib import Path

import io
import numpy as np
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from nicegui import app, ui
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Pydantic models for API docs
# ---------------------------------------------------------------------------

class MessageIn(BaseModel):
    sender: str = Field(..., description="Name of the sender (agent name, service, or person).")
    header: str = Field(..., description="Short message title.")
    body: str = Field("", description="Message body. Markdown is supported.")

class MessageOut(BaseModel):
    id: int
    sender: str
    header: str
    body: str
    created_at: str
    status: str

class StatusOut(BaseModel):
    status: str

class ErrorOut(BaseModel):
    error: str

class CreatedOut(BaseModel):
    id: int
    status: str


# ---------------------------------------------------------------------------
# IT8951 import — USB first, SPI fallback
# ---------------------------------------------------------------------------
try:
    from it8951_usb import USBEPD
    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False

try:
    from IT8951.display import AutoEPDDisplay, AutoDisplay
    from IT8951 import constants
    SPI_AVAILABLE = True
except ImportError:
    SPI_AVAILABLE = False
    # Provide fallback display mode constants when IT8951 SPI package is absent
    class _DisplayModes:
        INIT = 0; DU = 1; GC16 = 2; GL16 = 3; A2 = 6
    class constants:
        DisplayModes = _DisplayModes()

EPAPER_AVAILABLE = USB_AVAILABLE or SPI_AVAILABLE
if not EPAPER_AVAILABLE:
    logging.warning("IT8951 not available — running in headless mode")


class USBAutoDisplay:
    """Adapter that gives USBEPD the same interface as AutoEPDDisplay."""

    def __init__(self, usb_epd, rotate=None, mirror=False):
        self.epd = usb_epd
        self._rotate = rotate
        self.width = usb_epd.width
        self.height = usb_epd.height
        if rotate in ('CW', 'CCW'):
            self.width, self.height = self.height, self.width
        self.frame_buf = Image.new('L', (self.width, self.height), 0xFF)

    def clear(self):
        """Clear the display with an INIT refresh to eliminate ghosting."""
        self.frame_buf.paste(0xFF, box=(0, 0, self.width, self.height))
        white = b'\xFF' * (self.epd.width * self.epd.height)
        self.epd.load_img_area(white, xy=(0, 0), dims=(self.epd.width, self.epd.height))
        self.epd.display_area((0, 0), (self.epd.width, self.epd.height), display_mode=0)  # INIT

    def draw_full(self, mode):
        # Accept both int and constants.DisplayModes enum
        mode_int = int(mode) if not isinstance(mode, int) else mode
        img = self.frame_buf
        if self._rotate == 'flip':
            img = img.transpose(Image.ROTATE_180)
        elif self._rotate == 'CW':
            img = img.transpose(Image.ROTATE_270)
        elif self._rotate == 'CCW':
            img = img.transpose(Image.ROTATE_90)
        data = img.tobytes()
        self.epd.load_img_area(data, xy=(0, 0), dims=(self.epd.width, self.epd.height))
        self.epd.display_area((0, 0), (self.epd.width, self.epd.height), display_mode=mode_int)

    def draw_partial(self, mode):
        self.draw_full(mode)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "messages.db"
SETTINGS_PATH = Path(__file__).parent / "settings.json"
DISPLAY_WIDTH = 1448
DISPLAY_HEIGHT = 1072
SPI_HZ = 24000000
MARGIN = 30
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
HEADER_FONT_SIZE = 77
BODY_FONT_SIZE = 45

log = logging.getLogger("epaper")
logging.basicConfig(level=logging.INFO)

app.add_static_files('/img', str(Path(__file__).parent / 'img'))

_db_lock = threading.Lock()
_display_lock = threading.Lock()
_epd = None  # lazily initialised AutoEPDDisplay
_last_frame: Image.Image | None = None  # last rendered RGB frame
_displayed_ids: list[int] = []  # IDs of messages currently shown on screen
_page_stack: list[int] = [0]    # stack of message-list start offsets (for prev/next page)

# ---------------------------------------------------------------------------
# Persistent settings (VCOM, display mode, enhanced driving)
# ---------------------------------------------------------------------------
import json as _json

_DEFAULT_SETTINGS = {
    "vcom": -2.00,
}




def _load_settings() -> dict:
    try:
        return {**_DEFAULT_SETTINGS, **_json.loads(SETTINGS_PATH.read_text())}
    except (FileNotFoundError, _json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


def _save_settings(settings: dict):
    SETTINGS_PATH.write_text(_json.dumps(settings, indent=2))

# ---------------------------------------------------------------------------
# ANSI escape sequence handling
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# Map ANSI background color codes to RGB tuples for highlight rendering
_ANSI_BG_RGB = {
    40: (0, 0, 0),         # black bg
    41: (255, 0, 0),       # red bg
    42: (0, 255, 0),       # green bg
    43: (255, 255, 0),     # yellow bg
    44: (0, 0, 255),       # blue bg
    45: (255, 0, 255),     # magenta bg
    46: (0, 255, 255),     # cyan bg
    47: (255, 255, 255),   # white bg
}

# Dark backgrounds get white text, light backgrounds get black text
_DARK_BGS = {40, 41, 42, 44, 45}  # black, red, green, blue, magenta


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _fg_for_bg(bg_code: int | None) -> tuple:
    """Return white text for dark backgrounds, black for light/no background."""
    return (255, 255, 255) if bg_code in _DARK_BGS else (0, 0, 0)


def parse_ansi_segments(text: str) -> list[tuple[str, tuple, tuple | None]]:
    """Parse text with ANSI codes into [(text, fg_rgb, bg_rgb|None), ...] segments."""
    segments = []
    bg_code = None  # current background ANSI code
    bg_rgb = None   # current background RGB
    pos = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], _fg_for_bg(bg_code), bg_rgb))
        codes_str = m.group()[2:-1]
        if codes_str:
            for code in codes_str.split(";"):
                c = int(code) if code else 0
                if c == 0:
                    bg_code = None
                    bg_rgb = None
                elif c in _ANSI_BG_RGB:
                    bg_code = c
                    bg_rgb = _ANSI_BG_RGB[c]
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], _fg_for_bg(bg_code), bg_rgb))
    return segments


# Map ANSI background codes to CSS for the web UI (dark theme aware)
_ANSI_BG_CSS = {
    40: ("black", "white"), 41: ("red", "white"), 42: ("lime", "black"),
    43: ("yellow", "black"), 44: ("blue", "white"), 45: ("magenta", "white"),
    46: ("cyan", "black"), 47: ("lightgray", "black"),
}


def ansi_to_html(text: str) -> str:
    """Convert ANSI-colored text to HTML with background highlight styles."""
    import html as _html
    parts = []
    current_style = None  # (bg_css, fg_css) or None
    pos = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            chunk = _html.escape(text[pos:m.start()])
            if current_style:
                bg, fg = current_style
                parts.append(f'<span style="background:{bg};color:{fg};padding:0 2px">{chunk}</span>')
            else:
                parts.append(chunk)
        codes_str = m.group()[2:-1]
        if codes_str:
            for code in codes_str.split(";"):
                c = int(code) if code else 0
                if c == 0:
                    current_style = None
                elif c in _ANSI_BG_CSS:
                    current_style = _ANSI_BG_CSS[c]
        pos = m.end()
    if pos < len(text):
        chunk = _html.escape(text[pos:])
        if current_style:
            bg, fg = current_style
            parts.append(f'<span style="background:{bg};color:{fg};padding:0 2px">{chunk}</span>')
        else:
            parts.append(chunk)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_lock:
        conn = _get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sender      TEXT NOT NULL DEFAULT '',
                header      TEXT NOT NULL,
                body        TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'queued'
            )
        """)
        # Migrate: add sender column if missing
        cols = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "sender" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()


def add_message(sender: str, header: str, body: str) -> int:
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(
            "INSERT INTO messages (sender, header, body, created_at, status) VALUES (?, ?, ?, ?, 'queued')",
            (sender, header, body, datetime.now().isoformat()),
        )
        msg_id = cur.lastrowid
        conn.commit()
        conn.close()
    return msg_id


def get_current_message() -> dict | None:
    """Return the oldest queued message (the one currently displayed)."""
    with _db_lock:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM messages WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def get_queued_messages() -> list[dict]:
    """Return all queued messages (oldest first)."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM messages WHERE status='queued' ORDER BY id ASC"
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_message_by_id(msg_id: int) -> dict | None:
    with _db_lock:
        conn = _get_db()
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def dismiss_message(msg_id: int):
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE messages SET status='dismissed' WHERE id=?", (msg_id,))
        conn.commit()
        conn.close()


def dismiss_all():
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE messages SET status='dismissed' WHERE status='queued'")
        conn.commit()
        conn.close()


def dismiss_displayed(ids: list[int]):
    """Dismiss only the messages currently shown on screen."""
    if not ids:
        return
    with _db_lock:
        conn = _get_db()
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            "UPDATE messages SET status='dismissed' WHERE id IN (" + placeholders + ") AND status='queued'",
            ids,
        )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# E-Paper rendering
# ---------------------------------------------------------------------------

def _get_epd():
    global _epd
    if _epd is None and EPAPER_AVAILABLE:
        settings = _load_settings()
        if USB_AVAILABLE:
            try:
                usb_epd = USBEPD(vcom=settings["vcom"])
                _epd = USBAutoDisplay(usb_epd, rotate="flip", mirror=False)
                log.info("Using USB transport (%dx%d)", _epd.width, _epd.height)
                return _epd
            except Exception:
                log.exception("USB EPD init failed, trying SPI")
        if SPI_AVAILABLE:
            _epd = AutoEPDDisplay(vcom=settings["vcom"], rotate="flip", mirror=False, spi_hz=SPI_HZ)
            _apply_enhanced_driving(_epd)
            log.info("Using SPI transport")
    return _epd


def _apply_enhanced_driving(epd):
    """Write enhanced driving capability register (0x0602 to reg 0x0038)."""
    try:
        epd.epd.write_register(0x0038, 0x0602)
        log.info("Enhanced driving capability enabled")
    except Exception:
        log.exception("Failed to set enhanced driving register")


def _reinit_epd():
    """Re-initialize the display with current settings (e.g. after VCOM change)."""
    global _epd
    with _display_lock:
        _epd = None  # force re-init on next use


FOOTER_FONT_SIZE = 32
MSG_GAP = 22  # vertical gap between messages
HEADER_SEP_GAP = 15  # header text to separator line
SEP_BODY_GAP = 16    # separator line to body text
BODY_LINE_GAP = 8    # between body lines


def _draw_ansi_text(draw: ImageDraw.Draw, x: float, y: float, text: str, font: ImageFont.FreeTypeFont, line_height: int = 0):
    """Draw text with ANSI background highlights and contrasting foreground."""
    segments = parse_ansi_segments(text)
    h = line_height or font.size
    cx = x
    for segment_text, fg, bg in segments:
        w = font.getlength(segment_text)
        if bg is not None:
            draw.rectangle([(cx, y), (cx + w, y + h)], fill=bg)
        draw.text((cx, y), segment_text, font=font, fill=fg)
        cx += w


def _rgb_to_subpixel(rgb_img: Image.Image) -> Image.Image:
    """Convert an RGB image to subpixel-addressed grayscale for the color e-paper.

    Adapted from evnchn-utilities/color-e-paper-processor/postprocess.py.
    The panel has R, B, G subpixel columns — this interleaves the channels
    to address each subpixel individually.
    """
    arr = np.array(rgb_img)  # (H, W, 3)
    h, w = arr.shape[:2]

    # Quantize to 4-bit color (multiples of 17)
    arr = np.floor_divide(arr, 17) * 17

    # Extract every 3rd pixel from each channel (subpixel addressing)
    # Swapped R/B positions to account for 180° panel rotation
    blues = arr[:, :, 2].reshape(-1)[0::3]
    greens = arr[:, :, 1].reshape(-1)[2::3]
    reds = arr[:, :, 0].reshape(-1)[1::3]

    # Pad greens if needed
    greens = np.pad(greens, (0, blues.size - greens.size), "constant")

    # Stack in rotated subpixel order: B, R, G
    stacked = np.vstack((blues, reds, greens))
    interleaved = stacked.T.reshape(-1)
    interleaved = np.delete(interleaved, -1)
    result = interleaved.reshape(h, w)

    gray_img = Image.fromarray(np.uint8(result), "L")
    # 2x upscale then back to original size (helps subpixel blending)
    gray_img = gray_img.resize((w * 2, h * 2), Image.NEAREST)
    gray_img = gray_img.resize((w, h), Image.BICUBIC)
    return gray_img


def render_messages(messages: list[dict], start: int = 0):
    """Draw as many messages as fit, with an X/N footer at the bottom."""
    img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        header_font = ImageFont.truetype(FONT_PATH, HEADER_FONT_SIZE)
        body_font = ImageFont.truetype(FONT_PATH, BODY_FONT_SIZE)
        footer_font = ImageFont.truetype(FONT_PATH, FOOTER_FONT_SIZE)
    except OSError:
        log.warning("Font not found at %s, falling back to default", FONT_PATH)
        header_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        footer_font = ImageFont.load_default()

    page_messages = messages[start:]
    total = len(messages)

    # Reserve space for footer
    footer_height = FOOTER_FONT_SIZE + MARGIN  # font + bottom margin
    max_y = DISPLAY_HEIGHT - footer_height

    y = MARGIN
    shown = 0

    for msg in page_messages:
        # Estimate space needed for this message header
        header_h = HEADER_FONT_SIZE + HEADER_SEP_GAP + 2 + SEP_BODY_GAP if msg["header"] else 0
        body_lines = msg["body"].split("\n") if msg["body"] else []

        # Check if this message fits (at least the header must fit)
        if shown > 0 and y + header_h > max_y:
            break

        # Draw header
        if msg["header"]:
            _draw_ansi_text(draw, MARGIN, y, msg["header"], header_font)
            y += HEADER_FONT_SIZE + HEADER_SEP_GAP
            draw.line([(MARGIN, y), (DISPLAY_WIDTH - MARGIN, y)], fill=(0, 0, 0), width=2)
            y += SEP_BODY_GAP

        # Draw body lines — always reserve space for 2 lines
        BODY_LINES_PER_MSG = 2
        truncated = False
        for li, line in enumerate(body_lines):
            if y + BODY_FONT_SIZE > max_y:
                truncated = True
                break
            _draw_ansi_text(draw, MARGIN, y, line, body_font)
            y += BODY_FONT_SIZE + BODY_LINE_GAP

        # If body was cut off, overwrite end of last drawn line with "..."
        if truncated and li > 0:
            ellipsis = "..."
            ew = body_font.getlength(ellipsis)
            ex = DISPLAY_WIDTH - MARGIN - ew
            ey = y - BODY_FONT_SIZE - BODY_LINE_GAP
            draw.rectangle([(ex, ey), (DISPLAY_WIDTH - MARGIN, ey + BODY_FONT_SIZE)], fill=(255, 255, 255))
            draw.text((ex, ey), ellipsis, font=body_font, fill=(0, 0, 0))

        # Advance y to fill 2 body lines regardless of actual count
        drawn_lines = min(len(body_lines), BODY_LINES_PER_MSG)
        remaining_lines = BODY_LINES_PER_MSG - drawn_lines
        y += remaining_lines * (BODY_FONT_SIZE + BODY_LINE_GAP)

        shown += 1
        y += MSG_GAP  # gap before next message

    # Draw footer
    first = start + 1
    last = start + shown
    if total == 0:
        footer_text = "No messages"
    elif shown == total:
        footer_text = f"{total} message{'s' if total != 1 else ''}"
    else:
        footer_text = f"Messages {first}–{last} of {total}  (◄ ► to page)"
    footer_w = footer_font.getlength(footer_text)
    footer_x = (DISPLAY_WIDTH - footer_w) / 2  # center
    footer_y = DISPLAY_HEIGHT - MARGIN - FOOTER_FONT_SIZE
    draw.text((footer_x, footer_y), footer_text, font=footer_font, fill=(128, 128, 128))

    # Convert RGB to subpixel-addressed grayscale for color e-paper
    global _last_frame, _displayed_ids
    _last_frame = img.copy()
    _displayed_ids = [m["id"] for m in page_messages[:shown]]
    _push_to_display(_rgb_to_subpixel(img))


# ---------------------------------------------------------------------------
# Claude usage tracking
# ---------------------------------------------------------------------------
import urllib.request

_CLAUDE_TOKEN_PATH = Path(__file__).parent / "claude_token.txt"
_USAGE_DB_PATH = Path(__file__).parent / "claude_usage.db"


def _init_usage_db():
    conn = sqlite3.connect(str(_USAGE_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            five_hour REAL,
            seven_day REAL,
            seven_day_opus REAL,
            seven_day_sonnet REAL
        )
    """)
    conn.commit()
    conn.close()


def _get_claude_token() -> str | None:
    try:
        return _CLAUDE_TOKEN_PATH.read_text().strip()
    except FileNotFoundError:
        return None


def fetch_claude_usage() -> dict | None:
    """Fetch current Claude usage from the OAuth endpoint."""
    token = _get_claude_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "homelab-dashboard/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception:
        log.exception("Failed to fetch Claude usage")
        return None


def _log_usage(usage: dict):
    """Store a usage sample in the local SQLite database."""
    five_hour = usage.get("five_hour", {}).get("utilization") if usage.get("five_hour") else None
    seven_day = usage.get("seven_day", {}).get("utilization") if usage.get("seven_day") else None
    seven_day_opus = usage.get("seven_day_opus", {}).get("utilization") if usage.get("seven_day_opus") else None
    seven_day_sonnet = usage.get("seven_day_sonnet", {}).get("utilization") if usage.get("seven_day_sonnet") else None
    conn = sqlite3.connect(str(_USAGE_DB_PATH))
    conn.execute(
        "INSERT INTO usage_log (timestamp, five_hour, seven_day, seven_day_opus, seven_day_sonnet) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), five_hour, seven_day, seven_day_opus, seven_day_sonnet),
    )
    conn.commit()
    conn.close()


_cached_usage: dict | None = None


def _usage_poll_loop():
    """Poll Claude usage every 15 minutes and log to SQLite."""
    import time
    global _cached_usage
    while True:
        usage = fetch_claude_usage()
        if usage:
            _cached_usage = usage
            _log_usage(usage)
            log.info("Claude usage: 5h=%.0f%%, 7d=%.0f%%",
                     usage.get("five_hour", {}).get("utilization", 0) or 0,
                     usage.get("seven_day", {}).get("utilization", 0) or 0)
            # If idle (no messages), refresh e-paper with usage
            if not get_queued_messages():
                render_idle()
                _push_to_display(_rgb_to_subpixel(_last_frame))
        time.sleep(900)  # 15 minutes


def _render_usage_chart(draw, img_width, img_height, font_path):
    """Render Claude usage info on the idle screen."""
    usage = _cached_usage
    if not usage:
        try:
            font = ImageFont.truetype(font_path, 40)
        except OSError:
            font = ImageFont.load_default()
        draw.text((MARGIN, MARGIN), "Claude Usage", font=font, fill=(0, 0, 0))
        draw.text((MARGIN, MARGIN + 60), "No token configured", font=font, fill=(128, 128, 128))
        draw.text((MARGIN, MARGIN + 120), "Place token in claude_token.txt", font=font, fill=(128, 128, 128))
        return

    try:
        title_font = ImageFont.truetype(font_path, 52)
        label_font = ImageFont.truetype(font_path, 40)
        value_font = ImageFont.truetype(font_path, 72)
        small_font = ImageFont.truetype(font_path, 30)
    except OSError:
        title_font = label_font = value_font = small_font = ImageFont.load_default()

    y = MARGIN
    draw.text((MARGIN, y), "Claude Usage", font=title_font, fill=(0, 0, 0))
    y += 80

    # Bar chart helper
    bar_x = MARGIN
    bar_width = img_width - 2 * MARGIN
    bar_height = 50

    for key, label in [("five_hour", "5-Hour"), ("seven_day", "7-Day"),
                        ("seven_day_sonnet", "7-Day Sonnet"), ("seven_day_opus", "7-Day Opus")]:
        section = usage.get(key)
        if section is None:
            continue
        util = section.get("utilization")
        if util is None:
            continue
        resets_at = section.get("resets_at", "")

        draw.text((bar_x, y), f"{label}: {util:.0f}%", font=label_font, fill=(0, 0, 0))
        y += 55

        # Draw bar background
        draw.rectangle([bar_x, y, bar_x + bar_width, y + bar_height], fill=(220, 220, 220))
        # Draw filled portion
        fill_w = int(bar_width * util / 100)
        if fill_w > 0:
            # Color: green < 50%, yellow 50-80%, red > 80%
            if util < 50:
                color = (0, 255, 0)
            elif util < 80:
                color = (255, 255, 0)
            else:
                color = (255, 0, 0)
            draw.rectangle([bar_x, y, bar_x + fill_w, y + bar_height], fill=color)
        # Draw bar border
        draw.rectangle([bar_x, y, bar_x + bar_width, y + bar_height], outline=(0, 0, 0), width=2)

        y += bar_height + 5
        if resets_at:
            reset_short = resets_at[:19].replace("T", " ")
            draw.text((bar_x, y), f"Resets: {reset_short}", font=small_font, fill=(100, 100, 100))
            y += 40
        y += 20

    # Draw diagonal reference line for 7-day usage
    seven_day = usage.get("seven_day")
    if seven_day and seven_day.get("utilization") is not None and seven_day.get("resets_at"):
        y += 20
        draw.text((bar_x, y), "7-Day Pace", font=label_font, fill=(0, 0, 0))
        y += 55

        chart_x = bar_x
        chart_w = bar_width
        chart_h = min(300, img_height - y - MARGIN - 40)
        chart_y = y

        # Chart background
        draw.rectangle([chart_x, chart_y, chart_x + chart_w, chart_y + chart_h],
                       fill=(245, 245, 245), outline=(0, 0, 0), width=2)

        # Diagonal line (0,0) to (100,100) — the "steady pace" line
        draw.line([(chart_x, chart_y + chart_h), (chart_x + chart_w, chart_y)],
                  fill=(100, 100, 100), width=5)

        # Calculate current position on the timeline
        from datetime import timezone
        resets_at_str = seven_day["resets_at"]
        try:
            reset_dt = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            # 7 days = 604800 seconds
            elapsed = 604800 - (reset_dt - now).total_seconds()
            time_pct = max(0, min(100, elapsed / 604800 * 100))
        except Exception:
            time_pct = 50  # fallback

        util = seven_day["utilization"]
        px = chart_x + int(chart_w * time_pct / 100)
        py = chart_y + chart_h - int(chart_h * util / 100)

        # Draw current point
        r = 8
        if util > time_pct:
            dot_color = (255, 0, 0)  # "losing" — above the line
        else:
            dot_color = (0, 255, 0)  # "winning" — below the line
        draw.ellipse([px - r, py - r, px + r, py + r], fill=dot_color, outline=(0, 0, 0), width=2)

        # Label
        status = "over pace" if util > time_pct else "under pace"
        draw.text((chart_x + 10, chart_y + 10),
                  f"{util:.0f}% used, {time_pct:.0f}% elapsed ({status})",
                  font=small_font, fill=(0, 0, 0))


def render_idle():
    """Show Claude usage on the idle screen when the queue is empty."""
    img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    _render_usage_chart(draw, DISPLAY_WIDTH, DISPLAY_HEIGHT, FONT_PATH)
    global _last_frame, _displayed_ids
    _last_frame = img.copy()
    _displayed_ids = []
    _push_to_display(_rgb_to_subpixel(img))


def _push_to_display(img: Image.Image):
    """Send a PIL image to the IT8951 e-paper display."""
    with _display_lock:
        epd = _get_epd()
        if epd is None:
            log.info("No e-paper display — skipping render")
            return
        try:
            epd.clear()
            epd.frame_buf.paste(0xFF, box=(0, 0, epd.width, epd.height))
            epd.frame_buf.paste(img, (0, 0))
            epd.draw_full(constants.DisplayModes.GC16)
            log.info("E-paper display updated")
        except Exception:
            log.exception("Failed to update e-paper display")


def update_display():
    """Render Claude usage on the e-paper (always usage, messages are web-only)."""
    render_idle()



def next_page():
    """Advance to the next page of messages."""
    global _page_stack
    messages = get_queued_messages()
    if not messages:
        return
    next_start = (_page_stack[-1] if _page_stack else 0) + len(_displayed_ids)
    if next_start < len(messages):
        _page_stack.append(next_start)
        threading.Thread(target=update_display, daemon=True).start()


def prev_page():
    """Go back to the previous page of messages."""
    global _page_stack
    if len(_page_stack) > 1:
        _page_stack.pop()
        threading.Thread(target=update_display, daemon=True).start()


def clear_page():
    """Dismiss messages on the current page and return to page 0."""
    global _page_stack
    ids = list(_displayed_ids)
    dismiss_displayed(ids)
    _page_stack[:] = [0]
    threading.Thread(target=update_display, daemon=True).start()


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def _validate_message(header: str) -> str | None:
    """Return error string if invalid, None if OK."""
    if not header:
        return "header is required"
    return None


def _msg_to_json(msg: dict) -> dict:
    return {"id": msg["id"], "sender": msg.get("sender", ""),
            "header": msg["header"], "body": msg["body"],
            "created_at": msg["created_at"], "status": msg["status"]}



_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Homelab Dashboard</title>
<style>
  body { background: #0a0a0f; color: #e0e0e0; font-family: system-ui, sans-serif; margin: 2rem; }
  h1 { color: #22d3ee; }
  a { color: #22d3ee; }
  a:hover { color: #67e8f9; }
</style>
</head>
<body>
<h1>Homelab Dashboard</h1>
<ul>
  <li><a href="/openapi.json">API Specification (OpenAPI JSON)</a></li>
  <li><a href="/docs">Interactive API Documentation (Swagger UI)</a></li>
  <li><a href="/redoc">API Documentation (ReDoc)</a></li>
  <li><a href="/dashboard">Dashboard</a></li>
  <li><a href="/settings">Display Settings</a></li>
</ul>
</body>
</html>"""


@app.get("/", include_in_schema=False)
async def root():
    return Response(content=_INDEX_HTML, media_type="text/html")


@app.get("/index.html", include_in_schema=False)
async def index_html():
    return Response(content=_INDEX_HTML, media_type="text/html")


@app.get("/api/messages", response_model=list[MessageOut],
         summary="List all active messages",
         description="Returns all queued (non-dismissed) messages, oldest first.")
async def list_messages():
    return [_msg_to_json(m) for m in get_queued_messages()]


@app.get("/api/message/{msg_id}", response_model=MessageOut,
         summary="Get a single message",
         description="Returns a message by ID. Returns 404 if not found.",
         responses={404: {"model": ErrorOut}})
async def get_message(msg_id: int):
    msg = get_message_by_id(msg_id)
    if not msg:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _msg_to_json(msg)


@app.post("/api/message", response_model=CreatedOut, status_code=201,
          summary="Create a new message",
          description="Post a message to the dashboard. "
                      "Body supports **Markdown** formatting (headings, lists, code blocks, links, etc.). "
                      "No character limits — agents are free to post whatever they need.",
          responses={400: {"model": ErrorOut}})
async def post_message(data: MessageIn):
    err = _validate_message(data.header)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    msg_id = add_message(data.sender, data.header, data.body)
    log.info("Message %d queued from %s: %s", msg_id, data.sender, data.header)
    return JSONResponse({"id": msg_id, "status": "queued"}, status_code=201)


@app.put("/api/message/{msg_id}", response_model=MessageOut,
         summary="Update a message",
         description="Update the header and/or body of an existing queued message. "
                     "Omitted fields keep their current value.",
         responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}})
async def update_message(msg_id: int, data: MessageIn):
    msg = get_message_by_id(msg_id)
    if not msg or msg["status"] != "queued":
        return JSONResponse({"error": "not found"}, status_code=404)

    header = data.header
    body = data.body

    err = _validate_message(header)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE messages SET sender=?, header=?, body=? WHERE id=?",
                     (data.sender, header, body, msg_id))
        conn.commit()
        conn.close()

    log.info("Message %d updated", msg_id)
    return _msg_to_json(get_message_by_id(msg_id))


@app.delete("/api/message/{msg_id}", response_model=StatusOut,
            summary="Dismiss a message",
            description="Remove a single message from the display.",
            responses={404: {"model": ErrorOut}})
async def delete_message(msg_id: int):
    msg = get_message_by_id(msg_id)
    if not msg or msg["status"] != "queued":
        return JSONResponse({"error": "not found"}, status_code=404)
    dismiss_message(msg_id)
    log.info("Message %d dismissed", msg_id)
    return {"status": "dismissed"}


@app.delete("/api/messages/displayed", response_model=StatusOut,
            summary="Dismiss on-screen messages",
            description="Dismiss only the messages currently rendered on the e-paper display. "
                        "Queued messages not yet shown are preserved. Used by the WPS button hook.")
async def delete_displayed_messages():
    global _page_stack
    ids = list(_displayed_ids)
    dismiss_displayed(ids)
    _page_stack[:] = [0]
    log.info("Displayed messages dismissed: %s", ids)
    return {"status": f"dismissed {len(ids)} displayed messages"}


@app.delete("/api/messages", response_model=StatusOut,
            summary="Dismiss all messages",
            description="Remove all messages from the display and show the idle screen.")
async def delete_all_messages():
    dismiss_all()
    log.info("All messages dismissed")
    return {"status": "all dismissed"}


@app.get("/api/frame",
         summary="Get current display frame as PNG",
         description="Returns the last rendered frame as a PNG image. "
                     "This is the RGB image before color e-paper subpixel conversion.",
         responses={200: {"content": {"image/png": {}}}, 404: {"model": ErrorOut}})
async def get_frame():
    if _last_frame is None:
        return JSONResponse({"error": "no frame rendered yet"}, status_code=404)
    buf = io.BytesIO()
    _last_frame.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# ---------------------------------------------------------------------------
# Mouse input listener
# ---------------------------------------------------------------------------
import struct as _struct

MOUSE_DEVICE = "/dev/input/event0"
_EV_KEY = 0x01
_BTN_LEFT   = 0x110  # 272
_BTN_RIGHT  = 0x111  # 273
_BTN_MIDDLE = 0x112  # 274
# 64-bit Linux input_event: timeval(8+8) + type(2) + code(2) + value(4) = 24 bytes
_INPUT_EVENT_FMT  = "qqHHi"
_INPUT_EVENT_SIZE = _struct.calcsize(_INPUT_EVENT_FMT)


def mouse_listener():
    """Read mouse events and map clicks to page navigation / clear."""
    import time as _time
    while True:
        try:
            with open(MOUSE_DEVICE, "rb") as f:
                log.info("Mouse listener started on %s (event size=%d)", MOUSE_DEVICE, _INPUT_EVENT_SIZE)
                while True:
                    data = f.read(_INPUT_EVENT_SIZE)
                    if len(data) < _INPUT_EVENT_SIZE:
                        break
                    _, _, ev_type, ev_code, ev_value = _struct.unpack(_INPUT_EVENT_FMT, data)
                    if ev_type == _EV_KEY and ev_value == 1:  # key-press only
                        if ev_code == _BTN_LEFT:
                            log.info("Mouse: left click → prev page")
                            prev_page()
                        elif ev_code == _BTN_RIGHT:
                            log.info("Mouse: right click → next page")
                            next_page()
                        elif ev_code == _BTN_MIDDLE:
                            log.info("Mouse: middle click → clear page")
                            clear_page()
        except FileNotFoundError:
            log.warning("Mouse device %s not found, retrying in 5s", MOUSE_DEVICE)
            _time.sleep(5)
        except Exception:
            log.exception("Mouse listener error, retrying in 5s")
            _time.sleep(5)


# ---------------------------------------------------------------------------
# NiceGUI Web UI
# ---------------------------------------------------------------------------

@ui.page("/dashboard")
def dashboard():
    ui.add_head_html('<meta name="viewport" content="width=device-width, initial-scale=1">')
    ui.dark_mode(True)
    ui.query('body').classes('bg-gray-950')
    ui.query('.nicegui-content').classes('p-0 gap-0')

    # Full-height two-column layout: content left, avatar right
    ui.add_head_html(f'<style>body {{ background-color: #181819 !important; }}</style>')

    # Two-column layout via CSS grid: content left, avatar right
    # Avatar aspect ratio ~1080:2340, so at 100vh height, width ≈ 46vh
    ui.add_head_html('''<style>
    .dashboard-grid {
        display: grid !important;
        grid-template-columns: 1fr 46vh;
        height: 100vh;
        width: 100vw;
        overflow: hidden;
    }
    .dashboard-grid > div { min-width: 0; min-height: 0; }
    </style>''')
    with ui.element('div').classes('dashboard-grid'):
        # Left — scrollable message content
        with ui.scroll_area().style('height:100vh;'):
            with ui.column().classes('w-full p-6 gap-4'):
                with ui.column().classes(
                    'w-full items-center py-8 px-4 mb-6 rounded-2xl '
                    'bg-gradient-to-br from-gray-900 via-gray-900 to-cyan-950 '
                    'border border-cyan-500/30'
                ):
                    ui.label('Homelab Dashboard').classes('text-4xl font-bold text-cyan-400 tracking-tight')
                    ui.label('Agent Message Board').classes('text-gray-400 text-lg mt-1')

                messages_container = ui.column().classes('w-full gap-3')

        # Right — avatar
        ui.image('/img/avatar.webp').style(
            'height:100vh; width:100%; object-fit:contain; object-position:right top;'
        ).props('fit=contain no-spinner')

    def _fmt_time(iso_str):
        """Format ISO timestamp to a friendly relative/absolute string."""
        try:
            dt = datetime.fromisoformat(iso_str)
            now = datetime.now()
            delta = now - dt
            if delta.total_seconds() < 60:
                return "just now"
            elif delta.total_seconds() < 3600:
                mins = int(delta.total_seconds() / 60)
                return f"{mins}m ago"
            elif delta.total_seconds() < 86400:
                hours = int(delta.total_seconds() / 3600)
                return f"{hours}h ago"
            else:
                return dt.strftime("%b %d, %H:%M")
        except Exception:
            return iso_str

    def refresh():
        messages = get_queued_messages()

        messages_container.clear()
        with messages_container:
            if not messages:
                with ui.card().classes('w-full p-8 rounded-2xl bg-gray-900 border border-gray-800 shadow-none'):
                    ui.label('No messages').classes('text-gray-500 italic text-lg text-center')
            else:
                ui.button('Clear All', on_click=do_clear_all).props(
                    'color=red flat').classes('self-end')
                for msg in messages:
                    sender = msg.get("sender", "") or "unknown"
                    stamp = _fmt_time(msg["created_at"])
                    with ui.card().classes('w-full p-5 rounded-2xl bg-gray-900 shadow-none').style(
                        'border: 1px solid rgba(34, 211, 238, 0.3); box-shadow: none;'
                    ):
                        with ui.row().classes('w-full items-center justify-between mb-2'):
                            with ui.row().classes('items-center gap-2'):
                                ui.label(sender).classes('text-cyan-400 font-semibold text-sm')
                                ui.label(stamp).classes('text-gray-500 text-xs')
                            ui.button(icon='close',
                                      on_click=lambda mid=msg["id"]: do_dismiss(mid)
                                      ).props('flat dense round color=grey-7 size=sm')
                        ui.label(msg["header"]).classes('text-lg font-bold text-cyan-300')
                        if msg["body"]:
                            ui.markdown(msg["body"]).classes('text-gray-300')

    def do_dismiss(msg_id):
        dismiss_message(msg_id)
        refresh()

    def do_clear_all():
        dismiss_all()
        refresh()

    refresh()
    ui.timer(5.0, refresh)


@ui.page("/settings")
def settings_page():
    ui.add_head_html('<meta name="viewport" content="width=device-width, initial-scale=1">')
    ui.dark_mode(True)
    ui.query('body').classes('bg-gray-950')
    ui.query('.nicegui-content').classes('p-0 gap-0')
    settings = _load_settings()

    with ui.column().classes('w-full max-w-2xl mx-auto p-6 gap-4'):
        with ui.column().classes(
            'w-full items-center py-8 px-4 mb-4 rounded-2xl '
            'bg-gradient-to-br from-gray-900 via-gray-900 to-cyan-950 '
            'border border-cyan-500/30'
        ):
            ui.label('Display Settings').classes('text-4xl font-bold text-cyan-400 tracking-tight')

        ui.link('Back to Dashboard', '/dashboard').classes(
            'px-4 py-2 rounded-full bg-cyan-950 text-cyan-400 border border-cyan-500/30 '
            'hover:bg-cyan-900 hover:border-cyan-400 font-medium text-sm no-underline transition-colors self-start'
        )

        # VCOM slider
        with ui.card().classes('w-full p-6 rounded-2xl bg-gray-900 border border-gray-800'):
            ui.label('VCOM Voltage').classes('text-lg font-semibold text-cyan-400 mb-2')
            ui.label('Adjusts panel contrast. More negative = darker blacks. '
                     'Check your panel\'s FPC cable for the recommended value.').classes('text-xs text-gray-500 mb-4')
            vcom_label = ui.label(f'{settings["vcom"]:.2f} V').classes('text-2xl font-mono text-cyan-300')
            vcom_slider = ui.slider(min=-3.0, max=-0.5, step=0.05, value=settings['vcom']).props('label-always color=cyan')
            vcom_slider.on_value_change(lambda e: vcom_label.set_text(f'{e.value:.2f} V'))

        status_label = ui.label('').classes('text-sm text-cyan-500')

        def apply_settings():
            new_settings = {
                'vcom': round(vcom_slider.value, 2),
            }
            _save_settings(new_settings)
            _reinit_epd()
            threading.Thread(target=update_display, daemon=True).start()
            status_label.set_text(f'Settings applied. VCOM={new_settings["vcom"]:.2f}V. '
                                  f'Display refreshing...')

        ui.button('Apply & Refresh Display', on_click=apply_settings).props('color=cyan')


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def on_startup():
    init_db()
    _init_usage_db()
    log.info("Database initialised at %s", DB_PATH)
    log.info("E-paper available: %s", EPAPER_AVAILABLE)
    # Do an initial usage fetch before first render
    global _cached_usage
    usage = fetch_claude_usage()
    if usage:
        _cached_usage = usage
        _log_usage(usage)
        log.info("Initial Claude usage fetched: 5h=%.0f%%, 7d=%.0f%%",
                 usage.get("five_hour", {}).get("utilization", 0) or 0,
                 usage.get("seven_day", {}).get("utilization", 0) or 0)
    # Render whatever is current (or idle with usage)
    threading.Thread(target=update_display, daemon=True).start()
    threading.Thread(target=mouse_listener, daemon=True).start()
    log.info("Mouse listener thread started")
    threading.Thread(target=_usage_poll_loop, daemon=True).start()
    log.info("Claude usage poll thread started (every 15min)")

app.on_startup(on_startup)

ui.run(host="0.0.0.0", port=8090, title="Homelab Dashboard", reload=False,
       fastapi_docs=True)
