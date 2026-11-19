#!/usr/bin/env bash
# Generate this week's Community Signal inputs with last30days.
#
# Usage: scripts/community-signal/run.sh [YYYY-MM-DD] [topic ...]
#
# Env:
#   SKILL_DIR            last30days-skill checkout (default: .cache/last30days-skill, cloned if missing)
#   SKILL_REF            git ref to clone (default: v4.3.2)
#   LAST30DAYS_PYTHON    Python 3.12 interpreter (default: `uv python find 3.12`)
#   COMMUNITY_SIGNAL_TOPICS  newline- or pipe-separated topics, used when none are given as args
#   DEVTO_TAGS           dev.to tags that opt that keyless source in (default: ai,llm,agents)
#   DAYS                 lookback window in days (default: 30; use 7 for "this week")
#   SSL_CERT_FILE        CA bundle, only needed behind a TLS-intercepting proxy
#   OUT_DIR              where outputs go (default: data/community-signal)
#
# Writes:
#   $OUT_DIR/<date>.html        synthetic "newsletter email" for the n8n Gemini curation step
#   $OUT_DIR/<date>.json        flat candidate list
#   $OUT_DIR/latest.html|json   copies, so n8n can fetch a stable URL
#   $OUT_DIR/raw/<date>/        engine exports and logs
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SKILL_DIR="${SKILL_DIR:-$REPO/.cache/last30days-skill}"
SKILL_REF="${SKILL_REF:-v4.3.2}"
OUT_DIR="${OUT_DIR:-$REPO/data/community-signal}"

ISSUE_DATE="${1:-$(date -u +%F)}"; shift || true
if [ "$#" -gt 0 ]; then
  TOPICS=("$@")
elif [ -n "${COMMUNITY_SIGNAL_TOPICS:-}" ]; then
  IFS=$'\n|' read -r -d '' -a TOPICS < <(printf '%s\0' "$COMMUNITY_SIGNAL_TOPICS") || true
else
  TOPICS=("AI agents for enterprise" "LLM coding assistants" "AI in software engineering teams")
fi

if [ ! -f "$SKILL_DIR/skills/last30days/scripts/last30days.py" ]; then
  echo ">> cloning last30days-skill@$SKILL_REF into $SKILL_DIR" >&2
  git clone --quiet --depth 1 --branch "$SKILL_REF" https://github.com/mvanhorn/last30days-skill.git "$SKILL_DIR"
fi
ENGINE="$SKILL_DIR/skills/last30days/scripts/last30days.py"
PY="${LAST30DAYS_PYTHON:-$(uv python find 3.12)}"

export LAST30DAYS_ALLOW_ENGINE_PLAN=1   # headless: let the engine plan without a hosting LLM
DEVTO_TAGS="${DEVTO_TAGS:-ai,llm,agents}"
DAYS="${DAYS:-30}"
RAW="$OUT_DIR/raw/$ISSUE_DATE"
mkdir -p "$RAW"
inputs=()

echo ">> discovery: AI" >&2
# One-shot discovery names topics by heuristic ("Windows 11"), then researches that short name.
# With the keyless web backend in the mix the top evidence URL becomes a generic search hit or
# even an ad; restricted to community sources it is the story people actually upvoted.
if EXCLUDE_SOURCES=grounding "$PY" "$ENGINE" --discover "AI" --days "$DAYS" --emit=json --output "$RAW/discover-ai.json" \
     --save-dir "$RAW/save" --no-browser-cookies > "$RAW/discover-ai.log" 2>&1; then
  inputs+=("$RAW/discover-ai.json")
else
  echo "   discovery failed (exit $?), see $RAW/discover-ai.log" >&2
fi

for topic in "${TOPICS[@]}"; do
  [ -z "$topic" ] && continue
  slug="$(printf '%s' "$topic" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')"
  echo ">> research: $topic" >&2
  if "$PY" "$ENGINE" "$topic" --days "$DAYS" --devto-tags "$DEVTO_TAGS" --emit=json --output "$RAW/research-$slug.json" \
       --save-dir "$RAW/save" --no-browser-cookies > "$RAW/research-$slug.log" 2>&1; then
    inputs+=("$RAW/research-$slug.json")
  else
    echo "   research '$topic' failed (exit $?), see $RAW/research-$slug.log" >&2
  fi
done

if [ "${#inputs[@]}" -eq 0 ]; then
  echo "!! no engine output at all; not writing candidates" >&2
  exit 1
fi

python3 -I "$HERE/convert.py" "${inputs[@]}" --date "$ISSUE_DATE" --format email-html  --out "$OUT_DIR/$ISSUE_DATE.html"
python3 -I "$HERE/convert.py" "${inputs[@]}" --date "$ISSUE_DATE" --format candidates  --out "$OUT_DIR/$ISSUE_DATE.json"
cp "$OUT_DIR/$ISSUE_DATE.html" "$OUT_DIR/latest.html"
cp "$OUT_DIR/$ISSUE_DATE.json" "$OUT_DIR/latest.json"
rm -rf "$RAW/save"
echo ">> done: $OUT_DIR/$ISSUE_DATE.{html,json}" >&2
