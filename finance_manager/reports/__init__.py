"""Monthly report generation: data assembly, narrative, and HTML rendering."""

from finance_manager.reports.builder import build_monthly_report
from finance_manager.reports.narrative import generate_narrative
from finance_manager.reports.render import render_report_html

__all__ = ["build_monthly_report", "generate_narrative", "render_report_html"]
