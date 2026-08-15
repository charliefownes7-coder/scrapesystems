"""
Google Sheets read/write.

Assumes you already have a service account JSON key set up (the same
one your existing scraper uses to write to Sheets) and that it has
edit access to the target spreadsheet.
"""

import gspread
import pandas as pd

SERVICE_ACCOUNT_FILE = "service_account.json"
SHEET_NAME = "Sterling Digital Leads"
WORKSHEET_NAME = "Sheet1"  # TODO: confirm this matches your actual tab name

# Matches your real sheet's column headers, in order (kept exactly as
# they already appear in the sheet, typos and all, so nothing breaks).
#
# "Move to Cold Call" is new: a manual override so a lead that has a
# Facebook page can still be routed to Cold Call instead of DM (e.g.
# an abandoned/inactive page not worth DMing). It doesn't exist in
# your actual sheet yet - load_leads() below adds it in-memory as a
# blank column, and it gets written to the real sheet automatically
# the next time anything saves.
REQUIRED_COLUMNS = [
    "Name", "Category", "Adress", "Phone",
    "Call status", "Call notes", "Text Phone Number", "Email",
    "Rating", "Reveiw count", "Has website", "Has facebook", "Facebook URL",
    "Move to Cold Call",
    "Maps URL", "Search query",
]


def _get_worksheet():
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open(SHEET_NAME)
    return sh.worksheet(WORKSHEET_NAME)


def load_leads() -> pd.DataFrame:
    ws = _get_worksheet()
    records = ws.get_all_records()
    df = pd.DataFrame(records)

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df


def save_leads(df: pd.DataFrame) -> None:
    df = df.fillna("")  # blank/missing cells must be "" here, never NaN - NaN isn't valid JSON
    values = [df.columns.tolist()] + df.astype(str).values.tolist()

    # Only clear the sheet once the new data is fully built and ready to
    # write - never clear first, since a failure after that point would
    # leave the sheet empty with nothing written back.
    ws = _get_worksheet()
    ws.clear()
    ws.update(values)