#!/usr/bin/env bash
#SBATCH --job-name=cg-path-dev
#SBATCH --account=pi_juanzhai_umass_edu
#SBATCH --partition=superpod-a100
#SBATCH --qos=normal
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
set -euo pipefail
SNAPSHOT=$1
RESULTS_ROOT=$2
TASK_PYTHON=/work/pi_juanzhai_umass_edu/chetan/CausalGuard/.venv/bin/python
SERVER_PYTHON=/work/pi_juanzhai_umass_edu/gehaozhang/conda/envs/Unsloth/bin/python
MODEL_PATH=/datasets/ai/qwen3/hub/models--Qwen--Qwen3-32B/snapshots/9216db5781bf21249d130ec9da846c4624c16137
JOB_OUTPUT="${RESULTS_ROOT}/job-${SLURM_JOB_ID}"
mkdir -p "${JOB_OUTPUT}"
cd "${SNAPSHOT}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${SNAPSHOT}/vendor:${SNAPSHOT}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
PORT=$((20000 + SLURM_JOB_ID % 20000))
"${TASK_PYTHON}" -c 'import sys; from pathlib import Path; from scripts.evaluate_path_development import verify_snapshot; verify_snapshot(Path(sys.argv[1]))' "${SNAPSHOT}"
date -u +%FT%TZ >"${JOB_OUTPUT}/server_start_utc.txt"
"${SERVER_PYTHON}" -m vllm.entrypoints.openai.api_server \
 --model "${MODEL_PATH}" --served-model-name Qwen/Qwen3-32B \
 --host 127.0.0.1 --port "${PORT}" --dtype bfloat16 \
 --max-model-len 16384 --gpu-memory-utilization 0.95 --max-num-seqs 4 \
 --disable-log-requests --enforce-eager >"${JOB_OUTPUT}/vllm-server.log" 2>&1 &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT
for _ in $(seq 1 240); do
 if curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then break; fi
 if ! kill -0 "${SERVER_PID}" 2>/dev/null; then exit 1; fi
 sleep 5
done
curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null
date -u +%FT%TZ >"${JOB_OUTPUT}/server_ready_utc.txt"
"${TASK_PYTHON}" -m scripts.evaluate_path_development \
 --snapshot "${SNAPSHOT}" --output "${JOB_OUTPUT}/trajectories" --port "${PORT}"
date -u +%FT%TZ >"${JOB_OUTPUT}/finished_utc.txt"
