# server.py
# MedXpert offline bot + WhatsApp integration
# Run locally: uvicorn server:app --reload --port 3000

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import os

from dotenv import load_dotenv
load_dotenv()

# Twilio for WhatsApp replies
from twilio.twiml.messaging_response import MessagingResponse

# Local modules (you already have these)
import vaccinations
import preventive_health
import diseases_multilang
import languages

# ------------------ basic setup ------------------

app = FastAPI()
if os.path.isdir("public"):
    app.mount("/public", StaticFiles(directory="public"), name="public")

# Load FAQ DB (40-disease multilingual json)
FAQ_PATH = "faq_multilang.json"
FAQ = {}
if os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "r", encoding="utf-8") as f:
        FAQ = json.load(f)


class ChatMessage(BaseModel):
    message: str
    lang: str = "en"


# ------------------ core logic ------------------

def search_faq(text: str, lang: str = "en") -> str | None:
    """
    Simple FAQ search on faq_multilang.json.
    Works for questions like:
      'what is dengue', 'symptoms of malaria', etc.
    """
    if not FAQ or not text:
        return None

    q = text.lower().strip()

    # try to match any key inside question
    best_key = None
    for key in FAQ.keys():
        k = key.lower().strip()
        if not k:
            continue
        if k == q or k in q or q in k:
            best_key = key
            break

    # second pass: token-based
    if not best_key:
        tokens = q.split()
        for key in FAQ.keys():
            k = key.lower().strip()
            if any(t and t in k for t in tokens):
                best_key = key
                break

    if best_key:
        entry = FAQ[best_key]
        return entry.get(lang) or entry.get("en")
    return None


def process_message(text: str, lang: str = "en") -> dict:
    """
    Single place where we decide the answer.
    Used by both /chat (web) and /whatsapp (Twilio).
    Returns: {"type": ..., "answer": "...", "extra": optional}
    """
    text = (text or "").strip()
    lang = (lang or "en").lower()
    lower = text.lower()

    if not text:
        return {
            "type": "fallback",
            "answer": "Please type a health question, like 'what is dengue?' or 'symptoms of malaria'."
        }

    # 1) FAQ (40-disease database)
    faq_answer = search_faq(text, lang)
    if faq_answer:
        return {"type": "faq", "answer": faq_answer}

    # 2) Vaccination schedule
    if any(w in lower for w in ["vaccine", "vaccination", "vaccine schedule", "immunization"]):
        return {
            "type": "vaccination",
            "answer": "Here is the offline vaccination schedule (infant, child, adult, pregnant).",
            "extra": vaccinations.VACCINATION_SCHEDULES
        }

    # 3) Preventive health modules
    for key in preventive_health.MODULES.keys():
        if key in lower:
            return {
                "type": "preventive",
                "answer": preventive_health.MODULES[key]
            }

    # 4) Simple disease info module (if you have it)
    disease_info = diseases_multilang.find_disease(text, lang)
    if disease_info:
        return {"type": "disease", "answer": disease_info}

    # 5) Fallback
    fallback_text = (
        "I couldn't find an offline answer for that.\n\n"
        "Try asking about:\n"
        "• symptoms of dengue / malaria / TB\n"
        "• how to prevent malaria / typhoid\n"
        "• vaccination schedule for infant / child / adult / pregnant\n\n"
        "For emergencies (chest pain, severe bleeding, breathing difficulty), "
        "please contact a doctor or hospital immediately."
    )
    return {"type": "fallback", "answer": fallback_text}


# ------------------ web UI route ------------------

@app.get("/")
def index():
    path = os.path.join("public", "index.html")
    if os.path.exists(path):
        return HTMLResponse(open(path, "r", encoding="utf-8").read())
    return HTMLResponse("<h1>MedXpert</h1><p>UI not found.</p>")


# ------------------ /chat (web) ------------------

@app.post("/chat")
def chat(msg: ChatMessage):
    result = process_message(msg.message, msg.lang)
    return JSONResponse({
        "type": result["type"],
        "payload": {
            "answer": result["answer"],
            "extra": result.get("extra")
        }
    })


# ------------------ /whatsapp (Twilio webhook) ------------------

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Twilio will call this URL when someone sends a WhatsApp message.
    We read 'Body', call process_message, and reply with TwiML XML.
    """
    form = await request.form()
    body = (form.get("Body") or "").strip()

    # later we can add multi-language via "lang:hi; question..."
    lang = "en"

    result = process_message(body, lang)
    answer = result.get("answer") or "Sorry, something went wrong."

    resp = MessagingResponse()
    msg = resp.message()
    msg.body(answer)

    return HTMLResponse(str(resp), media_type="application/xml")
