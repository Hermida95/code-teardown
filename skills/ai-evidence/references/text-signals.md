# Text signals

Everything here comes from `analyze_text.py` (standard library only) and from what the model notices when it reads the text. No language model is behind the script, so there is **no perplexity or token-probability score**; those are the strongest statistical signals in this field and they need a model.

**Read this first.** Text is the weakest ground in this skill. Two different kinds of evidence exist and they are not alike:

- **Residue** is something a chat assistant leaves behind that a person does not type. It is strong, but it only shows that assistant text was pasted in, not how much of the text it is.
- **Style** is a habit of how models write, and it is also a habit of many people. It is weak, it ages as models change, and it flags non-native writers and formal registers far more than native, informal writing. Style items are capped as a group (total weight 0.5 per question), so many of them together still give only low confidence.

A text can be fully AI-written and show none of this: asking a model to avoid its habits, or editing its output, removes most of it. The absence of signs is never evidence of a human author.

## Residue (weights 0.15-0.85)

| id | Score / weight | What was found | How it misleads |
| --- | --- | --- | --- |
| `tx-citation-markers` | 9.5 / 0.85 | Tokens a chat interface inserts: `【4:0†source】`, `oaicite`, `turn0search0`, `utm_source=chatgpt.com` in a link | Shows a paste from such a tool, not what share of the text is AI. Not conclusive on purpose |
| `tx-assistant-phrases` | 9 / 0.8 | "as an AI language model", "my knowledge cutoff", "regenerate response", "como modelo de lenguaje"... | A text that quotes or discusses chatbots can contain them. Read the context |
| `tx-assistant-soft-phrases` | 7 / 0.15 | "here is a revised version", "let me know if you'd like me to", "I hope this helps", "aquí tienes una versión" | People write all of these. Counted only when no strong phrase was found |
| `tx-placeholders` | 7 / 0.2 | `[Your Name]`, `[Insert date]`, `[Nombre]` left in the text | Form letters and templates written by hand have them too |
| `tx-chat-markdown` | 6.5 / 0.1 | Four or more Markdown headings or `**bold**` spans in a file that is not Markdown | People write Markdown into plain-text fields as well |
| `tx-invisible-chars` | 5 / 0 | Zero-width spaces, narrow no-break spaces, soft hyphens, stray byte-order marks | Shown, never scored: web pages, word processors and paste paths add them |

## Style (needs 150+ words, English or Spanish)

All thresholds are reasoned guesses, not calibrated values.

| id | Score / weight | Condition | How it misleads |
| --- | --- | --- | --- |
| `tx-phrase-density` | 7.5 / 0.2 at 8+ per 1000 words; 6.5 / 0.12 at 4+; 4 / 0.08 for none in 300+ words | Stock phrases models overuse ("delve into", "plays a crucial role", "in today's fast-paced world", "cabe destacar", "en el mundo actual"...) | People use them, school essays are full of them, and the list ages. A model told to avoid them does |
| `tx-em-dash` | 6.5 / 0.1 | 4+ em dashes per 1000 words | Editors, autocorrect and many authors use the em dash |
| `tx-sentence-variation` | 6.5 / 0.12 when the coefficient of variation is 0.35 or less; 3.5 / 0.12 at 0.75 or more | Over at least 12 sentences | Careful editors and formal registers are uniform; models can imitate variety |
| `tx-transition-openers` | 6.5 / 0.1 | 12 % or more of sentences open with a connector (Moreover, Furthermore, Además, Asimismo...) | Typical of school essays and of translations |
| `tx-contrastive-formula` | 6.5 / 0.1 | "It's not just X, it's Y" at least twice | A common human rhetorical device |
| `tx-bold-bullets` | 6.5 / 0.1 | Three or more bullets of the form "**Term**: explanation" | Documentation and slides use the same shape |
| `tx-irregularities` | 3 / 0.1 | Missing apostrophes, doubled words, "!!", chat slang, 3+ per 1000 words | A model can be told to imitate them |
| `tx-style-shift` | 6.5 / 0.1 (claim: mixed or polished) | At least three sections of ~120 words where one has many AI-typical markers and another almost none | One author whose register changes looks the same |
| `tx-skipped` | 5 / 0 | Under 150 words, or a language without a word list | Explains why no style item appears |

## What the model adds

The model reads the text and records up to six observations (`--visual observations.json`). Their weight is capped by kind:

| `kind` | Cap | Look for |
| --- | --- | --- |
| `fabricated_references` | 0.35 | Citations that do not seem to exist, wrong venues or years, invented quotes or statistics, links that look constructed. The most telling model failure; check only what you can check without going online unless asked |
| `voice_and_specificity` | 0.3 | A recognisable personal voice, specific detail, errors a person makes, opinions that commit. Points **away** from AI. Models can fake it, so it stays weak |
| `generic_content` | 0.25 | Balanced both-sides paragraphs without a position, claims that fit any topic, a conclusion that restates the introduction |
| `text_other` | 0.2 | Anything else; explain it |

## What is not here

- **Perplexity and burstiness measured by a model.** Needs a language model; planned only as an opt-in module.
- **Authorship comparison** against other texts by the same person. It is the most robust approach to the question "did this person write this", and it needs a baseline of their writing.
- **Other languages.** Only English and Spanish have word lists; a text in another language gets residue checks and measurements only.
- **Code.** Not covered yet.

## Using it responsibly

Do not use a text result to accuse anyone. For schoolwork, applications or journalism, look at drafts, version history and sources, and talk to the person. A result that says "not enough evidence" is often the honest one, and the report says what could not be checked.

## Running it

```
analyze_text.py <file|-> --out $WORK/evidence.json
```
