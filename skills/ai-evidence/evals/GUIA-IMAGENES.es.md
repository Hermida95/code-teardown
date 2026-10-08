# Guía práctica: reunir las imágenes de prueba

Para qué sirve: para que [`evaluate_dataset.py`](README.md) pueda decirte, con números, cuántas fotos reales marca por error y cuántas imágenes de IA detecta. Esta guía es la versión paso a paso. La de referencia, en inglés, es [COLLECTING.md](COLLECTING.md).

> Esto es para ver, comprobar y aprender sobre la herramienta. No acusa a nadie ni mide a ninguna persona: mide cuánto coincide la herramienta con las etiquetas que tú pones.

**Tiempo:** unas 3 horas en total, repartidas en 5 sesiones cortas. **Resultado:** unas 325 imágenes.

## La idea en una frase

Las imágenes de prueba tienen que haber hecho **el mismo viaje que las que querrás comprobar en la vida real**. Casi todo lo que te llega ha pasado por WhatsApp, una red social o una captura de pantalla, y por eso ha perdido los metadatos. Si tu conjunto solo tiene originales intactos, saldrá un resultado bonito que no significa nada.

## Antes de empezar

1. **Prepara las carpetas** (una sola vez):

```bash
mkdir -p ~/ai-eval-data/real/{camara-original,whatsapp,capturas,retocadas}
mkdir -p ~/ai-eval-data/generated ~/ai-eval-data/edited
```

2. **Regla de oro de las etiquetas:** una imagen solo entra si **estás seguro** de lo que es. Si dudas, fuera. Una etiqueta equivocada parece un error de la herramienta.
3. **Privacidad:** son fotos, y las fotos de otras personas son datos personales. Usa las tuyas, o las de quien te dé permiso. El conjunto se queda en tu ordenador: no lo subas a ningún sitio. La carpeta `evals/datasets/` ya está ignorada por git, y la herramienta no sube nada.
4. **No renombres** las imágenes generadas tal como las descarga la herramienta (el nombre por defecto es una pista legítima). Si prefieres renombrarlas, ejecuta luego con `--no-filename`.

## El plan, en cinco sesiones

| Sesión | Qué | Cuántas | Tiempo |
| --- | --- | --- | --- |
| A | Fotos reales originales y capturas de pantalla | 50 + 25 | 45 min |
| B | Fotos reales enviadas por mensajería y retocadas | 50 + 25 | 45 min |
| C | Imágenes generadas, con tres o más generadores | 100 + 25 | 60 min |
| D | Fotos tuyas editadas con herramientas de IA | 50 | 30 min |
| E | Revisar y ejecutar | – | 15 min |

Mínimo para empezar a probar el proceso (**piloto**, 20 minutos): 20 reales originales + 20 por WhatsApp + 20 generadas de dos generadores. Con eso no saldrán pesos sugeridos (hacen falta unas 100 por clase), pero verás que todo funciona antes de invertir las 3 horas.

## Sesión A: reales originales y capturas

**`real/camara-original/` (50 fotos).** Fotos tuyas de verdad, variadas: personas (con permiso), paisajes, comida, interiores, noche, carteles con texto, primeros planos.

- **iPhone:** por defecto guarda en HEIC, y la herramienta **no lo analiza a fondo** (solo busca marcas de procedencia genéricas). Dos formas de evitarlo: en *Ajustes > Cámara > Formatos* elige **Más compatible** para fotos nuevas, o desde la app Fotos del Mac usa *Archivo > Exportar > Exportar N fotos* y elige **JPEG**, con los metadatos incluidos.
- **Android:** suelen ser JPEG, vale tal cual.
- **Cámara:** JPEG tal cual sale. No abras y guardes de nuevo las fotos en ningún programa: se pierden metadatos.
- Copia los archivos con el Finder, **sin recomprimirlos**.
- Usa fotos de varios días y de varias situaciones, no 50 de la misma escena.

Mezcla también las que hizo tu móvil con HDR, modo noche o retrato: es fotografía computacional **real** y debe estar en el conjunto. No las excluyas.

**`real/capturas/` (25).** Haz 25 capturas de pantalla de fotos o webs: en el Mac `Cmd+Shift+4` (PNG), en el móvil la captura normal.

## Sesión B: mensajería y retoque

**`real/whatsapp/` (50).** Elige 50 de las fotos de la sesión A (no las muevas: cópialas), envíatelas por **la app que usas de verdad** (WhatsApp, Telegram...) y guarda lo que **llega**.

- Descarga desde la propia conversación (WhatsApp Web o de escritorio es lo más cómodo), no copies y pegues.
- Si la app te pregunta por la calidad, usa la que usas normalmente.
- Si usas dos apps, haz una mitad con cada una y pon cada una en su carpeta (`whatsapp`, `telegram`): el informe separará los falsos positivos por origen.

**`real/retocadas/` (25).** Pasa 25 fotos por el retoque **convencional** que usas: filtros del móvil, Lightroom, Snapseed, ajustes de la app Fotos.

> ⚠️ **Sin funciones de IA.** Herramientas como el borrado de objetos, «Clean Up», «Magic Eraser», «Magic Editor» o el relleno generativo son IA y **no** van aquí: van a la sesión D. Si dudas de si una función es IA, trátala como IA.

## Sesión C: imágenes generadas

**Elige al menos tres generadores** distintos. La mezcla decide lo que los números pueden decir. Conviene que haya uno que escriba credenciales de contenido (muchos de los grandes servicios incluyen C2PA, **compruébalo tú**) y uno local o abierto (Stable Diffusion con A1111 o ComfyUI), que suele guardar los parámetros dentro del PNG.

Para cada generador:

- Usa el **botón de descarga** de la herramienta. No arrastres la imagen desde el navegador, no la copies y pegues, no hagas captura y no la abras para «guardar como»: cualquiera de esas cosas puede cambiar el formato o quitar los metadatos.
- Comprueba la **extensión** del archivo que baja (PNG, WebP, JPEG): es lo que se evalúa.
- Mira los términos de uso: casi todos permiten guardar tus propias imágenes para probar, y ninguna de estas imágenes debe publicarse.

**Usa los mismos temas que en las reales** (así no mides el tema en vez de la IA). Una lista de 24 prompts para reutilizar en todos los generadores, con ligeras variaciones:

| Tipo | Prompts |
| --- | --- |
| Personas | retrato de una persona mayor sonriendo · grupo de cuatro amigos en una terraza · niño jugando con un perro en un parque · persona leyendo en un tren |
| Paisajes | montaña con niebla al amanecer · playa con acantilados al atardecer · calle de pueblo con lluvia · bosque en otoño |
| Cosas | plato de pasta en una mesa de madera · taza de café junto a una ventana · zapatillas deportivas sobre cemento · bicicleta apoyada en una pared |
| Interiores y noche | salón con sofá y estanterías · cocina moderna por la mañana · calle con luces de neón de noche · tienda con escaparate iluminado |
| Detalles difíciles | primer plano de unas manos sosteniendo una taza · cartel de una tienda con texto legible · reflejo en un escaparate · mercado con mucha gente al fondo |
| Otros estilos | ilustración plana de una ciudad · captura de una interfaz de app de móvil · foto de producto sobre fondo blanco · fotografía antigua en blanco y negro |

Haz **100 imágenes entre todos los generadores** (por ejemplo 35 + 35 + 30), con los prompts repartidos. Guárdalas en `generated/<generador>/`, por ejemplo `generated/chatgpt/`, `generated/midjourney/`, `generated/sd-local/`.

**25 más, «compartidas».** Toma una parte y haz el viaje que haría una imagen real: envíalas por mensajería y guarda lo que llega, en `generated/<generador>-compartida/`. (`--augment strip` simula esto, pero las copias reales son mejores.)

## Sesión D: fotos tuyas editadas con IA

Toma fotos reales tuyas y cámbialas con una herramienta de IA: relleno o ampliación generativa, borrado y sustitución de objetos, cambio de fondo, cambio de cara o pelo, ampliación con IA. Prueba **al menos tres herramientas distintas**. Guarda el resultado **tal como lo entrega la herramienta**, en `edited/<herramienta>/` (por ejemplo `edited/photoshop-genfill/`, `edited/movil-borrador/`). La foto original no va en `real/` si es un archivo distinto del editado, pero **nunca** pongas la original y su edición como el mismo contenido.

## Cosas que estropean un conjunto (y cómo evitarlas)

- **Que todas las reales sean JPEG y todas las generadas PNG.** La herramienta puede parecer buena solo por el formato. Haz que cada grupo tenga los dos formatos, o compara con la condición `--augment strip`.
- **Duplicados y recortes de la misma foto en grupos distintos.** Se cuela información. Una versión por foto.
- **Fotos de un solo día o de un solo lugar.** Varía.
- **Que el nombre delate la etiqueta.** Ejecuta una vez con y otra sin `--no-filename`.
- **Mirar el test antes de tiempo.** El script reparte las imágenes en dev (70 %) y test (30 %). Los pesos se sugieren con dev; las cifras principales son las de test. Si cambias pesos mirando test, deja de valer.

## Sesión E: revisar y ejecutar

1. **Mira cómo vas** (no ejecuta nada, no escribe nada):

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-data --check
```

Te dice cuántas imágenes hay por etiqueta y origen, qué falta y si hay duplicados. Si te pide «at least 100» por clase, es para que el 30 % de test llegue a las 30 que necesita el informe.

2. **Ejecuta la evaluación** (añade `--pixels` si tienes Pillow y NumPy):

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-data --out-dir ~/ai-eval-out --augment strip
```

Si no tienes Pillow, `--augment strip` lo necesita: usa `uv run --with pillow --with numpy python ...` en lugar de `python3 ...`.

3. **Lee `~/ai-eval-out/report.md`** de arriba abajo, empezando por la tasa de falsos positivos en imágenes reales. Esa es la cifra que importa. Mira el extremo alto de su intervalo, no el valor puntual: con 0 errores en 100 fotos reales, la cota superior sigue siendo de un 4 % aproximadamente.

4. **Qué mirar a continuación:** cuántas veces dice «sin pruebas suficientes» en cada grupo (en las de WhatsApp debería ser casi siempre, y es la respuesta correcta); y la tabla de pruebas por origen, que muestra qué señal salta en qué fotos reales.

Cuando lo tengas, pásame el resultado de `--check` o las primeras líneas del `report.md` y lo leemos juntos.
