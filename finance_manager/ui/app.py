from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import streamlit as st
from huggingface_hub import InferenceClient
from openai import OpenAI

from finance_manager.config import get_settings
from finance_manager.db import get_repository
from finance_manager.graph import FinanceGraphRunner, get_graph_runner
from finance_manager.reports.render import render_report_html
from finance_manager.schemas import Budget, MonthlyReport


settings = get_settings()
runner: FinanceGraphRunner = get_graph_runner()
openai_client: Optional[OpenAI] = None
hf_client: Optional[InferenceClient] = None

DEFAULT_USER_ID = "demo-user"


def _current_user() -> str:
    return st.session_state.get("user_id") or DEFAULT_USER_ID


def _current_month() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def _run_graph(task_type: str, **kwargs: Any) -> Dict[str, Any]:
    # Every graph run is scoped to the active user unless a caller overrides it.
    kwargs.setdefault("user_id", _current_user())
    return asyncio.run(runner.arun(task_type=task_type, **kwargs))


def _save_budget(budget: Budget) -> Budget:
    return asyncio.run(get_repository().set_budget(budget))


def _load_budget(month: str) -> Optional[Budget]:
    return asyncio.run(get_repository().get_budget(user_id=_current_user(), month=month))


def _show_errors(state: Dict[str, Any]) -> None:
    for message in state.get("errors") or []:
        st.error(message)


def _render_alerts(alerts: List[Dict[str, Any]]) -> None:
    """Render budget alerts with the severity mapped to a Streamlit callout."""
    if not alerts:
        st.success("No budget alerts. Everything is within its limits.")
        return
    for alert in alerts:
        severity = alert.get("severity")
        message = alert.get("message", "")
        if severity == "critical":
            st.error(message)
        elif severity == "warning":
            st.warning(message)
        else:
            st.info(message)


def _transcribe_audio(file_bytes: bytes, filename: str) -> str:
    provider = (settings.stt_provider or "whisper").lower()
    if provider == "whisper":
        if settings.openai_api_key:
            global openai_client
            if openai_client is None:
                openai_client = OpenAI(api_key=settings.openai_api_key)
            audio_file = ("audio", (filename, file_bytes, "audio/mpeg"))
            resp = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
            return resp.text  # type: ignore[attr-defined]
        else:
            raise RuntimeError("OPENAI_API_KEY not set for Whisper transcription")
    if provider == "qwen":
        if settings.hf_token:
            global hf_client
            if hf_client is None:
                hf_client = InferenceClient(token=settings.hf_token)
            # Qwen2.5-2B-Instruct can handle speech via asr; using hosted endpoint
            result = hf_client.post(
                "Qwen/Qwen2.5-2B-Instruct",
                headers={"Accept": "application/json"},
                data={"inputs": {"audio": file_bytes}},
            )
            if isinstance(result, dict):
                return result.get("text") or result.get("generated_text") or ""
        else:
            raise RuntimeError("HF_TOKEN not set for Qwen transcription")
    raise RuntimeError("Unsupported STT provider")


def _transactions_df(state: Dict[str, Any]) -> pd.DataFrame:
    txs = state.get("parsed_transactions") or []
    if not txs:
        return pd.DataFrame()
    df = pd.DataFrame(txs)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def tab_dashboard() -> None:
    st.subheader("Spend vs Budget")
    if st.button("Refresh analytics"):
        state = _run_graph("analytics", raw_input={"range": "all"})
        analytics = state.get("analytics_result") or {}
        monthly = analytics.get("monthly_spend") or []
        category_dist = analytics.get("category_distribution") or {}
        rolling = analytics.get("rolling_average") or []

        if monthly:
            fig = px.line(monthly, x="month", y="total", title="Monthly spend")
            st.plotly_chart(fig, use_container_width=True)

        if category_dist:
            cat_df = pd.DataFrame([{"category": k, "amount": v} for k, v in category_dist.items()])
            c1, c2 = st.columns(2)
            with c1:
                pie = px.pie(cat_df, names="category", values="amount", title="Category distribution")
                st.plotly_chart(pie, use_container_width=True)
            with c2:
                bar = px.bar(cat_df.sort_values("amount", ascending=False), x="category", y="amount", title="Spend by category")
                st.plotly_chart(bar, use_container_width=True)

        if rolling:
            roll_df = pd.DataFrame(rolling)
            if not roll_df.empty:
                roll_df["timestamp"] = pd.to_datetime(roll_df["timestamp"])
                roll_fig = px.line(roll_df, x="timestamp", y="average", title="30-day rolling average")
                st.plotly_chart(roll_fig, use_container_width=True)

        st.json(analytics)

    st.divider()
    st.subheader("This month against budget")
    if st.button("Check budget status"):
        state = _run_graph("budget", raw_input={"month": _current_month()})
        _show_errors(state)
        status = state.get("budget_status") or {}
        if status:
            cols = st.columns(4)
            cols[0].metric("Spent", f"{status.get('total_spent', 0):.2f}")
            cols[1].metric(
                "Limit",
                f"{status['total_limit']:.2f}" if status.get("total_limit") else "not set",
            )
            cols[2].metric(
                "Projected month end",
                f"{status['projected_total']:.2f}" if status.get("projected_total") else "n/a",
            )
            cols[3].metric(
                "Day", f"{status.get('days_elapsed', 0)} / {status.get('days_in_month', 0)}"
            )

            utilization = status.get("total_utilization")
            if utilization is not None:
                st.progress(min(float(utilization), 1.0))

            _render_alerts(status.get("alerts") or [])

            categories = status.get("categories") or []
            if categories:
                st.dataframe(pd.DataFrame(categories), use_container_width=True)


def tab_transactions() -> None:
    st.subheader("Transactions")
    state = st.session_state.get("last_ingest_state")
    if state:
        df = _transactions_df(state)
        if not df.empty:
            st.dataframe(df)
        else:
            st.info("No transactions yet.")
    else:
        st.info("Ingest data in SMS/PDF tabs to view transactions.")


def tab_ingest_sms() -> None:
    st.subheader("Ingest SMS batch")
    senders = st.text_input("Allowed senders (comma separated)", ",".join(settings.default_bank_senders))
    sms_blob = st.text_area("Paste SMS lines (one per line)")
    if st.button("Ingest SMS"):
        messages = []
        for line in sms_blob.splitlines():
            if not line.strip():
                continue
            messages.append({"text": line.strip(), "sender": "USER"})
        state = _run_graph("sms_batch", raw_input={"messages": messages, "senders": [s.strip() for s in senders.split(",")]})
        st.session_state["last_ingest_state"] = state
        st.success(f"Ingested {len(state.get('parsed_transactions') or [])} transactions")


def tab_ingest_pdf() -> None:
    st.subheader("Ingest PDFs")
    uploads = st.file_uploader("Upload PDF or text files", type=["pdf", "txt"], accept_multiple_files=True)
    if st.button("Process PDFs") and uploads:
        import tempfile

        paths: List[str] = []
        for f in uploads:
            suffix = Path(f.name).suffix or ".pdf"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(f.getbuffer())
                paths.append(tmp.name)
        state = _run_graph("pdf_upload", files=paths)
        st.session_state["last_ingest_state"] = state
        st.success(f"Processed {len(paths)} files, extracted {len(state.get('parsed_transactions') or [])} transactions")
        for path in paths:
            try:
                Path(path).unlink()
            except Exception:
                pass


def tab_ingest_email() -> None:
    st.subheader("Ingest bank emails")
    st.caption(
        "Fetches recent mail over IMAP and turns bank alerts into transactions. "
        "The mailbox is opened read-only, so nothing is marked as seen."
    )

    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        st.warning(
            "IMAP is not configured. Set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD "
            "in the environment to enable email ingestion."
        )
    else:
        st.success(f"Mailbox: {settings.imap_username} at {settings.imap_host}")

    col1, col2, col3 = st.columns(3)
    folder = col1.text_input("Folder", settings.imap_folder)
    since_days = col2.number_input(
        "Look back (days)", min_value=1, max_value=365, value=settings.email_since_days
    )
    limit = col3.number_input(
        "Max messages", min_value=1, max_value=2000, value=settings.email_fetch_limit
    )

    if st.button("Fetch and ingest"):
        with st.spinner("Reading mailbox..."):
            state = _run_graph(
                "email_batch",
                raw_input={
                    "folder": folder,
                    "since_days": int(since_days),
                    "limit": int(limit),
                },
            )
        _show_errors(state)
        st.session_state["last_ingest_state"] = state

        fetched = len(state.get("email_messages") or [])
        transactions = state.get("parsed_transactions") or []
        st.success(
            f"Fetched {fetched} message(s) and extracted {len(transactions)} transaction(s). "
            "Messages without an amount are skipped."
        )
        if transactions:
            st.dataframe(_transactions_df(state), use_container_width=True)

        status = state.get("budget_status") or {}
        if status.get("alerts"):
            st.subheader("Budget impact")
            _render_alerts(status["alerts"])


def tab_budget() -> None:
    st.subheader("Budget")
    month = st.text_input("Month (YYYY-MM)", _current_month())

    existing = _load_budget(month)
    default_total = existing.total_limit if existing else 1000.0
    default_limits = (
        "\n".join(f"{cat}:{amount:g}" for cat, amount in existing.per_category_limits.items())
        if existing and existing.per_category_limits
        else "Food:300\nTransport:150"
    )
    if existing:
        st.caption(f"Loaded the saved budget for {month}.")

    total = st.number_input("Total limit", min_value=0.0, value=float(default_total), step=50.0)
    category_limits = st.text_area("Per-category limits (cat:amount per line)", default_limits)

    if st.button("Save budget"):
        per_cat: Dict[str, float] = {}
        for line in category_limits.splitlines():
            if ":" in line:
                cat, amt = line.split(":", 1)
                try:
                    per_cat[cat.strip()] = float(amt.strip())
                except ValueError:
                    continue
        saved = _save_budget(
            Budget(
                user_id=_current_user(),
                month=month,
                total_limit=float(total),
                per_category_limits=per_cat,
            )
        )
        st.success(f"Budget saved for {saved.month}.")

    st.divider()
    st.subheader("Status and alerts")
    if st.button("Evaluate this budget"):
        state = _run_graph("budget", raw_input={"month": month})
        _show_errors(state)
        status = state.get("budget_status") or {}
        if not status:
            st.info("No budget status returned.")
            return

        col1, col2, col3 = st.columns(3)
        col1.metric("Spent", f"{status.get('total_spent', 0):.2f}")
        col2.metric(
            "Remaining",
            f"{status['total_remaining']:.2f}" if status.get("total_remaining") is not None else "n/a",
        )
        col3.metric(
            "Projected",
            f"{status['projected_total']:.2f}" if status.get("projected_total") is not None else "n/a",
        )

        _render_alerts(status.get("alerts") or [])
        categories = status.get("categories") or []
        if categories:
            st.dataframe(pd.DataFrame(categories), use_container_width=True)


def tab_subscriptions() -> None:
    st.subheader("Recurring charges and subscriptions")
    st.caption(
        "Groups transactions by merchant and looks for a regular cadence. "
        "Detection needs a few charges of history before a series appears."
    )

    col1, col2 = st.columns(2)
    min_occurrences = col1.number_input(
        "Minimum charges to qualify",
        min_value=2,
        max_value=12,
        value=settings.recurring_min_occurrences,
    )
    upcoming_days = col2.number_input(
        "Upcoming window (days)",
        min_value=1,
        max_value=90,
        value=settings.recurring_upcoming_days,
    )

    if st.button("Detect recurring charges"):
        state = _run_graph(
            "recurring",
            raw_input={
                "min_occurrences": int(min_occurrences),
                "upcoming_days": int(upcoming_days),
            },
        )
        _show_errors(state)
        summary = state.get("recurring_summary") or {}
        series = state.get("recurring_series") or []

        if not series:
            st.info(
                "No recurring charges detected yet. Ingest more history, or lower "
                "the minimum charge count."
            )
            return

        cols = st.columns(4)
        cols[0].metric("Active", summary.get("active_count", 0))
        cols[1].metric("Lapsed", summary.get("lapsed_count", 0))
        cols[2].metric("Per month", f"{summary.get('monthly_total', 0):.2f}")
        cols[3].metric("Per year", f"{summary.get('annual_total', 0):.2f}")

        for increase in summary.get("price_increases") or []:
            st.warning(
                f"{increase['merchant_label']} went from about "
                f"{increase['typical_amount']:.2f} to {increase['last_amount']:.2f} "
                f"({increase['amount_change_pct']:+.1f}%)."
            )

        upcoming = summary.get("upcoming") or []
        if upcoming:
            st.markdown("**Charging soon**")
            st.dataframe(
                pd.DataFrame(upcoming)[
                    ["merchant_label", "cadence", "last_amount", "next_expected", "days_until_next"]
                ],
                use_container_width=True,
            )

        st.markdown("**All detected series**")
        table = pd.DataFrame(series)[
            [
                "merchant_label",
                "category",
                "cadence",
                "occurrences",
                "typical_amount",
                "last_amount",
                "amount_change_pct",
                "monthly_cost",
                "status",
                "confidence",
            ]
        ]
        st.dataframe(table, use_container_width=True)

        monthly = table[table["status"] == "active"]
        if not monthly.empty:
            fig = px.bar(
                monthly.sort_values("monthly_cost", ascending=False),
                x="merchant_label",
                y="monthly_cost",
                title="Monthly cost by subscription",
            )
            st.plotly_chart(fig, use_container_width=True)


def tab_reports() -> None:
    st.subheader("Monthly report")
    st.caption(
        "Every figure is computed from stored transactions. The written summary "
        "is generated from those figures and falls back to a plain summary when "
        "no model is reachable."
    )

    col1, col2 = st.columns([2, 1])
    month = col1.text_input("Report month (YYYY-MM)", _current_month(), key="report_month")
    include_narrative = col2.checkbox("Write a summary", value=True)

    if st.button("Generate report"):
        with st.spinner("Building report..."):
            state = _run_graph(
                "report",
                raw_input={"month": month, "include_narrative": include_narrative},
            )
        _show_errors(state)
        payload = state.get("report_result")
        if not payload:
            st.error("Report generation failed.")
            return
        st.session_state["last_report"] = payload

    payload = st.session_state.get("last_report")
    if not payload:
        return

    report = MonthlyReport(**payload)
    cols = st.columns(4)
    cols[0].metric(
        "Total spend",
        f"{report.total_spent:.2f}",
        delta=f"{report.change_pct:+.1f}%" if report.change_pct is not None else None,
    )
    cols[1].metric("Transactions", report.transaction_count)
    cols[2].metric("Daily average", f"{report.daily_average:.2f}")
    cols[3].metric("Recurring per month", f"{report.recurring.monthly_total:.2f}")

    if report.narrative:
        st.markdown(f"> {report.narrative}")

    if report.highlights:
        st.markdown("**Highlights**")
        for item in report.highlights:
            st.markdown(f"- {item}")

    if report.categories:
        cat_df = pd.DataFrame([line.model_dump() for line in report.categories])
        fig = px.bar(cat_df, x="category", y="amount", title=f"Spend by category, {report.month}")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(cat_df, use_container_width=True)

    if report.budget and report.budget.alerts:
        st.markdown("**Budget alerts**")
        _render_alerts([alert.model_dump() for alert in report.budget.alerts])

    st.download_button(
        "Download report as HTML",
        data=render_report_html(report),
        file_name=f"finance-report-{report.user_id}-{report.month}.html",
        mime="text/html",
        help="Open in a browser and print to PDF for a shareable copy.",
    )


def tab_research() -> None:
    st.subheader("Research assistant")
    query = st.text_input("Ask a finance question")
    audio_file = st.file_uploader("Or upload an audio question (mp3/wav)", type=["mp3", "wav", "m4a"])
    if st.button("Transcribe audio") and audio_file:
        try:
            text = _transcribe_audio(audio_file.read(), audio_file.name)
            st.session_state["research_query_text"] = text
            st.success("Transcribed. You can edit below before searching.")
            query = text
        except Exception as err:
            st.error(f"Transcription failed: {err}")
    if "research_query_text" in st.session_state:
        query = st.text_input("Transcribed/edited question", value=st.session_state["research_query_text"])
    if st.button("Search") and query:
        state = _run_graph("research", raw_input={"query": query})
        st.write(state.get("research_answer"))
        st.write("Sources:")
        st.json(state.get("research_sources"))


def main() -> None:
    st.set_page_config(page_title="Agentic Finance Manager", layout="wide")
    st.title("Agentic Personal Finance Manager")

    with st.sidebar:
        st.header("Session")
        st.text_input("User id", value=DEFAULT_USER_ID, key="user_id")
        st.caption(f"Current month: {_current_month()}")

    tabs = st.tabs(
        [
            "Dashboard",
            "Transactions",
            "Ingest SMS",
            "Ingest Email",
            "Ingest PDF",
            "Budget",
            "Subscriptions",
            "Reports",
            "Research",
        ]
    )
    with tabs[0]:
        tab_dashboard()
    with tabs[1]:
        tab_transactions()
    with tabs[2]:
        tab_ingest_sms()
    with tabs[3]:
        tab_ingest_email()
    with tabs[4]:
        tab_ingest_pdf()
    with tabs[5]:
        tab_budget()
    with tabs[6]:
        tab_subscriptions()
    with tabs[7]:
        tab_reports()
    with tabs[8]:
        tab_research()


if __name__ == "__main__":
    main()


