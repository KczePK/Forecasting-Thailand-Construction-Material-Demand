"""
Clean Bank of Thailand BOTWEBSTAT Excel exports into a tidy long table.

BOT encodes the indicator hierarchy with leading spaces. Flattening it loses information:
the same label ("Municipal area", "Total investment", "No. of factories") appears under
several parents. This module rebuilds the full path for every row, so each series has a
unique key and no hard-coded row numbers are needed.

Usage
-----
    from bot_clean import clean_bot, to_wide
    long = clean_bot("data/EC_EI_009_S2.xlsx")
    wide = to_wide(long, ["Property Credit Outstanding (Millions of Baht)"])
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

SEP = " > "
_FOOTNOTE = re.compile(r"(\s+\d+/)+\s*$")  # trailing "1/", "3/ 4/" footnote markers
_PERIOD = re.compile(r"^([A-Za-z]{3}\s+\d{4})\s*([pr]?)\s*$")  # "FEB 2018 r", "NOV 2025 p"


def _indent_level(raw: str) -> int:
    """BOT uses 3-4 spaces per level and is not consistent, so bucket by width."""
    n = len(raw) - len(raw.lstrip(" "))
    return 0 if n == 0 else (n - 1) // 4 + 1


def _clean_label(raw: str) -> str:
    return _FOOTNOTE.sub("", raw.strip()).rstrip(" :").strip()


def _find_header_row(raw: pd.DataFrame) -> int:
    """The header row is the first one containing month labels such as 'DEC 2024'."""
    for i in range(min(len(raw), 20)):
        hits = sum(bool(_PERIOD.match(str(v))) for v in raw.iloc[i].tolist())
        if hits >= 3:
            return i
    raise ValueError("No row with month headers found; is this a BOTWEBSTAT export?")


def clean_bot(path: str | Path) -> pd.DataFrame:
    """
    Read a BOT export (.xlsx, or the same sheet saved as .csv; local path or URL) and
    return one row per (series, month) with columns:
      Dataset, Path, Level0..LevelN, Indicator, Depth, Date, Value, Status
    Status is 'p' (preliminary), 'r' (revised) or '' as flagged in the BOT header.
    """
    path_str = str(path)
    if path_str.lower().endswith(".csv"):
        raw = pd.read_csv(path_str, header=None, dtype=object, keep_default_na=False, na_values=[""])
    else:
        raw = pd.read_excel(path_str, header=None, dtype=object)
    path = Path(path_str.split("?")[0])
    codes = [m.group(0) for v in raw.iloc[:5, 0].dropna().astype(str) if (m := re.match(r"EC_\w+", v))]
    dataset = codes[0] if codes else path.stem

    h = _find_header_row(raw)
    header = raw.iloc[h].tolist()
    period_cols = {
        j: _PERIOD.match(str(v)) for j, v in enumerate(header) if _PERIOD.match(str(v))
    }
    label_col = min(period_cols) - 1

    records = []
    stack: list[tuple[int, str]] = []  # (effective level, label)
    last_header: tuple[int, int] | None = None  # (raw level, effective level)

    for i in range(h + 1, len(raw)):
        cell = raw.iat[i, label_col]
        if pd.isna(cell) or not str(cell).strip():
            continue
        text = str(cell)
        values = pd.to_numeric(pd.Series([raw.iat[i, j] for j in period_cols]), errors="coerce")
        has_values = values.notna().any()
        lvl = _indent_level(text)

        # Some sheets (e.g. the price-index file) put children at the same indent as a
        # value-less section header. Treat those as one level deeper than the header.
        if has_values and last_header is not None and lvl <= last_header[0]:
            eff = last_header[1] + 1
        else:
            eff = lvl
        if not has_values:
            last_header = (lvl, eff)
        elif last_header is not None and lvl < last_header[0]:
            last_header = None

        stack = [s for s in stack if s[0] < eff]
        stack.append((eff, _clean_label(text)))

        if not has_values:
            continue
        labels = [s[1] for s in stack]
        for (j, m), v in zip(period_cols.items(), values):
            records.append({
                "Dataset": dataset,
                "Path": SEP.join(labels),
                **{f"Level{k}": lab for k, lab in enumerate(labels)},
                "Indicator": labels[-1],
                "Depth": len(labels) - 1,
                "Date": pd.to_datetime(m.group(1).title(), format="%b %Y"),
                "Value": v,
                "Status": m.group(2),
            })

    out = pd.DataFrame(records)
    # BOT sometimes repeats a parent row with an identical series (e.g. Land Development
    # Licences appears twice: once broken down by type, once by region). Keep one.
    out = out.drop_duplicates(subset=["Path", "Date"], keep="first")
    level_cols = sorted(c for c in out.columns if c.startswith("Level"))
    out[level_cols] = out[level_cols].fillna("")
    return out.sort_values(["Path", "Date"]).reset_index(drop=True)


def to_wide(long: pd.DataFrame, paths: list[str] | None = None) -> pd.DataFrame:
    """Pivot to one column per Path, monthly index. Optionally select paths."""
    df = long if paths is None else long[long["Path"].isin(paths)]
    if paths is not None:
        missing = set(paths) - set(df["Path"])
        if missing:
            raise KeyError(f"Paths not found: {sorted(missing)}")
    wide = df.pivot(index="Date", columns="Path", values="Value").sort_index().asfreq("MS")
    wide.columns.name = None
    return wide

