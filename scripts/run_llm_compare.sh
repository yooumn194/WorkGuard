#!/usr/bin/env bash
# One-command LLM-vs-heuristic comparison: extraction quality + token cost.
#
# Prerequisites (choose an OpenAI-compatible provider):
#   export OPENAI_API_KEY=sk-...
#   export OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # Qwen
#   # or https://api.deepseek.com  (DeepSeek); leave unset for OpenAI
#   export WORKGUARD_LLM_MODEL=qwen-plus   # or deepseek-chat / gpt-4o-mini ...
#
# Usage:
#   .venv/bin/python eval/extraction_bench.py --mode heuristic   # baseline (offline)
#   bash scripts/run_llm_compare.sh                              # full comparison
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY is not set — cannot run the LLM leg."
  echo "Export OPENAI_API_KEY (+ optional OPENAI_BASE_URL / WORKGUARD_LLM_MODEL) and rerun."
  echo "The heuristic baseline reports already exist: eval/reports/extraction_report_heuristic.json"
  exit 1
fi

echo "== 1/4 heuristic baseline (offline, deterministic) =="
WORKGUARD_LLM_PROVIDER=heuristic .venv/bin/python eval/extraction_bench.py --mode heuristic

echo "== 2/4 pure LLM extraction (fallback disabled) =="
WORKGUARD_LLM_PROVIDER=openai .venv/bin/python eval/extraction_bench.py --mode llm

echo "== 3/4 production hybrid extraction (fallback enabled) =="
WORKGUARD_LLM_PROVIDER=openai .venv/bin/python eval/extraction_bench.py --mode hybrid

echo "== 4/4 LLM-mode pipeline eval (28 end-to-end cases) =="
WORKGUARD_LLM_PROVIDER=openai .venv/bin/python eval/evaluator.py

echo
echo "Reports: eval/reports/extraction_report_{heuristic,llm,hybrid}.json"
echo "Compare P/R/F1 per noise group + token_usage_total / estimated_cost_usd."
