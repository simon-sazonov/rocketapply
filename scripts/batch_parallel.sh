#!/bin/zsh
# Three shards of one batch, in parallel, each with its own key.
#
# The key comes from the environment, so nothing in the Python has to know that
# more than one exists — `GROQ_API_KEY` in a worker's environment overrides the
# configured one for that process only.
#
#   export PITWALL_KEY_0=gsk_...          # your own
#   export PITWALL_KEY_1=gsk_...
#   export PITWALL_KEY_2=gsk_...
#   scripts/batch_parallel.sh jobs.txt
#
# jobs.txt is one job id per line. The split is by position in the sorted id
# list, so shard 1 can never be handed a job shard 0 already did, and a rerun
# divides them exactly the same way.
set -u
cd "${0:A:h}/.."

FILE="${1:?usage: batch_parallel.sh <file-of-job-ids> [name]}"
NAME="${2:-batch-$(date +%Y-%m-%d)}"
SHARDS=3

for i in $(seq 0 $((SHARDS - 1))); do
  eval "KEY=\${PITWALL_KEY_$i:-}"
  if [[ -z "$KEY" ]]; then
    echo "PITWALL_KEY_$i is not set — run with one key per shard" >&2
    exit 1
  fi
done

echo "$NAME · $SHARDS shards · $(grep -c '[0-9]' "$FILE") job(s)"
for i in $(seq 0 $((SHARDS - 1))); do
  eval "KEY=\${PITWALL_KEY_$i}"
  GROQ_API_KEY="$KEY" .venv/bin/python scripts/run_pipeline.py apply batch \
      --from-file "$FILE" --name "$NAME" --shards $SHARDS --shard $i --no-audit \
      > "run/batch-shard$i.log" 2>&1 &
  echo "  shard $i -> pid $! -> run/batch-shard$i.log"
done

wait
echo "\nall shards finished — the report:\n"
.venv/bin/python scripts/run_pipeline.py apply batch --name "$NAME" --report
