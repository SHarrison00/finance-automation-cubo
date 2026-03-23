import io
import os
from datetime import datetime, timezone

import pandas as pd
from flask import Flask, jsonify
from google.auth import default
from google.cloud import storage
import gspread

app = Flask(__name__)

BUCKET_NAME = "afc-cubo-finance"
RAW_PREFIX = "raw/transactions/"
LATEST_PATH = "processed/transactions/latest/bank_transactions_clean.csv"
SNAPSHOTS_PREFIX = "processed/transactions/snapshots/"

SHEET_NAME = os.environ.get("SHEET_NAME", "Cubo Treasury")
BANK_TAB = "Bank Transactions"

REQUIRED_COLUMNS = [
    "Transaction Date",
    "Transaction Description",
    "Debit Amount",
    "Credit Amount",
    "Balance",
]

GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_storage_client():
    """Returns authenticated client object for the Google Cloud Storage API."""
    return storage.Client()


def get_sheets_client():
    """Returns authenticated client object for the Google Sheets API."""
    creds = default(scopes=GSHEETS_SCOPES)[0]
    return gspread.authorize(creds)


def list_raw_csvs(client):
    """Returns a list of CSVs from the Google Cloud bucket."""
    bucket = client.bucket(BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=RAW_PREFIX)
    blob_csvs = [blob for blob in blobs if blob.name.endswith(".csv")]
    return blob_csvs


def read_csv(blob):
    """Returns a DataFrame containing raw data from the CSV."""
    # Handle BOM character
    content = blob.download_as_text(encoding="utf-8-sig")
    return pd.read_csv(io.StringIO(content))


def validate_schema(df, filename):
    """Checks whether file has columns we expect."""
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"File {filename} is missing columns: {missing}")


def prepare(df):
    """Adds a datetime column for each bank transaction."""
    df = df[list(REQUIRED_COLUMNS)].copy()
    df[["Debit Amount", "Credit Amount"]] = df[["Debit Amount", "Credit Amount"]].fillna(0)

    # Extract time from transaction description
    pattern = r'\d{2}[A-Z]{3}\d{2}\s+(\d{2}:\d{2})'
    df['Time'] = df['Transaction Description'].str.extract(pattern)
    df['Time'] = df['Time'].fillna('17:00') # Nulls are bank service charges (happen c.5pm)

    df['Transaction Date'] = pd.to_datetime(df['Transaction Date'], format='%d/%m/%Y')
    df['Datetime'] = pd.to_datetime(
        df['Transaction Date'].dt.strftime('%Y-%m-%d') + ' ' + df['Time']
    )
    df['Datetime'] = df['Datetime'].dt.strftime('%Y-%m-%d %H:%M')

    # Update columns
    df = df[['Datetime'] + [c for c in REQUIRED_COLUMNS if c != 'Transaction Date']]

    return df


def write_gcs(client, path, content, content_type="text/csv"):
    """Writes a string of content to the given path in the Google Cloud bucket."""
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(path)
    blob.upload_from_string(content, content_type=content_type)


def push_to_sheet(gc, df):
    """
    Write the cleaned transactions DataFrame to the Google Sheet, while preserving 
    any categories previously assigned by the treasurer.
    """
    ws = gc.open(SHEET_NAME).worksheet(BANK_TAB)

    # Read existing categories
    categories = {
        (row["Balance"], row["Transaction Description"]): row["Category"]
        for row in ws.get_all_records()
        if row.get("Category")
    }

    # Add existing category to cleaned transactions dataframe
    df["Category"] = df.apply(
        lambda r: categories.get((r["Balance"], r["Transaction Description"]), ""),
        axis=1
    )

    # Wipe and update the Google Sheet
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())


@app.route("/health")
def health():
    """Health check endpoint. Returns 200 if the service is running."""
    return jsonify({"status": "ok"}), 200


@app.route("/update-transactions", methods=["POST"])
def update_transactions():
    """
    Reads all raw transaction CSVs from GCS, cleans and deduplicates them, then
    writes the result back to GCS, and updates the 'Bank Transactions' tab in 
    the Google Sheet. Triggered by the 'Update Transactions' button.
    """
    try:
        storage_client = get_storage_client()
        gc = get_sheets_client()
        
        blobs = list_raw_csvs(storage_client)

        # Read, validate and prepare CSVs
        frames = []
        for blob in blobs:
            df = read_csv(blob)
            validate_schema(df, blob.name)
            df = prepare(df)
            frames.append(df)

        # Combine CSVs
        df = pd.concat(frames)
        df = df.drop_duplicates()
        df = df.sort_values('Datetime', ascending=False)

        # Write to Google Cloud Storage
        write_gcs(storage_client, LATEST_PATH, df.to_csv(index=False), "text/csv")
        
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        snapshot_path = f"{SNAPSHOTS_PREFIX}{timestamp}_bank_transactions_clean.csv"
        write_gcs(storage_client, snapshot_path, df.to_csv(index=False), "text/csv")
        
        # Write to Google Sheet
        push_to_sheet(gc, df)

        return jsonify({"status": "ok"}), 200
    
    
    except Exception as e:
        
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
