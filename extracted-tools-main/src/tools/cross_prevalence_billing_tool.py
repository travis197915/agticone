from __future__ import annotations

"""
First, run /thynkr-bhagenticai/scripts/load_cross_prevalence_billing_codes.py to save the excel data /Users/upendra_banala@optum.com/Documents/thynkr-bhagenticai/Cross-Billing Prevailing Code List (1).xlsx to DB.
Then, you can use this tool to query the DB for any CPT code pairs, with optional embedded modifiers.
Cross-Prevalence Billing Code lookup tool
(self-bootstrapping, modifier-aware, FAST bulk load, with Revision History capture).

What this does on the FIRST ever call:
 1) Ensures the SQL tables exist.
 2) Parses your Excel quickly:
    * Finds the grid with "CPT Pay / CPT Deny / Modifier" across sheets (fast header scan),
      then re-reads only those columns and bulk-inserts all rows.
    * Reads the SECOND sheet (index 1) as the "revision history" sheet, maps common headers
      (Version/Date/Changed By/Notes), and stores each row (also as raw_json) into
      cross_prevalence_billing_rev_history.
 3) Commits once (very fast). Next calls only query SQL.

On all SUBSEQUENT calls:
 * Reads SQL only and returns the policy for the CPT pair.
 * Supports "compact numeric" inputs (e.g., 0010459 -> CPT 00104 + requires modifier 59).
"""

from typing import Any, Dict, List, Optional, Tuple, Set, Iterable
from pathlib import Path
import re
import string
import json

import pandas as pd
import pymssql
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agents.db_reporting import ensure_tables
from thynkr_bhagenticai.logging_utils import get_logger

logger = get_logger(__name__)

EXCEL_DEFAULT_PATH = (
    "/Users/upendra_banala@optum.com/Documents/"
    "thynkr-bhagenticai/Cross-Billing Prevailing Code List (1).xlsx"
)

# =============================================================================
# Pydantic Schemas
# =============================================================================
class CrossPrevalenceBillingInput(BaseModel):
    """Input schema for the cross-prevalence billing lookup tool."""
    sql_dsn: str = Field(..., description="SQL DSN: server:port;database;user;password")
    cpt_code_a: str = Field(..., description="First CPT code (supports compact numeric)")
    cpt_code_b: str = Field(..., description="Second CPT code (supports compact numeric)")
    excel_path: Optional[str] = Field(
        default=EXCEL_DEFAULT_PATH,
        description="Seed Excel path; used only if table is empty.",
    )


class CrossPrevalenceBillingResult(BaseModel):
    """Structured result for the cross-prevalence billing lookup."""
    ok: bool
    found: bool = False
    cpt_pay: Optional[str] = None
    cpt_deny: Optional[str] = None
    modifier: Optional[str] = None
    message: str = ""
    matches: List[Dict[str, str]] = Field(default_factory=list)
    required_modifiers: List[str] = Field(default_factory=list)
    modifiers_satisfied: Optional[bool] = None
    missing_required_modifiers: List[str] = Field(default_factory=list)
    bootstrapped: bool = False


# =============================================================================
# SQL helpers
# =============================================================================
def _parse_dsn(sql_dsn: str) -> Tuple[str, int, str, str, str]:
    parts = sql_dsn.split(";")
    if len(parts) != 4:
        raise ValueError("SQL_DSN must be 'server:port;database;user;password'")
    server_port, database, user, password = parts
    if ":" in server_port:
        server, port_s = server_port.split(":", 1)
        port = int(port_s)
    else:
        server, port = server_port, 1433
    return server, port, database, user, password


def _get_sql_connection(sql_dsn: str) -> Optional[pymssql.Connection]:
    if not sql_dsn or not str(sql_dsn).strip():
        logger.warning("cross_prev_no_dsn")
        return None
    try:
        server, port, database, user, password = _parse_dsn(sql_dsn)
        return pymssql.connect(
            server=server, port=port, database=database, user=user, password=password,
            timeout=60, login_timeout=30, tds_version="7.3",
        )
    except Exception as e:
        logger.error("cross_prev_sql_connection_failed", extra={"error": str(e)})
        return None


def _table_has_rows(conn: pymssql.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=%s",
        (table_name,)
    )
    exists = cur.fetchone()[0] > 0
    if not exists:
        cur.close()
        return False
    cur.execute(f"SELECT TOP 1 1 FROM {table_name}")
    has_any = cur.fetchone() is not None
    cur.close()
    return has_any


def _ensure_unique_index(conn: pymssql.Connection) -> None:
    """Unique index on (cpt_pay, cpt_deny) for cross_prevalence_billing_codes."""
    cur = conn.cursor()
    cur.execute(
        """
        IF NOT EXISTS (
            SELECT * FROM sys.indexes
            WHERE name='ux_cross_prev_pair'
              AND object_id=OBJECT_ID('cross_prevalence_billing_codes')
        )
        BEGIN
            CREATE UNIQUE INDEX ux_cross_prev_pair
            ON cross_prevalence_billing_codes (cpt_pay, cpt_deny);
        END
        """
    )
    conn.commit()
    cur.close()


HEADER_TOKENS = ("cpt pay", "cpt deny", "modifier")


def _col_idx_to_excel_letter(idx: int) -> str:
    """0-based column index -> Excel letter(s) (A, B, ..., AA, AB, ...)."""
    letters = []
    idx += 1  # convert to 1-based
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters.append(string.ascii_uppercase[rem])
    return "".join(reversed(letters))


def _find_header_and_colmap_preview(df_raw: pd.DataFrame) -> Optional[Tuple[int, Dict[str, int]]]:
    """
    Scan up to first ~250 rows for a header row containing tokens (CPT Pay/CPT Deny/Modifier).
    Return (header_row_idx, {'cpt_pay': col_idx, 'cpt_deny': col_idx, 'modifier': col_idx}) or None.
    """
    max_scan = min(250, len(df_raw))
    for r in range(max_scan):
        token_to_col: Dict[str, int] = {}
        for c in range(df_raw.shape[1]):
            val = df_raw.iat[r, c]
            s = str(val).strip().lower()
            if s in HEADER_TOKENS and s not in token_to_col:
                token_to_col[s] = c
        if all(tok in token_to_col for tok in HEADER_TOKENS):
            return r, {"cpt_pay": token_to_col["cpt pay"],
                       "cpt_deny": token_to_col["cpt deny"],
                       "modifier": token_to_col["modifier"]}
    return None


def _read_crossprev_from_excel_fast(excel_path: str) -> pd.DataFrame:
    """
    Fast loader for the main grid:
    * pd.ExcelFile(..., engine=openpyxl)
    * preview first 250 rows to locate header row and columns
    * re-parse selecting ONLY those columns, skipping rows above header
    * concatenate across any sheets that contain such a grid
    """
    xfile = Path(excel_path).expanduser()
    if not xfile.exists():
        raise FileNotFoundError(f"Excel file not found: {xfile}")

    xls = pd.ExcelFile(xfile, engine="openpyxl")
    frames: List[pd.DataFrame] = []

    for sheet in xls.sheet_names:
        preview = xls.parse(sheet_name=sheet, header=None, nrows=250)
        found = _find_header_and_colmap_preview(preview)
        if not found:
            continue

        header_row, colmap = found
        col_letters = ",".join(_col_idx_to_excel_letter(colmap[k]) for k in ("cpt_pay", "cpt_deny", "modifier"))
        df_part = xls.parse(
            sheet_name=sheet,
            header=None,
            skiprows=header_row + 1,
            usecols=col_letters,
        )
        if df_part.empty or df_part.shape[1] < 3:
            continue

        df_part = df_part.iloc[:, :3].copy()
        df_part.columns = ["cpt_pay", "cpt_deny", "modifier"]
        df_part = df_part.dropna(how="all")
        for c in ["cpt_pay", "cpt_deny", "modifier"]:
            df_part[c] = df_part[c].astype(str).str.strip()
        # Drop "Not found" placeholder rows if present
        df_part = df_part[
            (df_part["cpt_pay"].str.len() > 0)
            & (df_part["cpt_deny"].str.len() > 0)
            & (df_part["modifier"].str.len() > 0)
            & (~df_part["cpt_pay"].str.match(r"(?i)not\s*found"))
            & (~df_part["cpt_deny"].str.match(r"(?i)not\s*found"))
            & (~df_part["modifier"].str.match(r"(?i)not\s*found"))
        ]
        if not df_part.empty:
            frames.append(df_part)

    if not frames:
        raise ValueError(
            "Could not locate a header row with 'CPT Pay', 'CPT Deny', 'Modifier' in any sheet."
        )

    df_all = pd.concat(frames, axis=0, ignore_index=True)
    for c in ["cpt_pay", "cpt_deny", "modifier"]:
        df_all[c] = df_all[c].astype(str).str.strip()
    return df_all[["cpt_pay", "cpt_deny", "modifier"]]


# ==============================================================================
# Revision History - read second sheet & map common headers
# ==============================================================================
def _normalize_header(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


_REV_VERSION_TOKENS = ("version", "rev", "release")
_REV_DATE_TOKENS    = ("date", "effective", "updated", "as of")
_REV_USER_TOKENS    = ("by", "user", "author", "owner", "updated by", "modified by")
_REV_NOTES_TOKENS   = ("change", "notes", "description", "summary", "comment", "remarks", "updates")


def _score_column(name: str, tokens: Tuple[str, ...]) -> int:
    """Rough score: 2 if exact token present, 1 if token as substring."""
    n = name.lower()
    if n in tokens: return 2
    for t in tokens:
        if t in n: return 1
    return 0


def _read_revision_sheet_from_excel(excel_path: str) -> Tuple[pd.DataFrame, str]:
    """
    Reads the SECOND sheet (index 1) as Revision History.
    - Detect a header row: first row with >= 2 non-empty cells.
    - Use that header, read the rest of rows as data.
    - Keep all original columns, then also map to:
      version, revision_date, changed_by, notes
      when possible (heuristic matching).
    Returns (df, sheet_name) -- df includes the original columns (stringified).
    """
    xfile = Path(excel_path).expanduser()
    xls = pd.ExcelFile(xfile, engine="openpyxl")
    if len(xls.sheet_names) < 2:
        # No second sheet -- return empty
        return pd.DataFrame(), ""

    sheet_name = xls.sheet_names[1]  # second sheet by position
    # Read first 200 rows header=None to locate a plausible header row
    preview = xls.parse(sheet_name=sheet_name, header=None, nrows=200)
    header_row = None
    for r in range(len(preview)):
        row = preview.iloc[r]
        non_empty = sum(1 for v in row if str(v).strip() != "")
        if non_empty >= 2:
            header_row = r
            break

    if header_row is None:
        # Fall back: read whole sheet with default header
        df = xls.parse(sheet_name=sheet_name)
    else:
        df = xls.parse(sheet_name=sheet_name, header=header_row)

    if df.empty:
        return pd.DataFrame(), sheet_name

    # Stringify & strip columns/values
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    # Drop rows that are completely blank
    df = df.dropna(how="all")
    # Keep rows with any non-empty cell
    def _nonempty_row(row) -> bool:
        return any(str(v).strip() for v in row.values)
    df = df[df.apply(_nonempty_row, axis=1)]

    return df.reset_index(drop=True), sheet_name


def _map_revision_row(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Map a raw revision row dict to the standard fields:
      version, revision_date, changed_by, notes
    Uses heuristic scoring over column names to pick the best match.
    """
    keys = list(row.keys())
    norm = {k: _normalize_header(k) for k in keys}

    # pick best matching columns by score
    def pick(tokens: Tuple[str, ...]) -> Optional[str]:
        best_k, best_s = None, 0
        for k in keys:
            s = _score_column(norm[k], tokens)
            if s > best_s:
                best_k, best_s = k, s
        return best_k

    k_version = pick(_REV_VERSION_TOKENS)
    k_date    = pick(_REV_DATE_TOKENS)
    k_user    = pick(_REV_USER_TOKENS)
    k_notes   = pick(_REV_NOTES_TOKENS)

    return {
        "version":       (row.get(k_version) if k_version else None),
        "revision_date": (row.get(k_date)    if k_date    else None),
        "changed_by":    (row.get(k_user)    if k_user    else None),
        "notes":         (row.get(k_notes)   if k_notes   else None),
    }


def _save_revision_history(conn: pymssql.Connection, excel_path: str) -> int:
    """
    If the revision history table is empty, read the SECOND sheet and insert rows.
    Returns number of rows inserted.
    """
    if _table_has_rows(conn, "cross_prevalence_billing_rev_history"):
        return 0  # already loaded

    df, sheet_name = _read_revision_sheet_from_excel(excel_path)
    if df.empty:
        logger.info("rev_history_sheet_empty_or_missing")
        return 0

    rows_to_insert: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str, str, int]] = []
    for i, row in df.iterrows():
        as_dict = {str(k): (None if pd.isna(v) else str(v)) for k, v in row.to_dict().items()}
        std = _map_revision_row(as_dict)
        raw_json = json.dumps(as_dict, ensure_ascii=False)
        rows_to_insert.append((
            std.get("version"),
            std.get("revision_date"),
            std.get("changed_by"),
            std.get("notes"),
            raw_json,
            sheet_name or "",
            int(i),
        ))

    cur = conn.cursor()
    conn.autocommit(False)
    cur.executemany(
        """
        INSERT INTO cross_prevalence_billing_rev_history
            (version, revision_date, changed_by, notes, raw_json, source_sheet, row_index)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        rows_to_insert
    )
    conn.commit()
    cur.close()
    return len(rows_to_insert)


# ==============================================================================
# Excel bootstrap (one-time load) - FAST BULK INSERT + REV HISTORY SAVE
# ==============================================================================
def _bootstrap_from_excel(sql_dsn: str, excel_path: str) -> bool:
    """
    One-time bootstrap:
    - ensure_tables(...)
    - if main table is empty, load Excel quickly and bulk insert.
    - if revisions table is empty, also save second sheet as revision history.
    Returns True if a bootstrap (or attempted bootstrap) happened; False otherwise.
    """
    ensure_tables(sql_dsn)

    conn = _get_sql_connection(sql_dsn)
    if not conn:
        logger.warning("bootstrap_skipped_no_connection")
        return False

    try:
        did_any = False

        # MAIN TABLE
        if not _table_has_rows(conn, "cross_prevalence_billing_codes"):
            logger.info("bootstrap_parse_excel_start", extra={"excel_path": excel_path})
            df = _read_crossprev_from_excel_fast(excel_path)
            logger.info("bootstrap_parse_excel_done", extra={"rows": len(df)})

            rows = list(df.itertuples(index=False, name=None))  # (cpt_pay, cpt_deny, modifier)
            cur = conn.cursor()
            _ensure_unique_index(conn)

            conn.autocommit(False)
            # clear (in case the DDL created an empty shell)
            cur.execute("DELETE FROM cross_prevalence_billing_codes")
            # bulk insert
            cur.executemany(
                "INSERT INTO cross_prevalence_billing_codes (cpt_pay, cpt_deny, modifier) VALUES (%s, %s, %s)",
                rows
            )
            conn.commit()
            cur.close()
            logger.info("bootstrap_complete_main", extra={"inserted": len(rows)})
            did_any = True

        # REVISION HISTORY TABLE (SECOND SHEET)
        inserted = _save_revision_history(conn, excel_path)
        if inserted:
            logger.info("bootstrap_complete_revisions", extra={"inserted": inserted})
            did_any = True

        conn.close()
        return did_any

    except Exception as e:
        logger.error("bootstrap_failed", extra={"error": str(e)}, exc_info=True)
        try:
            conn.close()
        except Exception:
            pass
        return False


# =============================================================================
# Matching helpers
# =============================================================================
def _parse_cpt_with_optional_modifier(raw: Any) -> Tuple[str, List[str]]:
    """
    Convert input into (base_cpt, required_modifiers):
    - digits > 5 -> base=first 5, required=[last 2]
    - digits == 5 -> base=the 5-digit code
    - digits < 5 -> zero-pad to 5
    - alphanumeric -> return as-is, no modifier extracted
    """
    s = str(raw).strip()
    if s.isdigit():
        base = s[:5] if len(s) >= 5 else s.zfill(5)
        req = [s[-2:]] if len(s) > 5 else []
        return base, req
    return s, []


def _tokenize_modifier_text(policy_text: Optional[str]) -> Set[str]:
    """Extract two-character tokens that look like CPT modifiers (e.g., '25','59','XE','XP','XU')."""
    if not policy_text:
        return set()
    tokens = set(re.findall(r"[A-Za-z0-9]+", str(policy_text)))
    out: Set[str] = set()
    for t in tokens:
        tu = t.upper()
        if (tu.isdigit() and len(tu) == 2) or (tu.isalpha() and len(tu) == 2):
            out.add(tu)
    return out


# =============================================================================
# Main function
# =============================================================================
def check_cross_prevalence_billing_func(
    *, sql_dsn: str, cpt_code_a: str, cpt_code_b: str, excel_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    First call:
      - Ensures SQL table exists.
      - If empty, loads data once from Excel (path override via 'excel_path').

    Subsequent calls:
      - Reads modifiers directly from SQL.

    Modifier-aware:
      - If inputs embed required modifiers (numeric > 5 digits), only accept rows whose policy permits all required modifiers.
    """
    base_a, req_a = _parse_cpt_with_optional_modifier(cpt_code_a)
    base_b, req_b = _parse_cpt_with_optional_modifier(cpt_code_b)
    required_mods = sorted({m.upper() for m in (req_a + req_b) if m})

    # Bootstrap (one-time) if needed
    excel_to_use = excel_path or EXCEL_DEFAULT_PATH
    bootstrapped = _bootstrap_from_excel(sql_dsn, excel_to_use)

    # Ensure table present even if bootstrap skipped
    ensure_tables(sql_dsn)

    conn = _get_sql_connection(sql_dsn)
    if not conn:
        return CrossPrevalenceBillingResult(
            ok=False,
            message="Could not connect to SQL Server.",
            required_modifiers=required_mods,
            modifiers_satisfied=None,
            bootstrapped=bootstrapped,
        ).model_dump()

    query = """
        SELECT cpt_pay, cpt_deny, modifier
        FROM cross_prevalence_billing_codes
        WHERE (cpt_pay=%s AND cpt_deny=%s) OR (cpt_pay=%s AND cpt_deny=%s)
    """

    cursor: Optional[pymssql.Cursor] = None
    try:
        cursor = conn.cursor()
        cursor.execute(query, (base_a, base_b, base_b, base_a))
        rows = cursor.fetchall()
        raw_matches = [{"cpt_pay": str(r[0]), "cpt_deny": str(r[1]), "modifier": str(r[2])} for r in rows]

        if not rows:
            message = f"No cross-prevalence restriction found for CPT codes {base_a} and {base_b}. Both codes are allowed."
            return CrossPrevalenceBillingResult(
                ok=True, found=False, message=message, matches=raw_matches,
                required_modifiers=required_mods,
                modifiers_satisfied=None if not required_mods else False,
                missing_required_modifiers=required_mods if required_mods else [],
                bootstrapped=bootstrapped,
            ).model_dump()

        acceptable: List[Tuple[str, str, str, Set[str]]] = []
        if required_mods:
            for pay, deny, policy in rows:
                policy_text = str(policy)
                tokens = _tokenize_modifier_text(policy_text)
                if "not allowed" in policy_text.lower():
                    continue
                if all(m in tokens for m in required_mods):
                    acceptable.append((str(pay), str(deny), policy_text, tokens))
        else:
            for pay, deny, policy in rows:
                acceptable.append((str(pay), str(deny), str(policy), _tokenize_modifier_text(str(policy))))

        if acceptable:
            pay, deny, policy_text, _ = acceptable[0]
            return CrossPrevalenceBillingResult(
                ok=True, found=True, cpt_pay=pay, cpt_deny=deny,
                modifier=policy_text, message=policy_text, matches=raw_matches,
                required_modifiers=required_mods, modifiers_satisfied=True if required_mods else None,
                missing_required_modifiers=[], bootstrapped=bootstrapped,
            ).model_dump()

        # Rows exist but do not satisfy required modifiers
        if required_mods:
            missing_any: Set[str] = set()
            for _, _, policy in rows:
                tokens = _tokenize_modifier_text(str(policy))
                missing_any |= {m for m in required_mods if m not in tokens}
            return CrossPrevalenceBillingResult(
                ok=True, found=False,
                message=("Cross-prevalence rule(s) exist for the CPT pair, but the required "
                         f"modifier(s) {required_mods} are not permitted by the policy text."),
                matches=raw_matches, required_modifiers=required_mods,
                modifiers_satisfied=False, missing_required_modifiers=sorted(missing_any),
                bootstrapped=bootstrapped,
            ).model_dump()

        # Fallback: return first raw match
        first = raw_matches[0]
        return CrossPrevalenceBillingResult(
            ok=True, found=True,
            cpt_pay=first.get("cpt_pay"), cpt_deny=first.get("cpt_deny"),
            modifier=first.get("modifier"), message=first.get("modifier", ""),
            matches=raw_matches, required_modifiers=required_mods,
            modifiers_satisfied=None, missing_required_modifiers=[],
            bootstrapped=bootstrapped,
        ).model_dump()

    except Exception as e:
        logger.error("cross_prev_lookup_failed",
                     extra={"error": str(e), "cpt_code_a": base_a, "cpt_code_b": base_b},
                     exc_info=True)
        return CrossPrevalenceBillingResult(
            ok=False, message=f"Error during cross-prevalence lookup: {e}",
            required_modifiers=required_mods, modifiers_satisfied=None,
            bootstrapped=bootstrapped,
        ).model_dump()
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# Structured Tool (LangGraph)
check_cross_prevalence_billing = StructuredTool.from_function(
    func=check_cross_prevalence_billing_func,
    name="check_cross_prevalence_billing",
    description=(
        "Look up cross-prevalence billing restrictions for two CPT codes. "
        "First call auto-loads the SQL table from the Excel if empty (one-time bootstrap), "
        "and also saves the second sheet as revision history. "
        "Subsequent calls read from SQL only. Supports compact numeric inputs where "
        ">5 digits mean: first 5 = CPT, last 2 = required modifier (e.g., '0010459')."
    ),
    args_schema=CrossPrevalenceBillingInput,
)
