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
from finance_manager.graph import FinanceGraphRunner, get_graph_runner


settings = get_settings()
runner: FinanceGraphRunner = get_graph_runner()
openai_client: Optional[OpenAI] = None
hf_client: Optional[InferenceClient] = None


def _run_graph(task_type: str, **kwargs: Any) -> Dict[str, Any]:
    return asyncio.run(runner.arun(task_type=task_type, **kwargs))


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

        # Budget progress (uses local session budget if present)
        budget = st.session_state.get("budget")
        if budget and category_dist:
            total_spend = sum(category_dist.values())
            total_limit = budget.get("total_limit", 0) or 0
            st.metric("Total spend vs limit", f"{total_spend:.2f} / {total_limit:.2f}")
            progress = min(total_spend / total_limit, 1.0) if total_limit else 0.0
            st.progress(progress)

        st.json(analytics)


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


def tab_budget() -> None:
    st.subheader("Budget")
    month = st.text_input("Month (YYYY-MM)", datetime.utcnow().strftime("%Y-%m"))
    total = st.number_input("Total limit", min_value=0.0, value=1000.0, step=50.0)
    category_limits = st.text_area("Per-category limits (cat:amount per line)", "Food:300\nTransport:150")
    if st.button("Save budget"):
        per_cat = {}
        for line in category_limits.splitlines():
            if ":" in line:
                cat, amt = line.split(":", 1)
                try:
                    per_cat[cat.strip()] = float(amt.strip())
                except ValueError:
                    continue
        budget = {"user_id": "demo-user", "month": month, "total_limit": total, "per_category_limits": per_cat}
        st.session_state["budget"] = budget
        st.info("Budget saved in local session (API persistence available).")


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
    tabs = st.tabs(["Dashboard", "Transactions", "Ingest SMS", "Ingest PDF", "Budget", "Research"])
    with tabs[0]:
        tab_dashboard()
    with tabs[1]:
        tab_transactions()
    with tabs[2]:
        tab_ingest_sms()
    with tabs[3]:
        tab_ingest_pdf()
    with tabs[4]:
        tab_budget()
    with tabs[5]:
        tab_research()


if __name__ == "__main__":
    main()


