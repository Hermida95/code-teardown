#!/usr/bin/env python3
"""Text pipeline, step 1: extract evidence that a text was produced or polished with AI.

Standard library only; the text is read and counted, never executed or sent anywhere. It writes
evidence items in the same format as analyze_image.py (score 0 = not AI, 10 = AI; weight 0-1).

Text is the weakest ground of all. Two kinds of evidence exist here, and they are not equal:

  * RESIDUE: things a chat assistant leaves behind when its output is pasted: citation markers,
    "as an AI language model", tracking parameters in links. A person almost never writes these
    by accident, so they weigh a lot (but they show that AI text passed through, not how much).
  * STYLE: sentence-length variation, stock phrases, em-dash density. Every one of these is also
    something people do, models change over time, and non-native writers are flagged far more
    often than natives. They weigh very little, and score_evidence.py caps their total weight.

No language model is involved, so there is no perplexity score; that would need one.

Usage: analyze_text.py TEXT_FILE|- [--out evidence.json] [--force]
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # keep this check above every other import
    sys.exit(f"ai-evidence needs Python 3.11 or newer (this is {sys.version_info.major}.{sys.version_info.minor}).")

import argparse
import hashlib
import json
import re
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import checked_output_path, clean_quote, dump  # noqa: E402
from analyze_image import T, ev  # noqa: E402

VERSION = "0.1.0"
MAX_BYTES = 2 * 1024 * 1024
MIN_WORDS_STYLE = 150          # below this, style statistics are noise and are not scored
MIN_SENTENCES = 12
CHUNK_WORDS = 120

# Rates are per 1000 words. All thresholds are reasoned guesses, not calibrated values.
PHRASE_STRONG, PHRASE_SOME = 8.0, 4.0
EM_DASH_HIGH = 4.0
CV_LOW, CV_HIGH = 0.35, 0.75
OPENER_SHARE = 0.12

CITATION_MARKERS = [
    (re.compile(r"【\d+:\d+†[^】]{0,80}】"), "【n:n†source】"),
    (re.compile(r"\[?oaicite:\d+\]?|contentReference\[oaicite", re.I), "oaicite"),
    (re.compile(r"\bturn\d+(?:search|view|news|image)\d+\b"), "turnNsearchN"),
    (re.compile(r"[?&]utm_source=(?:chatgpt\.com|openai|copilot\.com|perplexity)\b", re.I), "utm_source=chatgpt.com"),
]

ASSISTANT_STRONG = {
    "en": [r"as an ai language model", r"as a large language model", r"as an ai assistant", r"i (?:cannot|can't|can not) (?:fulfill|assist with) (?:this|that) request",
           r"my (?:knowledge|training) (?:cutoff|cut-off)", r"as of my last (?:knowledge )?update", r"regenerate response", r"i don't have personal (?:opinions|experiences|feelings)",
           r"i'm sorry, but i (?:can't|cannot|am unable)", r"i am sorry, but i (?:can't|cannot|am unable)"],
    "es": [r"como (?:un )?modelo de lenguaje", r"como (?:una )?(?:ia|inteligencia artificial)\b", r"mi (?:fecha de corte|corte de conocimiento)",
           r"lo siento, pero no (?:puedo|me es posible)", r"no tengo opiniones personales", r"hasta mi última actualización"],
}
ASSISTANT_SOFT = {
    "en": [r"certainly! here", r"sure! here(?:'s| is)", r"here(?:'s| is) (?:a|an|the) (?:revised|rewritten|improved|polished) (?:version|draft)", r"i hope this helps",
           r"let me know if you(?:'d| would) like (?:me )?to", r"feel free to (?:ask|reach out)"],
    "es": [r"claro, aquí tienes", r"¡claro! aquí", r"aquí tienes (?:una|la) versión", r"espero que esto te (?:ayude|sirva)", r"si quieres, puedo", r"no dudes en preguntar"],
}
PLACEHOLDERS = re.compile(r"\[(?:insert|your|company|name|date|recipient|nombre|tu |inserta)[^\]\n]{0,40}\]", re.I)

# Stock phrases models overuse (and people use too). Lists drift as models change.
AI_PHRASES = {
    "en": [r"delve(?:s|d)? into", r"\bdelving\b", r"rich tapestry", r"\btapestry of\b", r"a testament to", r"\bmultifaceted\b", r"\bintricate (?:web|interplay|dance)\b",
           r"it(?:'s| is) (?:important|worth|crucial) to (?:note|mention|remember|consider)", r"plays? a (?:crucial|pivotal|vital|key) role", r"in today's (?:fast-paced|digital|ever-changing)",
           r"ever-evolving (?:landscape|world)", r"\bin the realm of\b", r"\bunderscor(?:e|es|ed|ing)\b", r"\bpivotal\b", r"\bseamless(?:ly)?\b", r"\bholistic\b", r"\bmeticulous(?:ly)?\b",
           r"\bleverag(?:e|es|ed|ing)\b", r"\bfoster(?:s|ed|ing)?\b", r"\bnavigat(?:e|es|ed|ing) the (?:complexities|challenges|landscape)\b", r"\bgame[- ]changer\b",
           r"\bin conclusion,", r"\boverall,", r"\bmoreover,", r"\bfurthermore,", r"embark(?:s|ed)? on a journey", r"\bunlock(?:s|ed|ing)? the (?:power|potential|secrets?)\b",
           r"\bnot only\b[^.!?\n]{0,80}\bbut also\b"],
    "es": [r"en el mundo (?:actual|de hoy)", r"en el (?:panorama|ámbito|contexto) actual", r"cabe (?:destacar|señalar|mencionar)", r"es (?:importante|fundamental|crucial) (?:destacar|señalar|tener en cuenta|recordar)",
           r"desempeña un papel (?:crucial|fundamental|clave)", r"un testimonio de", r"\bmultifacético\b", r"\btapiz\b", r"en un mundo cada vez más", r"\badentrarnos\b", r"\bsin lugar a dudas\b",
           r"\ben conclusión,", r"\ben resumen,", r"\basimismo,", r"\bademás,", r"\bpor otro lado,", r"\bholístic[oa]\b", r"\bimpulsar\b", r"\bfomentar\b", r"\bno solo\b[^.!?\n]{0,80}\bsino también\b"],
}
AI_PHRASE_RES = {lang: re.compile("|".join(f"(?:{p})" for p in patterns), re.I) for lang, patterns in AI_PHRASES.items()}
ASSISTANT_STRONG_RES = {lang: re.compile("|".join(f"(?:{p})" for p in patterns), re.I) for lang, patterns in ASSISTANT_STRONG.items()}
ASSISTANT_SOFT_RES = {lang: re.compile("|".join(f"(?:{p})" for p in patterns), re.I) for lang, patterns in ASSISTANT_SOFT.items()}
TRANSITIONS = {"en": ("moreover", "furthermore", "additionally", "in addition", "however", "overall", "in conclusion", "ultimately", "importantly"),
               "es": ("además", "asimismo", "sin embargo", "en conclusión", "en resumen", "por otro lado", "por lo tanto", "en definitiva", "cabe destacar")}
CONTRASTIVE = re.compile(r"\b(?:it(?:'s| is)|this is|that(?:'s| is)) not (?:just|only|merely|simply)\b[^.!?\n]{0,70}[,;—–-]\s*(?:it(?:'s| is)|but|this is)\b"
                         r"|\bno es (?:solo|solamente|simplemente)\b[^.!?\n]{0,70}[,;—–-]\s*(?:es|sino)\b", re.I)
BOLD_BULLET = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+\*\*[^*\n]{2,60}\*\*\s*[:：-]", re.M)
CHAT_MARKDOWN = re.compile(r"^#{1,4}\s+\S|\*\*[^*\n]{2,60}\*\*", re.M)
INVISIBLE = {"​": "zero-width space", "‌": "zero-width non-joiner", "‍": "zero-width joiner", "⁠": "word joiner",
             " ": "narrow no-break space", "﻿": "byte order mark inside the text", "­": "soft hyphen"}
IRREGULAR = [re.compile(r"(?<![.!?]\s)(?<=[a-záéíóú] )i (?=[a-z])"), re.compile(r"\b(?:dont|cant|wont|didnt|doesnt|isnt|im|ive)\b", re.I),
             re.compile(r"[!?]{2,}"), re.compile(r"\b(\w+) \1\b", re.I), re.compile(r"\.\.\.\s*[a-záéíóú]"), re.compile(r"\b(?:lol|jaja+|haha+|xd|btw|tbh|pq|xq|q tal)\b", re.I)]

STOPWORDS = {"en": {"the", "and", "of", "to", "in", "is", "that", "it", "for", "with", "as", "on", "was", "are", "this", "be", "have"},
             "es": {"el", "la", "de", "que", "y", "en", "los", "las", "del", "un", "una", "por", "con", "para", "es", "se", "su", "al", "lo"}}
SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+(?=[\"'(\[¿¡]*[A-ZÁÉÍÓÚÑÜ0-9])")
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def detect_language(words: list[str]) -> str:
    lowered = [w.lower() for w in words[:2000]]
    if not lowered:
        return "unknown"
    share = {lang: sum(w in stop for w in lowered) / len(lowered) for lang, stop in STOPWORDS.items()}
    best = max(share, key=share.get)
    return best if share[best] >= 0.08 and share[best] > 1.4 * min(share.values()) else "unknown"


def sentences_of(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d+\.)\s)", text):
        block = " ".join(block.split())
        if block:
            out += [s for s in SENTENCE_END.split(block) if s.strip()]
    return out


def measure(text: str) -> dict:
    words = WORD.findall(text)
    sentences = sentences_of(text)
    lengths = [len(WORD.findall(s)) for s in sentences]
    lengths = [n for n in lengths if n > 0]
    lang = detect_language(words)
    stats = {"words": len(words), "sentences": len(lengths), "language": lang, "avg_sentence_words": None, "sentence_length_cv": None}
    if lengths:
        stats["avg_sentence_words"] = round(statistics.fmean(lengths), 1)
    if len(lengths) >= 3 and statistics.fmean(lengths) > 0:
        stats["sentence_length_cv"] = round(statistics.pstdev(lengths) / statistics.fmean(lengths), 2)
    stats["_sentences"] = sentences
    stats["_lengths"] = lengths
    return stats


def per_1000(count: int, words: int) -> float:
    return count * 1000 / words if words else 0.0


def residue_evidence(text: str, lang_hint: str) -> list[dict]:
    out: list[dict] = []
    found = [(label, m.group(0)) for pattern, label in CITATION_MARKERS for m in [pattern.search(text)] if m]
    if found:
        out.append(ev("tx-citation-markers", "generated", 9.5, 0.85, "text", T("Citation markers from a chat assistant", "Marcadores de cita de un asistente de chat"),
                      T("The text contains tokens that chat assistants insert into their answers (" + ", ".join(label for label, _ in found) + "). Nobody types these. They show that text from such a tool was pasted in, not how much of the text it is.",
                        "El texto contiene marcas que los asistentes de chat insertan en sus respuestas (" + ", ".join(label for label, _ in found) + "). Nadie las escribe a mano. Indican que se pegó texto de una herramienta así, no cuánto del texto lo es."),
                      quote=found[0][1], source="residue"))
    langs = [lang_hint] if lang_hint in ASSISTANT_STRONG_RES else ["en", "es"]
    strong = [m.group(0) for lang in langs for m in [ASSISTANT_STRONG_RES[lang].search(text)] if m]
    if strong:
        out.append(ev("tx-assistant-phrases", "generated", 9, 0.8, "text", T("Phrases only an AI assistant says about itself", "Frases que solo dice un asistente de IA sobre sí mismo"),
                      T("Found a phrase such as \"as an AI language model\" or \"regenerate response\". It can appear legitimately when a text quotes or discusses chatbots, so read the context.",
                        "Hay una frase como «como modelo de lenguaje» o «regenerate response». Puede aparecer con razón si el texto cita o habla de chatbots, así que conviene leer el contexto."),
                      quote=strong[0], source="residue"))
    soft = [m.group(0) for lang in langs for m in [ASSISTANT_SOFT_RES[lang].search(text)] if m]
    if soft and not strong:
        out.append(ev("tx-assistant-soft-phrases", "generated", 7, 0.15, "text", T("Assistant-style opening or closing", "Apertura o cierre al estilo de un asistente"),
                      T("A phrase typical of chatbot replies (\"here is a revised version\", \"let me know if you'd like me to\"). People write these too, so it weighs little.",
                        "Una frase típica de las respuestas de un chatbot («aquí tienes una versión», «si quieres, puedo»). Las personas también las escriben, así que pesa poco."),
                      quote=soft[0], source="residue"))
    placeholders = PLACEHOLDERS.findall(text)
    if placeholders:
        out.append(ev("tx-placeholders", "generated", 7, 0.2, "text", T("Template placeholders left in the text", "Marcadores de plantilla olvidados en el texto"),
                      T(f"{len(placeholders)} bracketed placeholder(s) such as [Your Name] or [Insert date]. Chatbots leave these in drafts; so do human templates and form letters.",
                        f"{len(placeholders)} marcador(es) entre corchetes como [Tu nombre] o [Insertar fecha]. Los chatbots los dejan en los borradores; también las plantillas y cartas tipo humanas."),
                      quote=placeholders[0], source="residue"))
    return out


def style_evidence(text: str, stats: dict) -> tuple[list[dict], list[dict]]:
    """Returns (evidence, measurements). Style is only scored for enough text in a language with a word list."""
    lang, words = stats["language"], stats["words"]
    measurements = [{"label": T("Words", "Palabras"), "value": str(words)}, {"label": T("Sentences", "Frases"), "value": str(stats["sentences"])},
                    {"label": T("Detected language", "Idioma detectado"), "value": lang}]
    if stats["avg_sentence_words"] is not None:
        measurements.append({"label": T("Average sentence length (words)", "Longitud media de frase (palabras)"), "value": str(stats["avg_sentence_words"])})
    if stats["sentence_length_cv"] is not None:
        measurements.append({"label": T("Sentence-length variation (CV)", "Variación de longitud de frase (CV)"), "value": str(stats["sentence_length_cv"])})
    out: list[dict] = []
    if words < MIN_WORDS_STYLE or lang not in AI_PHRASE_RES:
        why_en = (f"The text has {words} words; style statistics need at least {MIN_WORDS_STYLE}." if words < MIN_WORDS_STYLE
                  else "The language was not recognised as English or Spanish, the only ones with word lists here.")
        why_es = (f"El texto tiene {words} palabras; las estadísticas de estilo necesitan al menos {MIN_WORDS_STYLE}." if words < MIN_WORDS_STYLE
                  else "No se reconoció el idioma como inglés o español, los únicos con listas de palabras aquí.")
        out.append(ev("tx-skipped", "generated", 5, 0.0, "text", T("Style analysis skipped", "Análisis de estilo omitido"), T(why_en, why_es), source="style"))
        return out, measurements

    phrases = AI_PHRASE_RES[lang].findall(text)
    rate = per_1000(len(phrases), words)
    measurements.append({"label": T("Stock phrases per 1000 words", "Frases hechas por cada 1000 palabras"), "value": f"{rate:.1f}"})
    if rate >= PHRASE_STRONG or rate >= PHRASE_SOME or (rate == 0 and words >= 300):
        score, weight = (7.5, 0.2) if rate >= PHRASE_STRONG else (6.5, 0.12) if rate >= PHRASE_SOME else (4.0, 0.08)
        title = (T(f"Many stock phrases ({rate:.1f} per 1000 words)", f"Muchas frases hechas ({rate:.1f} por cada 1000 palabras)") if rate >= PHRASE_SOME
                 else T("No stock phrases at all", "Ninguna frase hecha"))
        detail = (T("The text uses phrases that language models overuse (\"delve into\", \"plays a crucial role\", \"in today's fast-paced world\"...). People use them too, and the list ages as models change.",
                    "El texto usa frases que los modelos de lenguaje repiten en exceso («cabe destacar», «desempeña un papel crucial», «en el mundo actual»...). Las personas también las usan, y la lista envejece al cambiar los modelos.")
                  if rate >= PHRASE_SOME else T("None of the stock phrases on the list appears, which leans slightly towards a human writer. Models can avoid them when asked to.",
                                                "No aparece ninguna de las frases hechas de la lista, lo que inclina un poco hacia una persona. Los modelos pueden evitarlas si se les pide."))
        out.append(ev("tx-phrase-density", "generated", score, weight, "text", title, detail, quote=phrases[0] if phrases else None, source="style"))

    dashes = text.count("—")
    dash_rate = per_1000(dashes, words)
    measurements.append({"label": T("Em dashes per 1000 words", "Rayas (—) por cada 1000 palabras"), "value": f"{dash_rate:.1f}"})
    if dash_rate >= EM_DASH_HIGH:
        out.append(ev("tx-em-dash", "generated", 6.5, 0.1, "text", T(f"Many em dashes ({dash_rate:.1f} per 1000 words)", f"Muchas rayas largas ({dash_rate:.1f} por cada 1000 palabras)"),
                      T("Heavy use of the em dash is a known habit of some models. Many human writers, editors and keyboards with auto-correct produce it too.",
                        "El uso intenso de la raya larga es una costumbre conocida de algunos modelos. Muchos escritores, editores y teclados con autocorrección también la producen."), source="style"))

    cv, count = stats["sentence_length_cv"], stats["sentences"]
    if cv is not None and count >= MIN_SENTENCES and (cv <= CV_LOW or cv >= CV_HIGH):
        low = cv <= CV_LOW
        out.append(ev("tx-sentence-variation", "generated", 6.5 if low else 3.5, 0.12, "text",
                      T("Very uniform sentence length" if low else "Strongly varied sentence length", "Frases de longitud muy uniforme" if low else "Frases de longitud muy variada"),
                      T(f"Sentence length varies by {cv:.2f} (CV) over {count} sentences. " + ("Models tend to write sentences of similar length; so do careful editors and formal registers." if low else "People tend to mix short and long sentences more than models do; models can imitate it on request."),
                        f"La longitud de frase varía {cv:.2f} (CV) en {count} frases. " + ("Los modelos tienden a escribir frases de longitud parecida; también lo hacen los editores cuidadosos y los registros formales." if low else "Las personas suelen mezclar frases cortas y largas más que los modelos; estos pueden imitarlo si se les pide.")),
                      source="style"))

    openers = TRANSITIONS[lang]
    sentences = stats["_sentences"]
    starts = sum(s.lower().lstrip("\"'“¿¡(").startswith(openers) for s in sentences)
    if len(sentences) >= MIN_SENTENCES:
        share = starts / len(sentences)
        measurements.append({"label": T("Sentences opening with a transition word", "Frases que empiezan con un conector"), "value": f"{share * 100:.0f}%"})
        if share >= OPENER_SHARE:
            out.append(ev("tx-transition-openers", "generated", 6.5, 0.1, "text", T(f"{share * 100:.0f}% of sentences open with a transition word", f"El {share * 100:.0f}% de las frases empieza con un conector"),
                          T("Chains of \"Moreover\", \"Furthermore\", \"However\" at the start of sentences are typical of model prose, and of school essays.",
                            "Las cadenas de «Además», «Asimismo», «Sin embargo» al principio de las frases son típicas de la prosa de los modelos, y también de las redacciones escolares."), source="style"))

    contrastive = CONTRASTIVE.findall(text)
    if len(contrastive) >= 2:
        out.append(ev("tx-contrastive-formula", "generated", 6.5, 0.1, "text", T(f"\"It's not just X, it's Y\" used {len(contrastive)} times", f"Fórmula «no es solo X, es Y» usada {len(contrastive)} veces"),
                      T("A rhetorical pattern models repeat a lot. It is also a common human device.", "Un patrón retórico que los modelos repiten mucho. También es un recurso humano común."), source="style"))

    bold = BOLD_BULLET.findall(text)
    if len(bold) >= 3:
        out.append(ev("tx-bold-bullets", "generated", 6.5, 0.1, "text", T(f"{len(bold)} bullets with a bold lead-in", f"{len(bold)} viñetas con un arranque en negrita"),
                      T("Lists of \"**Term**: explanation\" are the default shape of chat-assistant answers. Documentation and slides use them too.",
                        "Las listas de «**Término**: explicación» son la forma por defecto de las respuestas de los asistentes de chat. La documentación y las diapositivas también las usan."), source="structure"))

    irregular = sum(len(p.findall(text)) for p in IRREGULAR)
    irregular_rate = per_1000(irregular, words)
    measurements.append({"label": T("Informal irregularities per 1000 words", "Irregularidades informales por cada 1000 palabras"), "value": f"{irregular_rate:.1f}"})
    if irregular_rate >= 3.0:
        out.append(ev("tx-irregularities", "generated", 3.0, 0.1, "text", T("Typos, slang and irregular punctuation", "Erratas, jerga y puntuación irregular"),
                      T("Missing apostrophes, doubled words, \"!!\" and chat slang are rare in unedited model output. A model can be told to imitate them.",
                        "Apóstrofos que faltan, palabras repetidas, «!!» y jerga de chat son raros en la salida sin editar de un modelo. Se le puede pedir a un modelo que los imite."), source="style"))

    # mixed authorship: does the density of AI-style markers jump between parts of the text?
    chunks, current, current_words = [], [], 0
    for sentence in sentences:
        current.append(sentence)
        current_words += len(WORD.findall(sentence))
        if current_words >= CHUNK_WORDS:
            chunks.append(" ".join(current))
            current, current_words = [], 0
    if len(chunks) >= 3:
        index = [(len(AI_PHRASE_RES[lang].findall(c)) + c.count("—") * 0.5 + len(CONTRASTIVE.findall(c))) * 100 / max(1, len(WORD.findall(c))) for c in chunks]
        measurements.append({"label": T("Style-marker density by section (per 100 words)", "Densidad de marcas de estilo por tramo (por cada 100 palabras)"), "value": ", ".join(f"{v:.1f}" for v in index)})
        if max(index) >= 1.5 and min(index) <= 0.2:
            out.append(ev("tx-style-shift", "ai_edited", 6.5, 0.1, "text", T("The style changes between sections", "El estilo cambia entre tramos"),
                          T(f"Some stretches carry many AI-typical markers and others almost none (range {min(index):.1f}-{max(index):.1f} per 100 words). That can mean a human text with pasted or polished parts. It can also be one author whose register changes.",
                            f"Unos tramos llevan muchas marcas típicas de IA y otros casi ninguna (rango {min(index):.1f}-{max(index):.1f} por cada 100 palabras). Puede significar un texto humano con partes pegadas o pulidas. También puede ser un solo autor cuyo registro cambia."), source="style"))
    return out, measurements


def invisible_evidence(text: str) -> tuple[list[dict], list[dict]]:
    counts = {name: text.count(char) for char, name in INVISIBLE.items() if text.count(char)}
    if not counts:
        return [], []
    summary = ", ".join(f"{n}× {name}" for name, n in counts.items())
    item = ev("tx-invisible-chars", "generated", 5, 0.0, "text", T("Invisible or unusual characters", "Caracteres invisibles o inusuales"),
              T(f"Found {summary}. Some chat tools and many web pages, word processors and copy-paste paths add these. It is shown for context and is not scored.",
                f"Hay {summary}. Algunas herramientas de chat y muchas páginas web, procesadores de texto y vías de copiar y pegar los añaden. Se muestra como contexto y no puntúa."), source="structure")
    return [item], [{"label": T("Invisible characters", "Caracteres invisibles"), "value": summary}]


def chat_markdown_evidence(text: str, name: str) -> list[dict]:
    if name.lower().endswith((".md", ".markdown", ".rst", ".html", ".htm")):
        return []
    hits = len(CHAT_MARKDOWN.findall(text))
    if hits >= 4:
        return [ev("tx-chat-markdown", "generated", 6.5, 0.1, "text", T("Chat-style Markdown in a plain text", "Markdown de chat en un texto plano"),
                   T(f"{hits} Markdown headings or **bold** spans in a file that is not Markdown. That is what you get when a chat answer is pasted as it was rendered. Plain-text authors rarely type it.",
                     f"{hits} encabezados Markdown o **negritas** en un archivo que no es Markdown. Es lo que queda al pegar una respuesta de chat tal como se mostraba. Quien escribe en texto plano rara vez lo teclea."), source="structure")]
    return []


def analyze(text: str, name: str = "text") -> dict:
    data = text.encode("utf-8", "replace")
    stats = measure(text)
    evidence = residue_evidence(text, stats["language"])
    inv_items, inv_measure = invisible_evidence(text)
    style_items, measurements = style_evidence(text, stats)
    evidence += style_items + inv_items + chat_markdown_evidence(text, name)
    seen, unique = set(), []
    for item in evidence:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    limits = [
        T("There is no language model behind this analysis, so no perplexity or token-probability score. Those are the strongest statistical signals and need a model.",
          "Detrás de este análisis no hay ningún modelo de lenguaje, así que no hay puntuación de perplejidad ni de probabilidad de palabras. Son las señales estadísticas más fuertes y necesitan un modelo."),
        T("Style signals are very unreliable. Non-native writers, formal registers and heavily edited text are flagged far more often than native, informal writing, and the word lists age as models change.",
          "Las señales de estilo son muy poco fiables. Quien escribe en un segundo idioma, los registros formales y los textos muy editados se marcan mucho más que la escritura nativa e informal, y las listas de palabras envejecen al cambiar los modelos."),
        T("Only English and Spanish have word lists.", "Solo el inglés y el español tienen listas de palabras."),
        T("A text can be AI-written and show none of this: asking a model to avoid its habits, or editing the result by hand, removes most of it.",
          "Un texto puede estar escrito por IA y no mostrar nada de esto: pedirle al modelo que evite sus costumbres, o editar el resultado a mano, elimina casi todo."),
    ]
    return {"tool": "analyze_text", "version": VERSION,
            "file": {"name": name, "format": "text", "modality": "text", "words": stats["words"], "sentences": stats["sentences"],
                     "language": stats["language"], "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)},
            "evidence": unique, "measurements": measurements + inv_measure, "limits": limits,
            "coverage": {"checked": [T("residue left by chat assistants", "restos que dejan los asistentes de chat"), T("stock phrases and style statistics", "frases hechas y estadísticas de estilo"),
                                     T("invisible characters", "caracteres invisibles")],
                         "not_checked": [T("perplexity or any language-model score", "perplejidad o cualquier puntuación de un modelo de lenguaje"),
                                         T("who actually wrote it: a comparison with the author's other texts", "quién lo escribió de verdad: una comparación con otros textos del autor"),
                                         T("facts and references (the model's reading, see SKILL.md)", "los datos y las referencias (lo hace el modelo al leer, ver SKILL.md)")]}}


def read_text(source: str) -> tuple[str, str]:
    if source == "-":
        raw, name = sys.stdin.buffer.read(MAX_BYTES + 1), "stdin"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            sys.exit(f"error: {path} is not a file")
        with path.open("rb") as handle:
            raw, name = handle.read(MAX_BYTES + 1), path.name
    truncated = len(raw) > MAX_BYTES
    raw = raw[:MAX_BYTES]
    if b"\x00" in raw[:4096]:
        sys.exit("error: this looks like a binary file, not text. Extract the text first (for a PDF or Word file, ask the model to read it and save the text).")
    text = raw.decode("utf-8-sig", "replace")
    if truncated:
        print(f"note: only the first {MAX_BYTES // (1024 * 1024)} MB were read", file=sys.stderr)
    return text, name


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract AI-provenance evidence from a text (static, stdlib only).")
    parser.add_argument("text", help="a text file, or - for standard input")
    parser.add_argument("--out", help="write the JSON to this .json file instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = parser.parse_args()
    text, name = read_text(args.text)
    result = analyze(text, name)
    if args.out:
        out = checked_output_path(args.out, ".json", args.force)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(result['evidence'])} evidence items)")
    else:
        dump(result)


if __name__ == "__main__":
    main()
