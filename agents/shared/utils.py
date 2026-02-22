"""
Shared utilities for all CSF intelligence agents.
=================================================
Provides:
  - setup_logging       — consistent logging (console + rotating file)
  - http_get_with_retry — HTTP GET with exponential backoff
  - load_json           — safe JSON loading
  - save_json           — atomic JSON write (temp file → rename)
  - ensure_dir          — mkdir -p helper

All agents import from here. Keep this file focused and stable;
new agents should be able to copy-paste the import block.
"""

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(
    name: str,
    level: str = "INFO",
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """
    Configure a named logger with console output and optional rotating file.

    Args:
        name:     Logger name (shown in every log line).
        level:    "DEBUG" | "INFO" | "WARNING" | "ERROR"
        log_file: If provided, also write to this rotating log file.

    Returns:
        Configured Logger instance.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # Avoid duplicate handlers when called multiple times (e.g. in tests)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always write to console
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler: 10 MB max, keep 5 backups
    if log_file:
        ensure_dir(Path(log_file).parent)
        fh = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(log_level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get_with_retry(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 30,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    logger: Optional[logging.Logger] = None,
) -> requests.Response:
    """
    HTTP GET with exponential backoff on transient failures.

    Retries on connection errors and HTTP 429/5xx responses.
    Each retry waits retry_delay * 2^(attempt-1) seconds.

    Args:
        url:         Target URL.
        params:      Query parameters dict.
        headers:     HTTP headers dict.
        timeout:     Per-request timeout in seconds.
        max_retries: Maximum number of attempts.
        retry_delay: Base delay in seconds between retries.
        logger:      Logger instance (uses module logger if None).

    Returns:
        requests.Response with 2xx status.

    Raises:
        requests.RequestException: after all retries exhausted.
    """
    log = logger or logging.getLogger(__name__)

    session = requests.Session()

    # urllib3-level retry for connection issues (not application-level 4xx/5xx)
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=max_retries,
            backoff_factor=retry_delay,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            wait = retry_delay * (2 ** (attempt - 1))
            log.warning(
                f"HTTP GET attempt {attempt}/{max_retries} failed ({exc}). "
                f"Retrying in {wait:.1f}s — {url}"
            )
            time.sleep(wait)

    log.error(f"HTTP GET failed after {max_retries} attempts: {url} — {last_exc}")
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

def load_json(path: Path, logger: Optional[logging.Logger] = None) -> dict:
    """
    Load JSON from a file. Returns empty dict on missing file or parse error.
    """
    log = logger or logging.getLogger(__name__)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        log.warning(f"JSON parse error in {path}: {exc}. Returning empty dict.")
        return {}


def save_json(data: Any, path: Path, logger: Optional[logging.Logger] = None) -> None:
    """
    Atomically write data as pretty-printed JSON.

    Writes to a .tmp file first, then renames to the target path.
    This prevents a partially-written file from corrupting stored data
    if the process is interrupted mid-write.
    """
    log = logger or logging.getLogger(__name__)
    path = Path(path)
    ensure_dir(path.parent)

    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        tmp_path.replace(path)
    except Exception as exc:
        log.error(f"Failed to save JSON to {path}: {exc}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def ensure_dir(path: Path) -> None:
    """Create directory (and all parents) if it doesn't already exist."""
    Path(path).mkdir(parents=True, exist_ok=True)
