from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import os
import re
from collections import Counter

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

# Load FAQ DB 
FAQ_PATH = "faq.json"
FAQ = {}
if os.path.exists(FAQ_PATH):
    with open(FAQ_PATH, "r", encoding="utf-8") as f:
        FAQ = json.load(f)

print("FAQ loaded keys:", list(FAQ.keys()))

# ------------------ simple NLP index over FAQ ------------------

# Very small English stopword list
STOP_WORDS = {
    "a", "an", "the", "is", "are", "am", "i", "you", "he", "she", "it", "we", "they",
    "of", "for", "to", "in", "on", "and", "or", "but", "with", "at", "from",
    "this", "that", "these", "those", "about", "what", "how", "when", "why",
    "do", "does", "did", "my", "your", "his", "her", "their", "our", "have",
    "has", "had", "me", "be", "been", "was", "were"
}

# disease_key -> set of tokens from its description+symptoms+prevention+remedies
DISEASE_DOCS: dict[str, set[str]] = {}


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"\w+", text.lower())
    return [t for t in tokens if t not in STOP_WORDS]


def build_disease_index() -> None:
    """
    Build a simple token set for each disease from its English content.
    This is used for symptom-based matching if the disease name is not typed.
    """
    global DISEASE_DOCS
    DISEASE_DOCS = {}

    if not FAQ:
        return

    for disease_key, disease_data in FAQ.items():
        lang_block = disease_data.get("en")
        if not lang_block:
            continue

        parts: list[str] = []
        for field in ("what", "symptoms", "prevention", "remedies"):
            val = lang_block.get(field)
            if isinstance(val, list):
                parts.extend(val)
            elif isinstance(val, str):
                parts.append(val)

        if not parts:
            continue

        doc = " ".join(parts)
        tokens = _tokenize(doc)
        if tokens:
            DISEASE_DOCS[disease_key] = set(tokens)

    print("DISEASE_DOCS built for diseases:", list(DISEASE_DOCS.keys()))


def nlp_guess_disease(text: str) -> str | None:
    """
    Very simple 'NLP':
    - Tokenize user text
    - For each disease, count how many tokens overlap
    - Pick disease with maximum overlap, if above a small threshold
    """
    if not DISEASE_DOCS:
        return None

    tokens = set(_tokenize(text))
    if not tokens:
        return None

    best_key = None
    best_overlap = 0

    for disease_key, doc_tokens in DISEASE_DOCS.items():
        overlap = len(tokens & doc_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_key = disease_key

    # Threshold: 3 overlapping tokens is a reasonable starting point
    if best_overlap < 3:
        return None

    print("[NLP] Guessed disease %r with overlap %d" % (best_key, best_overlap))
    return best_key


# Build the index once at startup
build_disease_index()

# --------- language + greeting texts ----------

SUPPORTED_LANGS = {"en", "hi"}   # add more later if needed

GREET_KEYWORDS = {
    "en": ["hi", "hello", "hey", "hai"],
    "hi": ["namaste", "namaskar", "namasthe", "नमस्ते", "नमस्कार"],
}

GREET_MESSAGE = {
    "en": (
        "👋 Hello! I'm *Ziva*, your whatsapp health assistant.\n\n"
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
    Search the disease JSON in faq.json.

    Steps:
    1) Rule-based detection using disease key (with _ / - normalized to space)
    2) If no match, use symptom-based NLP (nlp_guess_disease)
    3) Detect category: what / symptoms / prevention / remedies
    4) Format WhatsApp-friendly answer
    """
    if not FAQ or not text:
        return None

    q_orig = text
    q = text.lower().strip()
    lang = lang if lang in SUPPORTED_LANGS else "en"

    # ---------- 1) detect disease ----------

    disease_key = None

    # 1a) direct key substring (handles dengue, malaria, migraine, "kidney stones", etc.)
    for d in FAQ.keys():
        d_low = d.lower()
        # handle keys like "heart_attack", "heat_stroke", "food_poisoning"
        variants = {
            d_low,                      # heart_attack
            d_low.replace("_", " "),    # heart attack
            d_low.replace("-", " "),    # heat stroke
        }
        if any(v in q for v in variants):
            disease_key = d
            print("[FAQ] Matched by key substring:", d)
            break

    # 1b) if still not found, try simple symptom-based NLP
    if not disease_key:
        disease_key = nlp_guess_disease(q_orig)

    if not disease_key:
        print("[FAQ] No disease matched for query:", q)
        return None

    disease_data = FAQ.get(disease_key, {})
    lang_block = disease_data.get(lang) or disease_data.get("en")
    if not lang_block:
        print("[FAQ] No lang block for disease:", disease_key)
        return None

    # ---------- 2) detect what user is asking (category) ----------

    category = "what"   # default

    # English keywords
    if any(w in q for w in ["symptom", "symptoms", "signs"]):
        category = "symptoms"
    elif any(w in q for w in ["prevent", "prevention", "avoid", "protection"]):
        category = "prevention"
    elif any(w in q for w in ["remedy", "remedies", "treat", "treatment", "cure"]):
        category = "remedies"

    # Hindi keywords
    if any(w in q for w in ["लक्षण"]):
        category = "symptoms"
    elif any(w in q for w in ["बचाव", "रोकथाम"]):
        category = "prevention"
    elif any(w in q for w in ["उपचार", "इलाज"]):
        category = "remedies"

    data = lang_block.get(category)
    if data is None:
        data = lang_block.get("what")
        if data is None:
            return None
        category = "what"

    # ---------- 3) format answer ----------

    title_map = {
        "what": "About",
        "symptoms": "Symptoms",
        "prevention": "Prevention",
        "remedies": "Remedies"
    }

    if lang == "hi":
        title_map = {
            "what": "क्या है",
            "symptoms": "לक्षण",  # note: in your earlier code this had a typo 'बچाव'
            "prevention": "बचाव",
            "remedies": "उपचार / घरेलू उपाय"
        }

    heading = title_map.get(category, category.capitalize())
    disease_title = disease_key.replace("_", " ").replace("-", " ").title()

    if isinstance(data, list):
        bullet_lines = "\n".join("• " + item for item in data)
        return "*%s – %s*\n%s" % (disease_title, heading, bullet_lines)
    else:
        return "*%s – %s*\n%s" % (disease_title, heading, data)


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

    # 0.5) Thank you messages
    THANK_KEYWORDS = {
        "en": ["thank", "thanks", "thank you", "thx", "ty"],
        "hi": ["dhanyavad", "धन्यवाद", "shukriya", "शुक्रिया"]
    }

    for t in THANK_KEYWORDS.get(lang, []):
        if t in lower:
            thank_reply = {
                "en": "😊 You're welcome! Let me know if you need more health information.",
                "hi": "😊 आपका स्वागत है! यदि आपको और स्वास्थ्य जानकारी चाहिए, तो बताएं।"
            }
            return {
                "type": "thanks",
                "answer": thank_reply.get(lang, thank_reply["en"])
            }

    # 1) FAQ (40-disease database using JSON + NLP assist)
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

    # 4) Simple disease info module (if you still keep this separate)
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
