# Intelligent Bank Reconciliation Website

Python reconciliation app for comparing bank statements with MSD/F&O ledger or existing reconciliation workbooks.

## Run the no-install local website

This version uses Python's built-in web server plus `pandas`/`openpyxl`.

```powershell
& "C:\Users\FazyFlowerFlorita\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" server.py
```

Open `http://localhost:8501`.

## Run the Streamlit version

Install dependencies first:

```powershell
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Matching Colors

- Green: exact match
- Yellow: probable/fuzzy match
- Red: unmatched transaction
- Orange: duplicate entry
- Purple: missing entry, present only in bank or only in ledger

## Supported Input Pattern

The loader auto-detects common HSBC, Citibank, CitiDirect, PDF bank statements, MSD ledger exports, and reconciliation workbook columns:

- Dates: `Date`, `Post date`, `Cleared Date`, `Transaction Date`
- Amounts: `Amount`, `Total`, `Credit amount`, `Debit amount`
- References: `Bank reference`, `Customer Reference`, `Check No.`, `Payment`
- Description: `Narrative`, `Description`, `Payee / Depositor`
