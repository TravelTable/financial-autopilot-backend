from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def build_gmail_service(access_token: str, refresh_token: str | None, client_id: str, client_secret: str):
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def list_messages(service, query: str, page_token: str | None = None, max_results: int = 100) -> dict:
    return service.users().messages().list(userId="me", q=query, pageToken=page_token, maxResults=max_results).execute()

def get_message(service, message_id: str, format: str = "full") -> dict:
    return service.users().messages().get(userId="me", id=message_id, format=format).execute()

def get_attachment(service, message_id: str, attachment_id: str) -> dict:
    return (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
