from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from openai import OpenAI
import urllib.parse
import os
import requests
import time
import base64
from datetime import datetime, timedelta, timezone

# -------------------------------------------------------
# Load environment variables
# -------------------------------------------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

# -------------------------------------------------------
# Initialize services
# -------------------------------------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

# -------------------------------------------------------
# Data Models
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


# -------------------------------------------------------
# STEP 0 — START OAUTH LOGIN
# -------------------------------------------------------
@app.get("/oauth/gmail/login")
def gmail_login():

    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Missing Google OAuth environment variables")

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send"
    }

    oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return {"oauth_url": oauth_url}


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

    response = requests.post(token_url, data=data)
    tokens = response.json()
    print("TOKEN RESPONSE:", tokens)

    if "access_token" not in tokens:
        raise HTTPException(status_code=400, detail=tokens)

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")

    # FIX: TIMESTAMPTZ uses real datetime, not int
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens["expires_in"]))

    # TEMP: Replace later with real user system
    user_email = "prod.tkmusic@gmail.com"

    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": tokens["token_type"],
        "scope": tokens["scope"],
        "expires_at": expires_at
    }).execute()

    return {"status": "saved", "email": user_email}


# -------------------------------------------------------
# STEP 2 — REFRESH TOKEN IF EXPIRED
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
        raise HTTPException(status_code=404, detail="No Gmail tokens found")

    record = result.data[0]

    # Token valid?
    if record["expires_at"] > datetime.now(timezone.utc):
        return record["access_token"]

    print("Access token expired — refreshing...")

    refresh_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": record["refresh_token"],
        "grant_type": "refresh_token",
    }

    response = requests.post(refresh_url, data=data)
    new_data = response.json()
    print("REFRESH RESPONSE:", new_data)

    if "access_token" not in new_data:
        raise HTTPException(status_code=400, detail=new_data)

    new_access = new_data["access_token"]

    # FIX: TIMESTAMPTZ value
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(new_data["expires_in"]))

    # Save updated token
    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": new_access,
        "refresh_token": record["refresh_token"],
        "token_type": record["token_type"],
        "scope": record["scope"],
        "expires_at": expires_at
    }).execute()

    return new_access


# -------------------------------------------------------
# STEP 3 — SEND EMAIL USING GMAIL API
# -------------------------------------------------------
def send_gmail_message(user_email: str, to_addr: str, subject: str, message_body: str):

    access_token = refresh_gmail_token(user_email)

    api_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    raw_email = f"To: {to_addr}\r\nSubject: {subject}\r\n\r\n{message_body}"

    encoded = base64.urlsafe_b64encode(raw_email.encode()).decode()

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {"raw": encoded}

    response = requests.post(api_url, json=payload, headers=headers)
    print("SEND RESPONSE:", response.json())

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.json())

    return response.json()


@app.post("/gmail/send")
def gmail_send(request: SendEmailRequest):
    result = send_gmail_message(
        user_email=request.user_email,
        to_addr=request.to,
        subject=request.subject,
        message_body=request.message
    )
    return {"status": "sent", "response": result}


# -------------------------------------------------------
# STEP 4 — AI EMAIL REPLY WEBHOOK
# -------------------------------------------------------
@app.post("/webhook/email")
async def process_email(payload: EmailPayload):

    system_prompt = (
        "You write clear, friendly, professional email replies. "
        "Keep replies concise and helpful."
    )

    user_prompt = f"""
    Write a professional reply email.

    Incoming email:
    From: {payload.sender}
    Subject: {payload.subject}
    Body:
    {payload.body}

    Reply politely and helpfully.
    """

    try:
        completion = client.chat.completions.create(
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
# Uvicorn Entrypoint (Railway Compatible)
# -------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
