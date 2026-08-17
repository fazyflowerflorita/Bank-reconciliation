from __future__ import annotations

import io
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pandas as pd

try:
    from rapidfuzz import fuzz

    def similarity(left: str, right: str) -> float:
        return float(fuzz.token_set_ratio(left, right))

except Exception:
    from difflib import SequenceMatcher

    def similarity(left: str, right: str) -> float:
        return SequenceMatcher(None, left, right).ratio() * 100


STATUS_COLORS = {
    "Exact Match": "#b7f7c4",
    "Fuzzy Match": "#fff3a3",
    "Unmatched Transaction": "#ffc2c2",
    "Duplicate Entry": "#ffd49a",
    "Missing Entry": "#dfc7ff",
}


DATE_KEYWORDS = ("date", "post date", "cleared")
AMOUNT_KEYWORDS = ("amount", "total", "debit", "credit")
REF_KEYWORDS = ("reference", "ref", "check", "payment", "trace", "journal", "jrnl", "voucher")
DESC_KEYWORDS = ("narrative", "description", "payee", "depositor", "master name", "name")


@dataclass
class MatchConfig:
    date_tolerance_days: int = 3
    amount_tolerance: float = 0.05
    fuzzy_threshold: int = 72


def read_excel_any(source: str | Path | BinaryIO | bytes, filename: str = "uploaded.xlsx") -> pd.DataFrame:
    data = _read_bytes(source)
    try:
        return _read_workbook(data)
    except Exception:
        repaired = _repair_xlsx_styles(data)
        return _read_workbook(repaired)


def standardize_transactions(source: str | Path | BinaryIO | bytes, dataset_name: str, filename: str = "") -> pd.DataFrame:
    inferred_name = filename or (Path(source).name if isinstance(source, (str, Path)) else "")
    if inferred_name.lower().endswith(".pdf"):
        return _parse_pdf_statement(source, dataset_name)

    raw = read_excel_any(source, filename)
    if raw.empty:
        return _empty_standard(dataset_name)
    key_value = _parse_key_value_statement(raw, dataset_name)
    if not key_value.empty:
        return key_value

    header_idx = _detect_header_row(raw)
    headers = raw.iloc[header_idx].fillna("").astype(str).map(_clean_header).tolist()
    body = raw.iloc[header_idx + 1 :].copy()
    body.columns = _dedupe_headers(headers)
    body = body.dropna(how="all")

    date_col = _pick_date_column(body)
    amount_col = _pick_amount_column(body)
    ref_col = _pick_column(body.columns, REF_KEYWORDS)
    desc_col = _pick_text_column(body, DESC_KEYWORDS)

    out = pd.DataFrame(index=body.index)
    out["source"] = dataset_name
    out["row_no"] = body.index.astype(int) + 1
    out["date"] = pd.to_datetime(body[date_col], errors="coerce", dayfirst=True) if date_col else pd.NaT
    out["amount"] = _amount_series(body, amount_col)
    out["reference"] = body[ref_col].map(_to_text) if ref_col else ""
    out["description"] = body[desc_col].map(_to_text) if desc_col else ""
    out["original_date"] = body[date_col].map(_to_text) if date_col else ""
    out["original_amount"] = body[amount_col].map(_to_text) if isinstance(amount_col, str) else out["amount"].map(str)

    out["amount"] = pd.to_numeric(out["amount"], errors="coerce").round(2)
    out["norm_ref"] = out["reference"].map(_normalize_ref)
    out["norm_desc"] = out["description"].map(_normalize_text)
    out["transaction_key"] = (
        out["date"].dt.strftime("%Y-%m-%d").fillna("")
        + "|"
        + out["amount"].fillna(0).map(lambda x: f"{x:.2f}")
        + "|"
        + out["norm_ref"]
    )

    mask = out["date"].notna() & out["amount"].notna()
    mask &= ~out["description"].str.contains("bank balance", case=False, na=False)
    standardized = out.loc[mask].reset_index(drop=True)
    if standardized.empty:
        key_value = _parse_key_value_statement(raw, dataset_name)
        if not key_value.empty:
            return key_value
    return standardized


def reconcile(bank: pd.DataFrame, ledger: pd.DataFrame, config: MatchConfig | None = None) -> tuple[pd.DataFrame, dict]:
    config = config or MatchConfig()
    bank = bank.copy().reset_index(drop=True)
    ledger = ledger.copy().reset_index(drop=True)
    if not bank.empty and not ledger.empty and bank["date"].notna().any():
        start = bank["date"].min() - pd.Timedelta(days=config.date_tolerance_days)
        end = bank["date"].max() + pd.Timedelta(days=config.date_tolerance_days)
        ledger = ledger[(ledger["date"] >= start) & (ledger["date"] <= end)].reset_index(drop=True)
    bank["bank_id"] = range(len(bank))
    ledger["ledger_id"] = range(len(ledger))

    used_bank: set[int] = set()
    used_ledger: set[int] = set()
    rows = []

    bank_dupes = _duplicate_ids(bank, "bank_id")
    ledger_dupes = _duplicate_ids(ledger, "ledger_id")

    for _, b in bank.iterrows():
        if b.bank_id in used_bank:
            continue
        candidates = ledger[
            (~ledger["ledger_id"].isin(used_ledger))
            & (ledger["date"] == b.date)
            & ((ledger["amount"] - b.amount).abs() <= 0.004)
        ]
        exact = None
        if len(candidates) == 1:
            exact = candidates.iloc[0]
        for _, l in candidates.iterrows():
            if exact is not None:
                break
            refs_match = bool(b.norm_ref and l.norm_ref and b.norm_ref == l.norm_ref)
            desc_match = bool(b.norm_desc and l.norm_desc and b.norm_desc == l.norm_desc)
            if refs_match or desc_match or not b.norm_ref or not l.norm_ref:
                exact = l
                break
        if exact is not None:
            used_bank.add(int(b.bank_id))
            used_ledger.add(int(exact.ledger_id))
            rows.append(_result_row(b, exact, "Exact Match", 100, "Date and amount matched; reference/description compatible."))

    for _, b in bank.iterrows():
        if b.bank_id in used_bank:
            continue
        pool = ledger[
            (~ledger["ledger_id"].isin(used_ledger))
            & ((ledger["amount"] - b.amount).abs() <= config.amount_tolerance)
            & ((ledger["date"] - b.date).abs().dt.days <= config.date_tolerance_days)
        ]
        best = None
        best_score = -1.0
        for _, l in pool.iterrows():
            score = max(similarity(b.norm_desc, l.norm_desc), similarity(b.norm_ref, l.norm_ref))
            if score > best_score:
                best = l
                best_score = score
        if best is not None and best_score >= config.fuzzy_threshold:
            used_bank.add(int(b.bank_id))
            used_ledger.add(int(best.ledger_id))
            rows.append(_result_row(b, best, "Fuzzy Match", round(best_score, 1), "Within tolerance; narration/reference is similar."))

    for _, b in bank.iterrows():
        if int(b.bank_id) not in used_bank:
            status = "Duplicate Entry" if int(b.bank_id) in bank_dupes else "Missing Entry"
            note = "Potential duplicate in bank file." if status == "Duplicate Entry" else "Exists in bank but not in ledger/MSD."
            rows.append(_result_row(b, None, status, 0, note))

    for _, l in ledger.iterrows():
        if int(l.ledger_id) not in used_ledger:
            status = "Duplicate Entry" if int(l.ledger_id) in ledger_dupes else "Missing Entry"
            note = "Potential duplicate in ledger/MSD file." if status == "Duplicate Entry" else "Exists in ledger/MSD but not in bank."
            rows.append(_result_row(None, l, status, 0, note))

    result = pd.DataFrame(rows)
    if result.empty:
        return result, {"total": 0}
    order = ["Exact Match", "Fuzzy Match", "Duplicate Entry", "Missing Entry", "Unmatched Transaction"]
    result["status_order"] = result["status"].map({v: i for i, v in enumerate(order)}).fillna(99)
    result = result.sort_values(["status_order", "bank_date", "ledger_date"]).drop(columns=["status_order"]).reset_index(drop=True)
    summary = result["status"].value_counts().to_dict()
    summary["total"] = int(len(result))
    summary["matched"] = int(summary.get("Exact Match", 0) + summary.get("Fuzzy Match", 0))
    summary["exceptions"] = int(len(result) - summary["matched"])
    return result, summary


def to_excel_bytes(result: pd.DataFrame, summary: dict) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)
        result.to_excel(writer, sheet_name="Reconciliation", index=False)
        workbook = writer.book
        sheet = writer.sheets["Reconciliation"]
        formats = {status: workbook.add_format({"bg_color": color}) for status, color in STATUS_COLORS.items()}
        status_col = result.columns.get_loc("status")
        for row_idx, status in enumerate(result["status"], start=1):
            sheet.set_row(row_idx, None, formats.get(status))
        sheet.autofilter(0, 0, len(result), len(result.columns) - 1)
        sheet.freeze_panes(1, 0)
        sheet.set_column(0, len(result.columns) - 1, 18)
        sheet.set_column(status_col, status_col, 24)
    return output.getvalue()


def _read_bytes(source) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    position = source.tell() if hasattr(source, "tell") else None
    data = source.read()
    if position is not None and hasattr(source, "seek"):
        source.seek(position)
    return data


def _read_workbook(data: bytes) -> pd.DataFrame:
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    frames = pd.read_excel(tmp_path, sheet_name=None, header=None, engine="openpyxl")
    useful = [frame.dropna(how="all") for frame in frames.values()]
    useful = [frame for frame in useful if not frame.empty]
    return max(useful, key=len) if useful else pd.DataFrame()


def _repair_xlsx_styles(data: bytes) -> bytes:
    source = io.BytesIO(data)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "xl/styles.xml":
                continue
            zout.writestr(item, zin.read(item.filename))
    return target.getvalue()


def _detect_header_row(raw: pd.DataFrame) -> int:
    best_idx, best_score = 0, -1
    keywords = DATE_KEYWORDS + AMOUNT_KEYWORDS + REF_KEYWORDS + DESC_KEYWORDS
    exact_headers = {
        "date",
        "debit",
        "credit",
        "amount",
        "description",
        "voucher",
        "journal number",
        "jrnl no.",
        "orig. audit trail",
        "orig. master name",
        "payee / depositor",
    }
    for idx, row in raw.head(25).iterrows():
        values = [_to_text(value).lower() for value in row.tolist()]
        text = " ".join(values)
        score = sum(1 for word in keywords if word in text)
        score += sum(4 for value in values if value in exact_headers)
        if score > best_score:
            best_idx, best_score = int(idx), score
    return best_idx


def _pick_column(columns, keywords) -> str | None:
    scored = []
    for col in columns:
        name = str(col).lower()
        score = sum(1 for key in keywords if key in name)
        if score:
            scored.append((score, len(name), col))
    return sorted(scored, reverse=True)[0][2] if scored else None


def _pick_date_column(df: pd.DataFrame) -> str | None:
    candidates = []
    for col in df.columns:
        name = str(col).lower()
        if not any(key in name for key in DATE_KEYWORDS):
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
        count = parsed.notna().sum()
        header_score = 3 if name == "date" else 1
        if count:
            candidates.append((count, header_score, -len(name), col))
    if candidates:
        return sorted(candidates, reverse=True)[0][3]
    return _pick_column(df.columns, DATE_KEYWORDS)


def _pick_text_column(df: pd.DataFrame, keywords) -> str | None:
    candidates = []
    priorities = {"narrative": 7, "description": 6, "payee": 5, "depositor": 5, "master name": 4}
    for col in df.columns:
        name = str(col).lower()
        header_score = sum(1 for key in keywords if key in name)
        if not header_score:
            continue
        priority = max((score for key, score in priorities.items() if key in name), default=1)
        text_count = df[col].map(_to_text).map(bool).sum()
        candidates.append((priority * 10 + header_score * 4 + text_count, text_count, -len(name), col))
    if candidates:
        return sorted(candidates, reverse=True)[0][3]
    return _pick_column(df.columns, keywords)


def _pick_amount_column(df: pd.DataFrame):
    credit = _pick_column(df.columns, ("credit amount", "credit"))
    debit = _pick_column(df.columns, ("debit amount", "debit"))
    if credit and debit:
        return (credit, debit)
    candidates = []
    for col in df.columns:
        name = str(col).lower()
        if "date" in name or "bank rec" in name:
            continue
        values = df[col].map(_parse_amount_safe)
        numeric_count = values.notna().sum()
        date_like_count = 0
        header_score = sum(4 for key in ("amount", "total", "debit", "credit") if key in name)
        if numeric_count:
            candidates.append((header_score + numeric_count - date_like_count, header_score, numeric_count, col))
    if candidates:
        return sorted(candidates, reverse=True)[0][3]
    return _pick_column(df.columns, ("amount", "total", "total aud", "total "))


def _amount_series(df: pd.DataFrame, amount_col):
    if isinstance(amount_col, tuple):
        debit_col, credit_col = _classify_debit_credit(amount_col)
        debit = df[debit_col].map(_parse_amount_safe).fillna(0)
        credit = df[credit_col].map(_parse_amount_safe).fillna(0)
        signed_debit = debit.map(lambda value: value if value < 0 else -value)
        signed_credit = credit.map(lambda value: abs(value) if value != 0 else 0)
        return signed_credit + signed_debit
    if amount_col:
        return df[amount_col].map(_parse_amount)
    return pd.Series([None] * len(df), index=df.index)


def _classify_debit_credit(cols: tuple[str, str]) -> tuple[str, str]:
    first, second = cols
    first_name = str(first).lower()
    second_name = str(second).lower()
    if "debit" in first_name or "credit" in second_name:
        return first, second
    if "debit" in second_name or "credit" in first_name:
        return second, first
    return first, second


def _parse_amount(value):
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if re.search(r"[A-Za-z]", text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text:
        return None
    number = float(text)
    return -abs(number) if negative else number


def _parse_amount_safe(value):
    try:
        return _parse_amount(value)
    except Exception:
        return None


def _result_row(bank_row, ledger_row, status, score, note):
    def value(row, field):
        return "" if row is None else row[field]

    return {
        "status": status,
        "score": score,
        "note": note,
        "bank_date": value(bank_row, "date"),
        "bank_amount": value(bank_row, "amount"),
        "bank_reference": value(bank_row, "reference"),
        "bank_description": value(bank_row, "description"),
        "bank_row": value(bank_row, "row_no"),
        "ledger_date": value(ledger_row, "date"),
        "ledger_amount": value(ledger_row, "amount"),
        "ledger_reference": value(ledger_row, "reference"),
        "ledger_description": value(ledger_row, "description"),
        "ledger_row": value(ledger_row, "row_no"),
    }


def _duplicate_ids(df: pd.DataFrame, id_col: str) -> set[int]:
    if df.empty:
        return set()
    cols = ["date", "amount", "norm_ref", "norm_desc"]
    duped = df.duplicated(cols, keep=False)
    return set(df.loc[duped, id_col].astype(int).tolist())


def _parse_key_value_statement(raw: pd.DataFrame, dataset_name: str) -> pd.DataFrame:
    if raw.shape[1] < 2:
        return _empty_standard(dataset_name)
    labels = raw.iloc[:, 0].map(_to_text)
    if labels.eq("Transaction Amount").sum() < 2 or labels.eq("Bank Reference").sum() < 2:
        return _empty_standard(dataset_name)

    transactions = []
    current = {}
    start_row = None
    for idx, row in raw.iterrows():
        label = _to_text(row.iloc[0])
        value = row.iloc[1] if len(row) > 1 else None
        if label == "Bank Reference":
            if current:
                transactions.append((start_row, current))
            current = {"bank_reference": _to_text(value)}
            start_row = int(idx) + 1
        elif current and label in {
            "Customer Reference",
            "Value Date",
            "Entry Date",
            "Transaction Amount",
            "Product Type",
            "Transaction Description",
            "Extra Information",
        }:
            current[label] = value
    if current:
        transactions.append((start_row, current))

    rows = []
    for row_no, tx in transactions:
        amount = _parse_amount_safe(tx.get("Transaction Amount"))
        date_value = tx.get("Entry Date") or tx.get("Value Date")
        if amount is None or not date_value:
            continue
        desc = " ".join(
            _to_text(tx.get(key))
            for key in ("Transaction Description", "Extra Information", "Product Type")
            if _to_text(tx.get(key))
        )
        ref = tx.get("Customer Reference") or tx.get("bank_reference")
        rows.append(
            {
                "source": dataset_name,
                "row_no": row_no,
                "date": pd.to_datetime(date_value, errors="coerce", dayfirst=False),
                "amount": round(float(amount), 2),
                "reference": _to_text(ref),
                "description": desc,
                "original_date": _to_text(date_value),
                "original_amount": _to_text(tx.get("Transaction Amount")),
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return _empty_standard(dataset_name)
    out = out[out["date"].notna()].copy()
    out["norm_ref"] = out["reference"].map(_normalize_ref)
    out["norm_desc"] = out["description"].map(_normalize_text)
    out["transaction_key"] = (
        out["date"].dt.strftime("%Y-%m-%d").fillna("")
        + "|"
        + out["amount"].fillna(0).map(lambda x: f"{x:.2f}")
        + "|"
        + out["norm_ref"]
    )
    return out.reset_index(drop=True)


def _parse_pdf_statement(source: str | Path | BinaryIO | bytes, dataset_name: str) -> pd.DataFrame:
    try:
        import pdfplumber
    except Exception as exc:
        raise RuntimeError("PDF upload requires pdfplumber. Install dependencies from requirements.txt.") from exc

    data = _read_bytes(source)
    rows = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                parsed = _parse_bank_pdf_line(line)
                if parsed:
                    rows.append(parsed)

    out = pd.DataFrame(rows)
    if out.empty:
        return _empty_standard(dataset_name)
    out.insert(0, "source", dataset_name)
    out["row_no"] = range(1, len(out) + 1)
    out["date"] = pd.to_datetime(out["date"], errors="coerce", yearfirst=False)
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce").round(2)
    out["original_date"] = out["date"].dt.strftime("%m/%d/%Y")
    out["original_amount"] = out["amount"].map(lambda value: f"{value:.2f}" if pd.notna(value) else "")
    out["norm_ref"] = out["reference"].map(_normalize_ref)
    out["norm_desc"] = out["description"].map(_normalize_text)
    out["transaction_key"] = (
        out["date"].dt.strftime("%Y-%m-%d").fillna("")
        + "|"
        + out["amount"].fillna(0).map(lambda x: f"{x:.2f}")
        + "|"
        + out["norm_ref"]
    )
    return out[out["date"].notna() & out["amount"].notna()].reset_index(drop=True)


def _parse_bank_pdf_line(line: str) -> dict | None:
    text = re.sub(r"\s+", " ", line.strip())
    if not re.match(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+", text):
        return None

    matches = list(re.finditer(r"-?\$?\d[\d,]*\.\d{2}", text))
    if not matches:
        return None

    date_token = text.split(" ", 1)[0]
    if date_token.count("/") == 1:
        date_token = f"{date_token}/2026"
    amount_match = matches[-2] if len(matches) >= 2 else matches[-1]
    amount_text = amount_match.group(0)
    description = text[text.find(" ") + 1 : amount_match.start()].strip()
    if not description or "balance" in description.lower():
        return None

    amount = _parse_amount_safe(amount_text)
    if amount is None:
        return None
    if _is_pdf_debit(description):
        amount = -abs(amount)
    elif _is_pdf_credit(description):
        amount = abs(amount)

    ref_match = re.search(r"(?:CHECK NO:|REFERENCE #|REF(?:ERENCE)? #?)\s*([A-Z0-9-]+)", description, re.I)
    reference = ref_match.group(1) if ref_match else ""
    return {"date": date_token, "amount": amount, "reference": reference, "description": description}


def _is_pdf_debit(description: str) -> bool:
    text = description.upper()
    return any(
        key in text
        for key in (
            "CHECK NO",
            "ACH DEBIT",
            "TRANSFER DEBIT",
            "PREAUTHORIZED TRANSFER",
            "SERVICE CHARGE",
            "DEBIT",
            "DRAWDOWN",
        )
    )


def _is_pdf_credit(description: str) -> bool:
    text = description.upper()
    return any(key in text for key in ("DEPOSIT", "ELECTRONIC CREDIT", "TRANSFER CREDIT", "CREDIT"))


def _empty_standard(dataset_name: str) -> pd.DataFrame:
    return pd.DataFrame(columns=["source", "row_no", "date", "amount", "reference", "description", "norm_ref", "norm_desc"])


def _clean_header(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value).strip())
    return value if value and value.lower() != "nan" else "blank"


def _dedupe_headers(headers):
    seen = {}
    result = []
    for header in headers:
        count = seen.get(header, 0)
        seen[header] = count + 1
        result.append(header if count == 0 else f"{header}_{count + 1}")
    return result


def _to_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", _to_text(value).upper())).strip()


def _normalize_ref(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _to_text(value).upper())
