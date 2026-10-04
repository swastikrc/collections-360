#!/usr/bin/env bash
# Layer 2 live run: everything that needs the Gemini key, in the runbook's order.
#
#   bash scripts/layer2_live.sh                # full run
#   bash scripts/layer2_live.sh --extra FILE   # also answer the extra questions (CSV: question_id, question_text, ...)
#   bash scripts/layer2_live.sh --only-extra FILE
#
# Stops before spending calls if the key has no quota (HTTP 429). Re-running is cheap: benchmark questions already
# answered are reused (--resume-from the latest run); only new questions or ones that hit a model error are asked again.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${C360_ENV_FILE:-$HOME/.config/c360/layer2.env}"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
export MAPLE_DATA="${MAPLE_DATA:-$PWD/maple_collections_release}"
unset C360_WAREHOUSE C360_L2_MONGO
DELAY="${C360_L2_EVAL_DELAY:-1}"
OUT=outputs/layer2
EXTRA=""; ONLY_EXTRA=0
while [ $# -gt 0 ]; do
  case "$1" in
    --extra) EXTRA="$2"; shift 2 ;;
    --only-extra) EXTRA="$2"; ONLY_EXTRA=1; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done

step() { printf '\n==== %s ====\n' "$*"; }

step "0. preconditions"
[ -n "${GEMINI_API_KEY:-}" ] || { echo "GEMINI_API_KEY is not set (expected in $ENV_FILE)"; exit 2; }
[ -n "${C360_L2_MODEL:-}${C360_L2_MODELS:-}" ] || { echo "set C360_L2_MODEL, or C360_L2_MODELS for the model rotation"; exit 2; }
echo "key: set (length ${#GEMINI_API_KEY}) · model: ${C360_L2_MODELS:+rotation }${C360_L2_MODELS:-$C360_L2_MODEL}"
podman start c360-mongo >/dev/null 2>&1 || true
python3 - <<'EOF'
import os, sys
from google import genai
from google.genai import types, errors
from c360.layer2.llm.rotation import parse_pool, classify
pool = [n for n, _ in parse_pool(os.environ.get("C360_L2_MODELS", ""))] or [os.environ["C360_L2_MODEL"]]
client, ok = genai.Client(), []
for m in pool:                                   # one tiny request per model
    try:
        client.models.generate_content(model=m, contents="Reply ok", config=types.GenerateContentConfig(max_output_tokens=20))
        print(f"quota check {m}: ok"); ok.append(m)
    except errors.APIError as e:
        kind, delay = classify(e)
        print(f"quota check {m}: HTTP {e.code} ({kind}{f', retry in {delay:.0f}s' if delay else ''})")
if not ok:
    sys.exit(3)
print(f"{len(ok)} of {len(pool)} model(s) usable now")
EOF

latest() { ls -1t $OUT/eval_"$1"_*.json 2>/dev/null | while read -r f; do python3 -c "import json,sys; sys.exit(0 if not json.load(open('$f')).get('fake') else 1)" && { echo "$f"; break; }; done; }

if [ "$ONLY_EXTRA" = 0 ]; then
  step "1. offline tests + doctor (must be green before spending calls)"
  python3 -m pytest tests/layer2 -q -m "not live" | tail -1
  python3 -m c360.layer2.doctor | head -1

  step "2. live smoke tests (5 routes, synthetic world)"
  python3 -m pytest tests/layer2 -q -m live | tail -3 || true

  step "3. refusal set"
  python3 -m c360.layer2.eval --set refusal --delay "$DELAY" || true

  step "4. benchmark (35 questions; answers CSV written automatically)"
  PREV=$(latest benchmark || true)
  python3 -m c360.layer2.eval --set benchmark --delay "$DELAY" ${PREV:+--resume-from "$PREV"} || true

  step "5. held-out questions on the real data"
  PREV=$(latest heldout_real || true)
  python3 -m c360.layer2.eval --set heldout_real --delay "$DELAY" ${PREV:+--resume-from "$PREV"} || true
fi

if [ -n "$EXTRA" ]; then
  step "6. extra questions from $EXTRA"
  NAME="benchmark_$(basename "${EXTRA%.*}" | tr ' ' '_')"
  PREV=$(latest "$NAME" || true)
  python3 -m c360.layer2.eval --questions-file "$EXTRA" --delay "$DELAY" ${PREV:+--resume-from "$PREV"} || true
fi

step "7. submission CSV (benchmark + extras, latest live runs)"
RUNS=$(ls -1t $OUT/eval_benchmark*_*.json 2>/dev/null | tac | tr '\n' ' ')
if [ -n "$RUNS" ]; then
  python3 -m c360.layer2.eval.submission --from $RUNS --out $OUT/benchmark_answers.csv || true
  echo "submit: $OUT/benchmark_answers.csv"
fi

step "8. acceptance report"
python3 -m c360.layer2.acceptance --examples || python3 -m c360.layer2.acceptance
echo "done: docs/layer2/acceptance_report.md"
