# Language Specification — Hindi + English

## 1. Purpose

The language layer defines how the assistant handles user language between:

- Speech-to-text (STT)
- Language understanding / LLM
- PC action execution
- Response generation
- Text-to-speech (TTS)

### V1 Language Scope

The first version supports:

- English
- Hindi
- Hindi-English mixed language (Hinglish)

Other Indian languages are outside the V1 implementation and may be added later.

---

## 2. Language Flow

The language pipeline is:

User Speech
    ↓
Saaras STT
    ↓
Raw Transcript
    ↓
Language Detection
    ↓
Language Normalization
    ↓
LLM / Intent Understanding
    ↓
Action + Response
    ↓
Response Language Selection
    ↓
Bulbul TTS
    ↓
Spoken Response

The language layer must preserve the user's intended action while handling
Hindi, English, and mixed Hindi-English input.

---

## 3. Supported Language Modes

The language layer recognizes three V1 modes.

### 3.1 English

Examples:

- "Open Chrome"
- "Take a screenshot"
- "Set the volume to 40"
- "What is the battery level?"

Expected response:

- "Opening Chrome."
- "Taking a screenshot."
- "The volume is now 40 percent."

---

### 3.2 Hindi

Examples:

- "क्रोम खोलो"
- "स्क्रीनशॉट लो"
- "वॉल्यूम चालीस कर दो"
- "बैटरी कितनी है?"

Expected response:

- "क्रोम खोल रहा हूँ।"
- "स्क्रीनशॉट ले रहा हूँ।"
- "वॉल्यूम चालीस प्रतिशत कर दिया है।"
- "बैटरी इतनी है।"

---

### 3.3 Hindi-English Mixed Language

Examples:

- "Chrome kholo"
- "YouTube open karo"
- "Volume 40 kar do"
- "Screenshot le lo"
- "Notepad mein ek note likho"
- "Battery kitni hai?"

The system must treat mixed Hindi-English commands as a
single natural language instruction rather than requiring the user
to speak entirely in one language.

---

## 4. Language Detection

The language layer should determine the dominant interaction language.

Possible outputs:

```text
en
hi
hi-en