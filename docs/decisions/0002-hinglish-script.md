# 0002. Hinglish uses Roman script; Hindi stays Devanagari

- Status: Accepted
- Date: 2026-10-08

## Context

Many users speak Hinglish (Hindi mixed with English words). STT can return it in
Devanagari or in Roman letters, and the LLM tends to answer in whatever script it saw
last. Mixed scripts confuse users who read the on-screen transcript.

## Decision

- Hinglish transcripts and replies are written in **Roman (Latin) script**
  ("Chrome kholo" -> "Chrome khol diya").
- Hindi spoken in Devanagari stays **Devanagari** ("क्रोम खोलो" -> "क्रोम खोल दिया").
- Other Indian languages reply in their own script.
- `language.detect.detect_language` is the single public detector. It returns
  `LanguageInfo(code, script, romanised)`; Roman Hindi is `hi-IN` with `romanised=True`,
  recognised with the V1 word list in `language/detector.py`. The brain prompt and the
  canned phrases use `romanised` to pick the script; TTS always gets `hi-IN`.

## Consequences

- Canned phrases (`language/phrases.py`) need a romanised Hindi variant.
- An English sentence that contains a common Hindi word ("do", "me") may be tagged
  Hinglish; the STT language code wins when present, which limits this.
- The golden set has separate `hi` and `hinglish` buckets, so accuracy is tracked for both.
