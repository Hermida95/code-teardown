# Looking at the image yourself

This is the weakest layer of evidence, so it is capped and must be concrete. For every observation say **what**, **where** (a region of the image) and **how sure**. If you cannot point to a place, drop it.

## Kinds and their weight caps

| `kind` | Cap | Look for |
| --- | --- | --- |
| `known_watermark` | 0.6 | A vendor's visible mark: the Gemini sparkle in a corner, DALL·E's row of coloured squares (bottom right), "Made with Google AI", Sora, Runway or Kling logos. Only count marks you actually recognise |
| `anatomy_text_errors` | 0.35 | Hands with wrong finger counts or fused fingers, teeth merging, mismatched earrings or glasses, asymmetric eyes with different highlights, **text that looks like letters but spells nothing**, garbled logos |
| `physics_lighting` | 0.3 | Shadows pointing different ways, reflections that do not match the scene, objects that merge into each other, impossible perspective or stairs, floating or duplicated items |
| `natural_cues` | 0.3 | Evidence **against** AI: sensor-like grain that varies with brightness, coherent readable text, consistent EXIF-like context, lens artefacts that match the scene, small mess a generator tends to tidy. Include these when they are really there |
| `texture_style` | 0.25 | Waxy skin without pores, over-smooth gradients, uniform "glossy" rendering, repeated patterns in hair, fabric or foliage |
| `composition` | 0.2 | Too-perfect centring, generic stock-like scene, background that dissolves into vague shapes |
| `other` | 0.2 | Anything that does not fit; explain it |

Choose `claim: "ai_edited"` when you see a local mismatch in an otherwise natural image (a patch with different grain, a seam, a region sharper or smoother than its surroundings, an object whose lighting does not match).

## Scores

0-2: points to a real image. 3-4: leans real. 5: neutral (do not record neutral items). 6-7: leans AI. 8-10: strongly points to AI. A visual observation will never weigh above its cap, so do not inflate the weight; spend the effort on getting the score and the `where` right.

## Common false alarms

- **Heavy JPEG compression or downscaling** smooths skin and textures and looks "AI". Say so and lower the score.
- **Stylised art, illustration, 3D renders and anime** are not photos; most photo heuristics do not apply.
- **Phone beautify modes, HDR and denoising** give waxy faces and tidy backgrounds on real photos.
- **AI upscalers** change the texture of a real photo. That is edited, not generated.
- **Text in non-Latin scripts** you cannot read is not evidence of garbled text.
- A real photo of an AI-looking subject (a wax figure, a mural, a CGI billboard).

## Reporting honestly

If the image is smaller than about 400 px, heavily compressed, or a screenshot, say the visual layer is too weak and record at most one or two observations. It is fine, and often right, for the visual layer to say little. Never invent a sign to fill the list.
