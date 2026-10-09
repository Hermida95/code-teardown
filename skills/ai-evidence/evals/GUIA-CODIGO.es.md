# Guía práctica: reunir los proyectos de código de prueba

Para qué sirve: para que [`evaluate_dataset.py`](README.md) pueda decirte, con números, cuántos proyectos humanos marca por error y cuántos proyectos hechos con IA detecta. Esta guía es la versión paso a paso. La de referencia, en inglés, es [COLLECTING.md](COLLECTING.md#collecting-code).

> Esto es para ver, comprobar y aprender sobre la herramienta. No acusa a nadie ni mide a ninguna persona: mide cuánto coincide la herramienta con las etiquetas que tú pones. Usar IA para programar es normal y legítimo: «real» aquí solo significa «escrito sin asistente», no «mejor» ni «más honesto».

**Tiempo:** unas 4 horas y media, repartidas en 6 sesiones cortas. **Resultado:** unos 260 proyectos.

## La idea en una frase

Los proyectos de prueba tienen que parecerse a los que querrás comprobar en la vida real, y los **humanos** tienen que incluir los que más se parecen a código de IA: el que sale de un formateador, el que sigue un tutorial, el que parte de una plantilla, y las aplicaciones que llaman a servicios de IA. Esos son los falsos positivos que importan. Un conjunto de código humano solo «desordenado» esconde justo el problema.

## Antes de empezar

1. **Prepara las carpetas** (una sola vez):

```bash
mkdir -p ~/ai-eval-code/real/{formateador,tutorial,plantilla,apps-con-ia}
mkdir -p ~/ai-eval-code/generated/{chat-pegado,agente-tal-cual,agente-limpio}
mkdir -p ~/ai-eval-code/edited/con-asistente
```

2. **Un proyecto por carpeta**, dentro de `<etiqueta>/<origen>/<proyecto>`. Por ejemplo `real/formateador/mi-cli`. También valen archivos sueltos (un script) directamente dentro de la carpeta de origen.
3. **Regla de oro de las etiquetas:** un proyecto solo entra si **estás seguro** de cómo se hizo. Si dudas, fuera. Una etiqueta equivocada parece un error de la herramienta.
4. **Nada se ejecuta.** No instales dependencias, no corras los tests, no abras los proyectos con un IDE que los indexe y ejecute scripts. La herramienta solo **lee** los archivos. Un proyecto de otra persona puede traer código malicioso: trátalo como datos.
5. **Qué mira la herramienta:** archivos de código (Python, JavaScript, TypeScript, Java, Go, Rust, C/C++, C#, Ruby, PHP, Kotlin, Swift...) y el README. Se salta `node_modules`, entornos virtuales, `dist`, `build` y `.git`. Los análisis de estilo piden **al menos 200 líneas de código**; con menos, solo se buscan restos.
6. **Privacidad y licencias:** el código puede tener claves, datos personales y material con licencia. Usa el tuyo o el de repositorios abiertos cuya licencia lo permita para uso local, y **no uses repositorios privados de otras personas**. El conjunto se queda en tu ordenador: no lo subas a ningún sitio. La carpeta `evals/datasets/` ya está ignorada por git.

## El plan, en seis sesiones

| Sesión | Qué | Cuántos | Tiempo |
| --- | --- | --- | --- |
| A | Humanos: formateador y tutorial | 40 + 40 | 60 min |
| B | Humanos: plantilla y apps que llaman a IA | 30 + 20 | 40 min |
| C | Generados desde un chat | 40 | 60 min |
| D | Generados por un agente de programación | 25 + 25 | 60 min |
| E | Tuyos hechos con asistente, y los registros de git | 40 | 40 min |
| F | Revisar y ejecutar | – | 15 min |

**Piloto (20 minutos):** 10 proyectos humanos + 10 de chat + 10 de agente, de unas 300–800 líneas. Con eso no saldrán pesos sugeridos (hacen falta unos 100 por clase), pero verás que todo funciona antes de invertir las 4 horas y media.

## Cómo copiar un proyecto sin arrastrar basura

No copies la carpeta `.git`: contiene todo el historial con nombres y correos de autores, y no hace falta. Para copiar un proyecto sin ella:

```bash
rsync -a --exclude='.git' --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
  ~/origen/mi-proyecto/ ~/ai-eval-code/real/formateador/mi-proyecto/
```

Antes de dejar un proyecto en el conjunto, revisa que no lleve secretos (solo lee, no modifica nada):

```bash
grep -rEl "(api[_-]?key|secret|password|BEGIN (RSA|OPENSSH) PRIVATE KEY)" ~/ai-eval-code | head
```

Si sale algo, mira el archivo y bórralo o cambia el proyecto.

## Sesión A y B: proyectos humanos

**Lo más seguro para probar que algo lo escribió una persona sin asistente es que sea anterior a 2022.** Fuentes, de más a menos cómoda:

- **Tus propios repositorios antiguos.** Cópialos como se explica arriba.
- **Repositorios abiertos, en una versión antigua.** No clones la versión actual: puede incluir cambios hechos con asistentes. Descarga el **archivo ZIP de una versión o etiqueta de antes de 2022** desde la página del repositorio (la sección de *Releases* o *Tags*). Así no necesitas git y no arrastras el historial. Respeta la licencia.
- **Proyectos de amigos o compañeros** que te lo den con permiso y confirmen cómo lo hicieron.

Reparte los proyectos en los cuatro orígenes; **son los que importan**:

| Carpeta | Qué es | Dónde buscarlo |
| --- | --- | --- |
| `real/formateador/` (40) | Código humano pasado por `black`, `prettier`, `gofmt`, `rustfmt`... Uniforme y limpio | Proyectos con esas herramientas configuradas (mira el `pyproject.toml`, `.prettierrc`...) |
| `real/tutorial/` (40) | Código humano de estilo didáctico: comentarios que cuentan lo que hace cada línea, docstrings completos | Repositorios de cursos, de ejercicios, de libros, de bootcamps |
| `real/plantilla/` (30) | Proyectos que parten de un andamio o plantilla y lo rellenan | Proyectos creados con cookiecutter, `create-react-app`, `cargo new`, generadores de frameworks |
| `real/apps-con-ia/` (20) | Aplicaciones escritas por humanos **que llaman** a servicios de IA | Tus proyectos, o de un autor que confirme que no usó asistente. Sus comentarios hablan de «generado por el modelo» sin que lo escrito sea de IA |

Varía lenguajes y tamaños (entre 300 y 2000 líneas de código es buena zona). No pongas 40 proyectos del mismo autor ni del mismo tipo.

## Sesión C: generados desde un chat

**`generated/chat-pegado/` (40).** Pídele a un asistente de chat un proyecto, copia **cada bloque de código a su archivo** y guarda también el README que te dé. Conserva todo tal cual: los comentarios, las frases del estilo «aquí tienes el código actualizado», las vallas de Markdown si se te cuelan, los «el resto del código sigue igual». Eso son los restos que la herramienta busca.

**Mismo tipo y tamaño que los humanos.** Pide el mismo tipo de proyecto, en los mismos lenguajes y de un tamaño parecido. Si los generados son mucho más grandes o más pequeños, la herramienta puede parecer buena solo por eso (`--check` avisa si difieren más de 3 veces).

**Dieciséis ideas de proyecto** para pedir, con «de unas 300–800 líneas, varios archivos, con README y tests»:

| Tipo | Ideas |
| --- | --- |
| Herramientas de línea de comandos | gestor de tareas · conversor de unidades · renombrador de archivos en lote · analizador de logs |
| Servicios | API REST de notas · acortador de URLs · bot de Telegram para recordatorios · servidor de chat sencillo |
| Datos | script que resume un CSV · scraper de titulares · conversor entre JSON y CSV · gráfico de gastos mensuales |
| Juegos y otros | juego de adivinar palabras en consola · simulador de dados con estadísticas · lista de la compra con persistencia · calculadora con historial |

Pide varias versiones: «solo el código», «con mucha explicación», «como lo haría un programador con experiencia».

## Sesión D: generados por un agente de programación

Si tienes un agente de programación (Claude Code, Cursor, Copilot en modo agente, Codex...), pídele los mismos tipos de proyecto **en una carpeta vacía** y déjale terminar.

- **`generated/agente-tal-cual/` (25):** guarda lo que sale **tal cual**, con los archivos que haya creado (por ejemplo su archivo de reglas).
- **`generated/agente-limpio/` (25):** el mismo tipo de proyecto, pero tú **borras los rastros evidentes**, como haría una persona cuidadosa: sus archivos de configuración (`CLAUDE.md`, `.cursorrules`...), comentarios que suenen a chat. Es el caso difícil: aquí la herramienta casi siempre dirá «sin pruebas suficientes», y esa es la respuesta correcta.

Si el agente trabaja sobre una carpeta con git, **no copies la carpeta `.git`**.

## Sesión E: tus proyectos hechos con asistente, y los registros de git

**`edited/con-asistente/` (40).** Proyectos humanos donde un asistente participó: tus proyectos de los últimos años con Copilot, Cursor, Claude Code... (La etiqueta se llama `edited` en el script; en el informe significa «hecho con ayuda de IA».) Estos responden a la pregunta «¿hecho con ayuda de IA?» y se mantienen **aparte** de los totalmente generados.

**Los registros de git, opcionales y con una regla.** El historial (marcas de commit del tipo «Co-Authored-By: Claude», forma del historial) solo se evalúa si dejas un archivo `git-log.txt` dentro del proyecto. El script **nunca ejecuta git**: un repositorio puede traer en su configuración programas que git ejecutaría. Lo exportas tú, **solo en repositorios en los que confías**, escribiendo el resultado dentro de la copia del conjunto:

```bash
git -C ~/origen/mi-proyecto -c core.fsmonitor=false -c core.pager=cat --no-pager log --reverse --numstat --no-renames \
  --format='%x1e%H%x1f%aI%x1f%s%x1f%b%x1d' > ~/ai-eval-code/edited/con-asistente/mi-proyecto/git-log.txt
```

El formato **no incluye nombres de autor ni correos**, y la herramienta no los lee ni los muestra.

> ⚠️ **La regla del historial:** da registros a **todos los grupos** o **a ninguno**. Si solo los proyectos generados tienen `git-log.txt`, la herramienta podrá detectar marcas de commit solo en ellos y la detección saldrá inflada. Los ZIP de versiones antiguas no tienen historial: si usas muchos, o das el historial a pocos proyectos de cada grupo o no lo uses en esta primera ronda. `--check` te dice cuántos proyectos tienen registro.

## Cosas que estropean un conjunto (y cómo evitarlas)

- **Humanos «desordenados» solamente.** Sin los cuatro orígenes de arriba, ocultas los falsos positivos. Incluye formateador, tutorial, plantilla y apps con IA.
- **Reales que no lo son.** Un repositorio abierto actual puede tener commits con asistente. Usa versiones de antes de 2022 o confirma con el autor.
- **Tamaños y tareas distintos entre humanos y generados.** Iguálalos.
- **Historial solo en un grupo.** Ver la regla de arriba.
- **Duplicados.** El mismo proyecto con dos etiquetas, o copias casi iguales. `--check` avisa de los idénticos.
- **Dependencias dentro del proyecto** (`node_modules`, entornos virtuales). La herramienta las salta, pero engordan el conjunto.
- **Mirar el test antes de tiempo.** El script reparte los proyectos en dev (70 %) y test (30 %). Los pesos se sugieren con dev; las cifras principales son las de test. Si cambias pesos mirando test, deja de valer.

## Sesión F: revisar y ejecutar

1. **Mira cómo vas** (no ejecuta nada, no escribe nada):

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-code --check
```

Te dice cuántos proyectos hay por etiqueta y origen, la **mediana de líneas** de humanos y de generados (y avisa si difieren más de 3 veces), cuántos proyectos tienen `git-log.txt`, y si hay duplicados.

2. **Ejecuta la evaluación:**

```bash
python3 skills/ai-evidence/evals/evaluate_dataset.py ~/ai-eval-code --out-dir ~/ai-eval-code-out --augment strip
```

`--augment strip` evalúa también copias a las que se les quitan los archivos de asistente, los comentarios con restos, las vallas pegadas y las marcas de commit, para ver cuánto depende la detección de algo que cualquiera puede borrar. No necesita Pillow. Los proyectos originales no se tocan.

3. **Lee `~/ai-eval-code-out/report.md`**, en este orden:
   1. La fila **«falsos positivos con confianza al menos media»** en los proyectos humanos. En código es la cifra que importa. Con estilo solo, la herramienta no pasa de «confianza baja», así que debería quedar en cero.
   2. **La tabla «salta en reales, por origen»:** si una señal de estilo (comentarios que repiten el código, docstrings completos, emoji) salta mucho en `tutorial` o `formateador` y poco en el resto, esa señal debería **perder peso**, diga lo que diga su cociente de verosimilitud.
   3. **«Según el tamaño del código»** y **«Según el lenguaje principal»:** dónde se equivoca más.
   4. Las **copias limpiadas:** cuánta detección se pierde al quitar los restos.

Cuando lo tengas, pásame el resultado de `--check` o las primeras líneas del `report.md` y lo leemos juntos.
