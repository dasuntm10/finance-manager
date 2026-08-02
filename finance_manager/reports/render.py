"""Render a MonthlyReport to standalone HTML.

The output is a single self-contained file with inline CSS and no external
assets, so it can be emailed, archived, or opened offline. Print styles are
included so a browser's "Save as PDF" produces a clean document.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from finance_manager.schemas import MonthlyReport


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_TEMPLATE_NAME = "monthly_report.html.j2"


def _format_money(value: Optional[float], currency: str = "") -> str:
    if value is None:
        return "n/a"
    formatted = f"{value:,.2f}"
    return f"{formatted} {currency}".strip()


def _format_pct(value: Optional[float], digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}%"


def _format_signed_pct(value: Optional[float], digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}%"


def _format_date(value, fmt: str = "%d %b %Y") -> str:
    if value is None:
        return "n/a"
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = _format_money
    env.filters["pct"] = _format_pct
    env.filters["signed_pct"] = _format_signed_pct
    env.filters["nice_date"] = _format_date
    return env


def render_report_html(report: MonthlyReport) -> str:
    """Render the monthly report as a standalone HTML document."""
    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(report=report)


def write_report_html(report: MonthlyReport, destination: Path) -> Path:
    """Render the report and write it to disk, returning the path written."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report_html(report), encoding="utf-8")
    return destination
