# Community Signal: links que la comunidad discutió, dentro del pipeline existente

Suma una segunda fuente de candidatos al newsletter sin tocar el prompt de curación, el Sheet de revisión
ni el sitio. Hoy cada edición nace de emails de newsletters reenviados a la inbox; esto agrega, una vez por
semana, un "email" sintético con lo que engineers discutieron en Hacker News, Reddit, YouTube, dev.to y GitHub
en los últimos 30 días, generado por [last30days](https://github.com/mvanhorn/last30days-skill).

```
GitHub Action (lunes 09:00 UTC, runner de GitHub, fuera del proxy corporativo)
  └─ scripts/community-signal/run.sh
       ├─ last30days --discover "AI"            → temas trending
       ├─ last30days "<topic>" × N              → links con engagement
       └─ convert.py --format email-html        → data/community-signal/<fecha>.html (+ latest.html)
                     --format candidates        → data/community-signal/<fecha>.json (+ latest.json)
  └─ commit a main  ([skip ci] en el mensaje; el sitio no lee esta carpeta)

n8n (martes 09:00, o manual)
  └─ HTTP Request: GET raw .../data/community-signal/latest.html
  └─ Code: lo deja con la forma de un email de Gmail  { subject, from, source, date, html }
  └─ ⇒ mismo nodo Gemini + Sheet + Drive que usa la rama de emails
```

## Por qué así

- **El prompt de Gemini ya hace la curación.** Pide "raw HTML of an incoming newsletter email" y evalúa cada
  `<a href>`. El HTML generado tiene esa forma: título como anchor, descripción, y una línea con fuente y
  engagement como contexto. Las reglas de 0% (news, launches, personal projects) filtran solas el ruido que
  trae la comunidad, y la revisión humana en el Sheet sigue igual.
- **GitHub Actions en vez de n8n para correr el motor.** El motor es Python 3.12 + yt-dlp y tarda 2-3 minutos.
  Desde la red de Monks, Netskope bloquea reddit.com; un runner de GitHub no tiene ese problema. n8n cloud
  además no puede ejecutar comandos. Así n8n solo hace un GET.
- **Candidatos, no edición.** El Action no escribe en `data/newsletters/`. Lo que publica el sitio sigue
  saliendo del flujo de revisión.

## Instalación

1. **Repo**: mergear esta carpeta y `.github/workflows/community-signal.yml`. Opcional, en
   Settings → Variables:
   - `COMMUNITY_SIGNAL_TOPICS`: temas separados por `|` (default: `AI agents for enterprise|LLM coding
     assistants|AI in software engineering teams`).
   - `DEVTO_TAGS`: tags de dev.to (default `ai,llm,agents`).
   - `LAST30DAYS_REF`: tag del skill a clonar (default `v4.3.2`).
   Secrets opcionales que amplían fuentes: `BRAVE_API_KEY` (web search estable, 2000 queries/mes gratis),
   `SCRAPECREATORS_API_KEY` (TikTok, Instagram, backfill de Reddit y YouTube; 10k llamadas gratis).
2. **Primera corrida**: Actions → "Community Signal (last30days)" → Run workflow. Deja
   `data/community-signal/latest.html` en main.
3. **n8n**: importar `n8n-community-signal.workflow.json`. Borrar el nodo final "→ connect to your Gemini
   curation node" y conectar "Shape as newsletter email" al nodo Gemini de la rama de emails. El item trae
   `html` (cuerpo) y `source = "Community Signal"`; renombrar campos si el nodo de Gmail usa otros nombres.
4. Si el repo pasa a privado, reemplazar el HTTP Request por el nodo GitHub (Get file) con un token.

## Probarlo antes de mergear

GitHub solo acepta el cron y "Run workflow" cuando el archivo del workflow ya está en `main`, así que desde
la rama del PR esas dos vías no existen. En su lugar:

1. **La Action corre sola en el PR** (trigger `pull_request`, acotado a esta carpeta y al workflow). Hace
   exactamente lo mismo que el cron pero no commitea: deja `latest.html`, `latest.json` y los logs del motor
   como artifact `community-signal` en la pestaña Checks del PR. Ahí se ve si Reddit y la web keyless
   responden desde el runner, cosa que desde la red de Monks no se puede comprobar.
2. **n8n se puede probar contra la rama.** El PR incluye un `data/community-signal/latest.html` generado
   desde una laptop (52 candidatos, sin Reddit). Importar el workflow, cambiar en "Fetch latest.html" la URL
   de `/main/` a `/<nombre-de-la-rama>/`, y usar "Run manually". Cuando el PR se mergea, volver a `/main/`.
3. Si el PR viene de un fork, el runner no tiene secrets: las fuentes keyless funcionan igual.

## Correrlo a mano

```bash
scripts/community-signal/run.sh 2026-11-18 "AI agents for enterprise" "LLM coding assistants"
```

Clona el skill en `.cache/last30days-skill` si no está, necesita `uv` (instala Python 3.12 solo) y, para
YouTube, `yt-dlp` en el PATH. Detrás de un proxy que intercepta TLS hay que exportar `SSL_CERT_FILE` con un
bundle que incluya el CA del proxy.

## Qué esperar de la primera edición

En la prueba del 2026-11-18 desde una laptop (sin Reddit): 63 candidatos únicos de 4 corridas, 74 links en el
HTML contando "related coverage". Con el prompt actual, la mayoría va a quedar en 0% (news y launches dominan
Hacker News) y eso está bien: la señal útil son los 10-15 artículos de fondo con mucho engagement que no llegan
por los newsletters de email. Si la proporción 0% resulta demasiado alta, bajar `MAX_CANDIDATES` o subir
`CANDIDATE_FLOOR` en `convert.py` antes de tocar el prompt.

## Costos

Todo gratis con las fuentes keyless. La Action usa unos 3-4 minutos de runner por semana. Las claves opcionales
tienen tier gratuito.
