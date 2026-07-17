#!/usr/bin/env python3
"""
Create a new Google Sheet and share it with specified emails.

Usage:
    python create_sheet.py "Sheet Title" user1@gmail.com user2@gmail.com

Requirements:
    - client_secrets.json in the same directory (OAuth2 credentials from Google Cloud Console)
      OR the Drive API enabled for the service account (your GCP project)

Getting client_secrets.json:
    1. Go to https://console.cloud.google.com/apis/credentials?project=<your-project-id>
    2. Create credentials → OAuth client ID → Desktop app
    3. Download JSON and save as client_secrets.json here

Alternatively, enable Drive API for the service account:
    https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=<your-project-number>
    Then run with --service-account flag.
"""

import os
import sys
import json
import argparse
from pathlib import Path


SERVICE_ACCOUNT_CREDS = Path(os.environ.get("GOOGLE_CREDS_PATH", str(Path(__file__).parent / "google-credentials.json")))
CLIENT_SECRETS = Path(__file__).parent / "client_secrets.json"
TOKEN_FILE = Path(__file__).parent / "oauth_token.json"


def _service_account_email() -> str:
    """Read the service account's own email out of its credentials file."""
    return json.loads(SERVICE_ACCOUNT_CREDS.read_text())["client_email"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_oauth_credentials():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS.exists():
                raise FileNotFoundError(
                    f"client_secrets.json not found. See script docstring for instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


def get_service_account_credentials():
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_CREDS), scopes=SCOPES
    )


def create_and_share_sheet(title: str, share_emails: list[str], use_service_account: bool = False):
    from googleapiclient.discovery import build

    if use_service_account:
        creds = get_service_account_credentials()
        print("Using service account credentials")
    else:
        creds = get_oauth_credentials()
        print("Using OAuth2 user credentials")

    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    # Create spreadsheet
    spreadsheet = sheets.spreadsheets().create(body={
        "properties": {"title": title}
    }).execute()

    sheet_id = spreadsheet["spreadsheetId"]
    sheet_url = spreadsheet["spreadsheetUrl"]
    print(f"Created: {title}")
    print(f"  ID:  {sheet_id}")
    print(f"  URL: {sheet_url}")

    # Share with service account so the scraper can write to it
    all_emails = list(share_emails)
    service_account_email = None
    if not use_service_account and SERVICE_ACCOUNT_CREDS.exists():
        service_account_email = _service_account_email()
        if service_account_email not in all_emails:
            all_emails.insert(0, service_account_email)

    for email in all_emails:
        drive.permissions().create(
            fileId=sheet_id,
            body={"type": "user", "role": "writer", "emailAddress": email},
            sendNotificationEmail=(email != service_account_email),
        ).execute()
        print(f"  Shared with: {email}")

    return sheet_id


def main():
    parser = argparse.ArgumentParser(description="Create a Google Sheet and share it")
    parser.add_argument("title", help="Spreadsheet title")
    parser.add_argument("emails", nargs="+", help="Email addresses to share with (writer)")
    parser.add_argument("--service-account", action="store_true",
                        help="Use service account instead of OAuth2 (requires Drive API enabled)")
    args = parser.parse_args()

    sheet_id = create_and_share_sheet(args.title, args.emails, args.service_account)

    print(f"\nAdd to profiles.json:")
    print(json.dumps({"sheet_id": sheet_id}, indent=2))


if __name__ == "__main__":
    main()
