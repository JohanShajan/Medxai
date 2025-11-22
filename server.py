from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import os

from dotenv import load_dotenv
load_dotenv()

# Twilio for WhatsApp replies
from twilio.twiml.messaging_response import MessagingResponse

# Local modules
import vaccinations
import preventive_health
import diseases_multilang
import languages

# ------------------ basic setup ------------------

app = FastAPI()
if os.path.isdir("public"):
    app.mount("/public", StaticFiles(directory="public"), name="public")

# Load FAQ DB (40-disease multilingual json)
FAQ_PATH = "faq.json"
FAQ = {}
if os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "r", encoding="utf-8") as f:
        FAQ = json.load(f)

# --------- language + greeting texts ----------

SUPPORTED_LANGS = {"en", "hi"}   # add more later if needed

GREET_KEYWORDS = {
    "en": ["hi", "hello", "hey", "hai"],
    "hi": ["namaste", "namaskar", "namasthe", "नमस्ते", "नमस्कार"],
}

GREET_MESSAGE = {
    "en": (
        "👋 Hello! I'm *Medxai*, your whatsapp health assistant.\n\n"
       
    ),
    "hi": (
        "👋 नमस्ते! मैं *MedXpert* हूँ, आपका ऑफ़लाइन स्वास्थ्य सहायक।\n\n"
        "आप ऐसे सवाल पूछ सकते हैं:\n"
        "• डेंगू के लक्षण\n"
        "• मलेरिया से बचाव कैसे करें\n"
        "• शिशु / बच्चा / वयस्क / गर्भवती के टीकाकरण की सूची\n\n"
        "किसी भाषा को चुनने के लिए आप ऐसे लिख सकते हैं:\n"
        "`lang:hi डेंगू के लक्षण`\n\n"
        "आपात स्थिति में तुरंत डॉक्टर या नज़दीकी अस्पताल से संपर्क करें।"
    ),
}

FALLBACK_MESSAGE = {
    "en": (
        "I couldn't find an  answer for that.\n\n"
        "Searching the web.....\n"
      
    ),
    "hi": (
        "मुझे इस प्रश्न का ऑफ़लाइन उत्तर नहीं मिला।\n\n"
        "आप इन विषयों पर पूछ सकते हैं:\n"
        "• डेंगू / मलेरिया / टीबी के लक्षण\n"
        "• मलेरिया / टाइफ़ॉइड से बचाव कैसे करें\n"
        "• शिशु / बच्चा / वयस्क / गर्भवती के टीकाकरण की सूची\n\n"
        "छाती में दर्द, तेज़ रक्तस्राव या सांस लेने में दिक्कत जैसी आपात स्थिति में "
        "कृपया तुरंत डॉक्टर या अस्पताल से संपर्क करें।"
    ),
}


class ChatMessage(BaseModel):
    message: str
    lang: str = "en"


# ------------------ core logic ------------------

def search_faq(text: str, lang: str = "en") -> str | None:
    """
    Simple FAQ search on faq.json.
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
    if lang not in SUPPORTED_LANGS:
        lang = "en"

    lower = text.lower()

    if not text:
        return {
            "type": "fallback",
            "answer": GREET_MESSAGE.get(lang, GREET_MESSAGE["en"])
        }

    # 0) Greetings (hi, hello, namaste, etc.)
    for g in GREET_KEYWORDS.get(lang, []):
        if lower == g or lower.startswith(g + " "):
            return {
                "type": "greeting",
                "answer": GREET_MESSAGE.get(lang, GREET_MESSAGE["en"])
            }

    # 1) FAQ (40-disease database)
    faq_answer = search_faq(text, lang)
    if faq_answer:
        return {"type": "faq", "answer": faq_answer}

    # 2) Vaccination schedule
    if any(w in lower for w in [
        "vaccine", "vaccination", "vaccine schedule", "immunization",
        "टीका", "टीकाकरण"
    ]):
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
    fallback_text = FALLBACK_MESSAGE.get(lang, FALLBACK_MESSAGE["en"])
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
    We read 'Body', parse optional lang prefix, call process_message,
    and reply with TwiML XML.
    """
    form = await request.form()
    raw_body = (form.get("Body") or "").strip()

    lang = "en"
    text = raw_body

    # Parse language prefix, e.g. "lang:hi डेंगू के लक्षण"
    lower = raw_body.lower()
    if lower.startswith("lang:"):
        rest = raw_body[5:].lstrip()  # after "lang:"
        if rest:
            first = rest.split()[0]           # e.g. "hi" or "hi;..."
            lang_code = first.split(";")[0].lower().strip()
            if lang_code in SUPPORTED_LANGS:
                lang = lang_code
                text = rest[len(first):].lstrip(" ;,")
            else:
                lang = "en"
                text = rest

    result = process_message(text, lang)
    answer = result.get("answer") or "Sorry, something went wrong."

    resp = MessagingResponse()
    resp.message(answer)

    return PlainTextResponse(content=str(resp), media_type="application/xml")
