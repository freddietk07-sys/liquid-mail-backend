from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from openai import OpenAI
import urllib.parse
import os
import requests
import base64
from datetime import datetime, timedelta, timezone

# -------------------------------------------------------
# Load env
# -------------------------------------------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()


# -------------------------------------------------------
# Models
# -------------------------------------------------------
class EmailPayload(BaseModel):
    inbox_id: str
    sender: str
    subject: str
    body: str


class SendEmailRequest(BaseModel):
    user_email: str
    to: str
    subject: str
    message: str


# Helper to ensure no Supabase bigints break JSON
def safe_record(record: dict):
    clean = {}
    for k, v in record.items():
        if isinstance(v, int) and abs(v) > 2_147_483_647:  # convert bigint → int
            clean[k] = int(v)
        else:
            clean[k] = v
    return clean


# -------------------------------------------------------
# STEP 0 — OAUTH LOGIN
# -------------------------------------------------------
@app.get("/oauth/gmail/login")
def gmail_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(500, "Missing Google OAuth env variables")

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send"
    }

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return {"oauth_url": url}


# -------------------------------------------------------
# STEP 1 — OAUTH CALLBACK: SAVE TOKENS
# -------------------------------------------------------
@app.get("/oauth/gmail/callback")
async def gmail_callback(code: str):
    token_url = "https://oauth2.googleapis.com/token"

    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    }

    res = requests.post(token_url, data=data)
    tokens = res.json()
    print("TOKEN RESPONSE:", tokens)

    if "access_token" not in tokens:
        raise HTTPException(400, tokens)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens["expires_in"]))

    # TODO: replace with real users later
    user_email = "prod.tkmusic@gmail.com"

    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type"),
        "scope": tokens.get("scope"),
        "expires_at": expires_at.isoformat()
    }).execute()

    return {"status": "saved", "email": user_email}


# -------------------------------------------------------
# STEP 2 — REFRESH TOKEN
# -------------------------------------------------------
def refresh_gmail_token(user_email: str):
    result = (
        supabase.table("gmail_tokens")
        .select("*")
        .eq("user_email", user_email)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(404, "No Gmail tokens found for user")

    record = safe_record(result.data[0])

    # Parse datetime from string → aware datetime
    stored_expiry = datetime.fromisoformat(record["expires_at"])

    if stored_expiry > datetime.now(timezone.utc):
        return record["access_token"]

    print("🔄 Access token expired — refreshing...")

    refresh_url = "https://oauth2.googleapis.com/token"

    res = requests.post(refresh_url, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": record["refresh_token"],
        "grant_type": "refresh_token"
    })
    new_data = res.json()
    print("REFRESH RESPONSE:", new_data)

    if "access_token" not in new_data:
        raise HTTPException(400, new_data)

    new_access = new_data["access_token"]
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=new_data["expires_in"])

    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": new_access,
        "refresh_token": record["refresh_token"],
        "token_type": record["token_type"],
        "scope": record["scope"],
        "expires_at": expires_at.isoformat()
    }).execute()

    return new_access


# -------------------------------------------------------
# STEP 3 — SEND GMAIL MESSAGE
# -------------------------------------------------------
def send_gmail_message(user_email: str, to_addr: str, subject: str, message_body: str):
    access_token = refresh_gmail_token(user_email)

    gmail_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    raw_msg = f"To: {to_addr}\r\nSubject: {subject}\r\n\r\n{message_body}"

    encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode()

    response = requests.post(
        gmail_url,
        json={"raw": encoded},
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    )

    print("SEND RESPONSE:", response.text)

    if response.status_code not in (200, 202):
        raise HTTPException(500, response.json())

    return response.json()


@app.post("/gmail/send")
def gmail_send(request: SendEmailRequest):
    data = send_gmail_message(
        user_email=request.user_email,
        to_addr=request.to,
        subject=request.subject,
        message_body=request.message
    )
    return {"status": "sent", "gmail": data}


# -------------------------------------------------------
# STEP 4 — AI WEBHOOK
# -------------------------------------------------------
@app.post("/webhook/email")
async def process_email(payload: EmailPayload):

    system_prompt = (
        "You write clear, friendly, professional email replies. "
        "Keep replies concise and helpful."
    )

    user_prompt = f"""
    Write a professional reply email:

    From: {payload.sender}
    Subject: {payload.subject}
    Body:
    {payload.body}
    """

    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        ai_reply = completion.choices[0].message.content

    except Exception as e:
        print("OPENAI ERROR:", e)
        ai_reply = "Error generating reply."

    supabase.table("email_logs").insert({
        "inbox_id": payload.inbox_id,
        "sender": payload.sender,
        "subject": payload.subject,
        "body": payload.body,
        "ai_reply": ai_reply,
        "confidence": 0.9,
        "status": "draft"
    }).execute()

    return {"status": "draft", "reply": ai_reply}


# -------------------------------------------------------
# Entry point
# -------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
