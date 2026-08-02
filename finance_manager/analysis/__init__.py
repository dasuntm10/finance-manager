"""Derived-insight modules that read transactions and produce analysis.

Modules:
    recurring      Recurring charge and subscription detection.
    budget_alerts  Budget utilization, projection and alerting.
"""

from finance_manager.analysis.budget_alerts import evaluate_budget
from finance_manager.analysis.recurring import detect_recurring, summarize_recurring

__all__ = ["detect_recurring", "summarize_recurring", "evaluate_budget"]
