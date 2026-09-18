"""Load environment variables and expose typed config values.

This is the single place the .env file is located and loaded. Import config
values from here rather than calling load_dotenv() and os.getenv() again —
modules live at different directory depths, so hand-rolled relative paths
to .env are easy to get wrong.

Lookup order for the .env file:
    1. $FORM25_ENV_FILE, if set (explicit override)
    2. <repo root>/.env
    3. <repo root>/../.env   (a parent-directory .env, the historical layout)

See .env.example for the variables that are read.
"""

import os
from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_env_file() -> str | None:
    override = os.getenv("FORM25_ENV_FILE")
    if override:
        return override if os.path.isfile(override) else None
    for candidate in (
        os.path.join(_REPO_ROOT, ".env"),
        os.path.join(os.path.dirname(_REPO_ROOT), ".env"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


ENV_FILE = _find_env_file()
if ENV_FILE:
    load_dotenv(ENV_FILE)

# ---------------------------------------------------------------------------
# API keys
#
# Every key below is read by real code. If you add one here, use it — an unused
# key in config is a credential that gets provisioned, committed to someone's
# .env, and never rotated because nobody remembers what it was for.
# ---------------------------------------------------------------------------

# FRED (Federal Reserve Economic Data) — macro context. Used by data/fetch_macro.py.
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")


# ---------------------------------------------------------------------------
# SEC EDGAR access
#
# SEC Fair Access requires every automated request to carry a User-Agent that
# names the tool and a contact address the SEC can actually reach. There is no
# default here on purpose: a hardcoded address would either be a fake one (not
# compliant, and the SEC throttles or blocks on it) or somebody's personal
# address committed to a public repo. Set SEC_CONTACT_EMAIL in .env.
# ---------------------------------------------------------------------------

SEC_CONTACT_EMAIL: str = os.getenv("SEC_CONTACT_EMAIL", "").strip()


def sec_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """
    Request headers for any SEC endpoint (www.sec.gov and data.sec.gov).

    Raises if SEC_CONTACT_EMAIL is unset — failing here is better than being
    rate-limited into silence halfway through a two-hour EDGAR scan.
    """
    if not SEC_CONTACT_EMAIL:
        raise RuntimeError(
            "SEC_CONTACT_EMAIL is not set. SEC Fair Access requires a real "
            "contact address in the User-Agent for automated requests.\n"
            "Set it in .env, e.g.  SEC_CONTACT_EMAIL=you@example.com\n"
            "See https://www.sec.gov/os/webmaster-faq#developers"
        )
    headers = {
        "User-Agent": f"Form25 Research Tool ({SEC_CONTACT_EMAIL})",
        "Accept-Encoding": "gzip, deflate",
    }
    if extra:
        headers.update(extra)
    return headers


def web_headers() -> dict[str, str]:
    """
    Headers for non-SEC public data sources (Treasury, Damodaran).

    These hosts have no Fair Access rule, so a missing contact address is not
    fatal — it is just less polite. Hence no raise, unlike sec_headers().
    """
    contact = f" ({SEC_CONTACT_EMAIL})" if SEC_CONTACT_EMAIL else ""
    return {"User-Agent": f"Form25 Research Tool{contact}"}


# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Backtest window
#
# One definition, used as the default by every command and by the README's
# reproduction steps. Three different pairs of defaults used to be scattered
# across the CLI, none of which matched the window the published results were
# actually computed over.
#
# The start is the first day the reconstructed universe has point-in-time
# listing data for; the end is the last day of the published run.
# ---------------------------------------------------------------------------

DEFAULT_START_DATE = "2019-06-14"
DEFAULT_END_DATE   = "2026-09-18"

# DB paths
DB_DIR = os.path.join(_REPO_ROOT, "db")
FORM25_DB_PATH = os.path.join(DB_DIR, "form25.db")
PRICES_DB_PATH = os.path.join(DB_DIR, "prices.db")
