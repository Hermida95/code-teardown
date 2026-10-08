# Security

ai-evidence reads untrusted image files, so its own safety matters.

## What it guarantees

- **No execution.** The image is parsed as bytes by hand-written readers with bounds checks. The optional pixel module is the only code that decodes an image (with Pillow, which is a large attack surface of its own): it refuses files over 64 MB or 50 million pixels before decoding, turns decompression-bomb warnings into errors, and runs as a separate process so a failure cannot take the rest down. Keep Pillow up to date, or skip the module for images you do not trust.
- **Bounded work.** File reads, text fields, decompression (zlib output is capped), chunk counts and EXIF entries all have limits, so crafted files cannot exhaust memory or loop.
- **Content is data.** The skill tells the model to treat text found in the image or its metadata as data, never as instructions.
- **Contained writes.** Scripts write only the `.json` / `.html` you name and refuse to overwrite without `--force`.
- **Self-contained report.** No scripts or external resources; everything from the file is HTML-escaped; a Content-Security-Policy forbids loading anything.
- **No upload.** Nothing is sent anywhere. The skill instructs the model not to send the image to third-party detectors unless the user asks.

## What it does not guarantee

- It is not a forensic tool and its output is not evidence for legal use, nor grounds to accuse or sanction anyone.
- It does not verify C2PA signatures; a declared source type is a claim, not a validated fact.
- Permission prompts belong to your agent client, not to this project; the skill does not pre-approve its scripts.

## Reporting a problem

Please open a private security advisory on the GitHub repository, or an issue that describes the impact without publishing a working exploit.
