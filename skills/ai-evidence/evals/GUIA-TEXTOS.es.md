# Guía práctica: reunir los textos de prueba

Para qué sirve: para que [`evaluate_dataset.py`](README.md) pueda decirte, con números, cuántos textos humanos marca por error y cuántos textos de IA detecta. Esta guía es la versión paso a paso. La de referencia, en inglés, es [COLLECTING.md](COLLECTING.md#collecting-texts).

> Esto es para ver, comprobar y aprender sobre la herramienta. No acusa a nadie ni mide a ninguna persona: mide cuánto coincide la herramienta con las etiquetas que tú pones. Y en texto es la modalidad **más débil**: las señales de estilo marcan mucho más a quien escribe en un segundo idioma. Medirlo es justo para lo que sirve esta guía.

**Tiempo:** unas 4 horas, repartidas en 6 sesiones cortas (más lo que tarden tus voluntarios). **Resultado:** unos 390 textos.

## La idea en una frase

Los textos de prueba tienen que parecerse a los que querrás comprobar en la vida real, y los **humanos** tienen que incluir a la gente a la que la herramienta más probablemente se equivoque: quien escribe en un segundo idioma, quien escribe con un registro formal, quien escribe corto e informal. Si el conjunto solo tiene humanos que escriben «como escribiría una IA», o solo textos coloquiales, el resultado será bonito y no significará nada.

## Antes de empezar

1. **Prepara las carpetas** (una sola vez):

```bash
mkdir -p ~/ai-eval-texts/real/{nativos,segundo-idioma,informal,escritos-en-word}
mkdir -p ~/ai-eval-texts/generated/{chat-pegado,limpio}
mkdir -p ~/ai-eval-texts/edited/{pulido-por-ia,mezcla}
```

2. **Un texto por archivo**, en **texto plano UTF-8** (`.txt`). Sin título ni firma dentro, sin nombres reales (cámbialos por «Ana», «la empresa»...).
3. **Regla de oro de las etiquetas:** un texto solo entra si **estás seguro** de quién lo escribió. Si dudas, fuera. Una etiqueta equivocada parece un error de la herramienta.
4. **Qué entiende la herramienta:** en inglés y español hace análisis de estilo, y solo a partir de **150 palabras**. En otros idiomas, o en textos más cortos, solo busca los restos que deja un chatbot. Plantéate cuántos textos cortos quieres: sirven para ver que la herramienta **se abstiene** en lugar de adivinar.
5. **Privacidad y ética:** los textos de otras personas son datos personales. Usa los tuyos, o los de gente que te dé su permiso expreso. **Nunca** uses trabajos de alumnos o compañeros, correos privados o textos confidenciales. El conjunto se queda en tu ordenador: no lo subas a ningún sitio. La carpeta `evals/datasets/` ya está ignorada por git.

## El plan, en seis sesiones

| Sesión | Qué | Cuántos | Tiempo |
| --- | --- | --- | --- |
| A | Textos humanos de nativos (tuyos de antes de 2022) | 100 | 60 min |
| B | Textos humanos de gente que escribe en un segundo idioma | 60 | lo que tarden tus voluntarios + 15 min |
| C | Textos humanos cortos e informales, y escritos en Word | 40 + 20 | 30 min |
| D | Textos generados por IA, pegados del chat y limpios | 60 + 60 | 60 min |
| E | Textos humanos pulidos por IA, y mezclas | 50 | 40 min |
| F | Revisar y ejecutar | – | 15 min |

**Piloto (20 minutos):** 20 textos tuyos de 200–300 palabras + 20 de IA pegados del chat + 20 de IA «limpios». Con eso no saldrán pesos sugeridos (hacen falta unos 100 por clase), pero verás que todo funciona antes de invertir las 4 horas.

## Sesión A: humanos nativos

**`real/nativos/` (100).** Lo más seguro para probar que algo lo escribió una persona es que sea **anterior a 2022**: correos tuyos antiguos (sin datos privados), redacciones, trabajos, entradas de un blog tuyo, mensajes largos, cartas, reseñas. Reparte las longitudes: unos 30 de menos de 150 palabras, 40 de 150–400, 30 de más de 400.

- Copia el texto en un `.txt` en **texto plano**. En el Mac: abre TextEdit, *Formato > Convertir en texto plano*, y pega con `Cmd+Opción+Mayús+V` (pegar sin formato).
- Quita saludos, firmas y nombres reales.
- No uses textos de autores famosos de internet: pueden estar en los datos de entrenamiento de un modelo, y los clásicos de dominio público tienen un estilo antiguo que no representa nada.

## Sesión B: humanos que escriben en un segundo idioma

**`real/segundo-idioma/` (60).** Es el grupo **más importante** del conjunto. Los detectores de texto marcan a estas personas mucho más que a los nativos, y un conjunto sin ellos esconde el principal daño posible.

- Pide a **voluntarios** que escriban sobre los **mismos temas** que los demás (la lista de abajo). Texto escrito por ellos, sin ayuda de ningún asistente, corrector con IA ni traductor.
- Que escriban en el idioma que dominan menos (inglés o español).
- Pídeles su permiso **expreso** y explícales para qué es. Un texto que puedes enviarles tal cual:

> «Estoy comprobando una herramienta que mira si un texto parece escrito con IA, para ver si se equivoca con gente que escribe en un segundo idioma. Te pido un texto de 150–300 palabras sobre este tema, escrito por ti sin ayuda. Solo lo usaré en mi ordenador para esta comprobación, sin tu nombre, y lo borraré si me lo pides. No se publicará ni se usará para evaluarte a ti.»

- Varía los niveles de dominio del idioma.
- Si pueden, que lo escriban en un editor simple, sin corrector.

## Sesión C: informales cortos, y los escritos en Word

**`real/informal/` (40).** Textos cortos y sueltos tuyos: notas, mensajes largos, comentarios, respuestas a un foro, antes de 2022. Mejor si son de menos de 150 palabras: ahí la herramienta solo busca restos y debería abstenerse.

**`real/escritos-en-word/` (20).** Textos tuyos, antiguos, escritos en **Word o Pages con autocorrección activada**. El corrector convierte `--` en raya larga (—) y las comillas rectas en tipográficas. La raya larga es una de las señales de estilo de la herramienta, así que esta carpeta mide si esa señal **marca por error** a quien escribe en un procesador de texto. Es una carpeta pequeña con mucho valor.

## Sesión D: textos generados por IA

**Dos formas de obtener texto de IA.** Las dos importan:

- **`generated/chat-pegado/` (60): pegado desde el chat.** Copias la respuesta de la interfaz del chat y la guardas. Aquí pueden aparecer restos (marcas de cita, frases como «claro, aquí tienes»). Para unos cuantos, activa la búsqueda web: algunos servicios añaden marcas de cita y enlaces con parámetros de seguimiento.
- **`generated/limpio/` (60): limpio.** El mismo tipo de texto, pero obtenido por la API o el *playground* del servicio, o tras **borrar a mano** cualquier resto. Es lo que entregaría alguien cuidadoso, y donde la herramienta casi siempre dirá «sin pruebas suficientes».

**Usa los mismos temas y las mismas longitudes que los humanos.** Si los textos generados son más largos que los humanos, o de otros temas, la herramienta puede parecer buena solo por eso. El comando `--check` avisa de una diferencia de longitud grande.

**Varias instrucciones**, para saber cuánto cambian el resultado:

1. Por defecto, solo el tema.
2. «Escribe de forma natural, como lo haría una persona.»
3. «Escribe como un estudiante universitario, con algún fallo menor.»
4. «Evita los clichés y las frases habituales de los asistentes.»
5. «Escríbelo en tres párrafos cortos.»

Es esperable que la herramienta lo haga **peor** con la 3 y la 4: justo eso quieres medir.

**Varios asistentes**, si puedes: al menos dos o tres.

**Veinticuatro temas**, para humanos y para IA (usa los mismos en los dos grupos):

| Español | English |
| --- | --- |
| Describe la ciudad donde vives | Describe the town or city you live in |
| Cuenta un viaje que recuerdes | Tell about a trip you remember |
| Opina sobre el uso del móvil en los colegios | Give your view on phones in schools |
| Explica cómo preparar tu plato favorito | Explain how to make your favourite dish |
| Escribe un correo pidiendo una prórroga | Write an email asking for an extension |
| Reseña una película o un libro | Review a film or a book |
| Una carta de motivación corta | A short motivation letter |
| Resume el argumento de algo que hayas leído | Summarise something you have read |
| Explica qué es la inflación a un niño | Explain inflation to a child |
| Escribe una queja a una compañía aérea | Write a complaint to an airline |
| Habla de un hobby y por qué te gusta | Talk about a hobby and why you like it |
| Da consejos para estudiar mejor | Give advice on studying better |

Con 12 temas en cada idioma sobran para repartir: cada persona o cada instrucción usa una parte distinta.

## Sesión E: humanos pulidos por IA, y mezclas

**`edited/pulido-por-ia/` (30).** Toma **textos tuyos** (de la sesión A) y pide a un asistente: «mejora este texto». Pega el resultado. Es el caso de quien escribe y luego «lo arregla con IA».

**`edited/mezcla/` (20).** Toma un texto tuyo y sustituye **uno o dos párrafos** por texto generado por IA (los restos de la herramienta que quieras dejar o quitar). Sirve para medir la señal de «el estilo cambia entre tramos».

Etiqueta estas dos carpetas como `edited`, no como `generated`.

## Cosas que estropean un conjunto (y cómo evitarlas)

- **Longitud y tema distintos entre humanos e IA.** La herramienta parece buena solo por eso. Iguala longitudes y temas.
- **Humanos que «suenan a IA».** Si solo incluyes humanos coloquiales, ocultas los falsos positivos. Incluye registro formal, textos académicos, cartas y gente que escribe en un segundo idioma.
- **Copiar de Word o de una web con formato.** Pega siempre como texto plano. Revisa que no queden viñetas, negritas o enlaces de la fuente.
- **Duplicados.** Una versión por texto.
- **Textos de menos de 150 palabras sin avisar.** Sirven, pero la herramienta no los puntúa por estilo. Mantén una mezcla.
- **Mirar el test antes de tiempo.** El script reparte los textos en dev (70 %) y test (30 %). Los pesos se sugieren con dev; las cifras principales son las de test. Si cambias pesos mirando test, deja de valer.

## Sesión F: revisar y ejecutar

1. **Mira cómo vas** (no ejecuta nada, no escribe nada):

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-texts --check
```

Te dice cuántos textos hay por etiqueta y origen, la **mediana de palabras** de humanos y de IA, y avisa si difieren más del doble (sesgo de longitud) o si todos los humanos son de un solo tipo.

2. **Ejecuta la evaluación:**

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-texts --out-dir ~/ai-eval-texts-out --augment strip
```

`--augment strip` evalúa también copias a las que se les quitan los restos de asistente (marcas de cita, frases, marcadores), para ver cuánto depende la detección de algo que cualquiera puede borrar. No necesita Pillow.

3. **Lee `~/ai-eval-texts-out/report.md`**, en este orden:
   1. La fila **«falsos positivos con confianza al menos media»** en textos humanos. En texto es la cifra que importa. Con estilo solo, la herramienta no pasa de «confianza baja», así que debería quedar en cero.
   2. **«Según el idioma»** y **«Según la longitud»**: dónde se equivoca más.
   3. **La tabla «salta en reales, por origen»**: si una señal (por ejemplo las frases hechas o las rayas largas) salta mucho más en `segundo-idioma` o en `escritos-en-word` que en `nativos`, esa señal debería **perder peso**, diga lo que diga su cociente de verosimilitud.
   4. Las **copias limpiadas**: cuánta detección se pierde al quitar los restos.

Cuando lo tengas, pásame el resultado de `--check` o las primeras líneas del `report.md` y lo leemos juntos.
