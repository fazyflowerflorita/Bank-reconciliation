import pandas as pd
import streamlit as st

from reconciliation_engine import MatchConfig, STATUS_COLORS, reconcile, standardize_transactions, to_excel_bytes


st.set_page_config(page_title="Intelligent Bank Reconciliation", layout="wide")

st.title("Intelligent Bank Reconciliation")
st.caption("Upload bank and MSD/F&O ledger files to classify exact matches, fuzzy matches, duplicates, and missing entries.")

with st.sidebar:
    st.header("Matching Rules")
    date_tolerance = st.slider("Date tolerance (+/- days)", 0, 10, 3)
    amount_tolerance = st.number_input("Amount tolerance", min_value=0.0, value=0.05, step=0.01)
    fuzzy_threshold = st.slider("Fuzzy narration/reference threshold", 50, 100, 72)
    st.divider()
    st.write("Green: exact")
    st.write("Yellow: fuzzy")
    st.write("Orange: duplicate")
    st.write("Purple: missing")

bank_file = st.file_uploader("Bank statement workbook or PDF", type=["xlsx", "pdf"], key="bank")
ledger_file = st.file_uploader("MSD/F&O ledger or reconciliation workbook/PDF", type=["xlsx", "pdf"], key="ledger")

if bank_file and ledger_file:
    bank = standardize_transactions(bank_file, "Bank", bank_file.name)
    ledger = standardize_transactions(ledger_file, "Ledger", ledger_file.name)
    result, summary = reconcile(
        bank,
        ledger,
        MatchConfig(date_tolerance_days=date_tolerance, amount_tolerance=amount_tolerance, fuzzy_threshold=fuzzy_threshold),
    )

    cols = st.columns(5)
    cols[0].metric("Total", summary.get("total", 0))
    cols[1].metric("Exact", summary.get("Exact Match", 0))
    cols[2].metric("Fuzzy", summary.get("Fuzzy Match", 0))
    cols[3].metric("Duplicates", summary.get("Duplicate Entry", 0))
    cols[4].metric("Exceptions", summary.get("exceptions", 0))

    def highlight(row):
        color = STATUS_COLORS.get(row["status"], "#fff")
        return [f"background-color: {color}"] * len(row)

    st.subheader("Reconciliation Result")
    st.dataframe(result.style.apply(highlight, axis=1), use_container_width=True, height=520)

    xlsx = to_excel_bytes(result, summary)
    st.download_button(
        "Download color-coded Excel report",
        data=xlsx,
        file_name="reconciliation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    st.info("Upload one bank statement and one ledger/reconciliation workbook to begin.")
