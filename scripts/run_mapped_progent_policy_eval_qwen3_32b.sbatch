#!/usr/bin/env bash
#SBATCH --job-name=cg-progent-qwen3
#SBATCH --account=pi_juanzhai_umass_edu
#SBATCH --partition=superpod-a100
#SBATCH --qos=normal
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --output=outputs/agentdojo/mapped_progent_qwen3_slurm-%j.out
#SBATCH --error=outputs/agentdojo/mapped_progent_qwen3_slurm-%j.err

set -euo pipefail

REPO=/work/pi_juanzhai_umass_edu/chetan/CausalGuard
SERVER_PYTHON=/work/pi_juanzhai_umass_edu/gehaozhang/conda/envs/Unsloth/bin/python
TASK_PYTHON="${REPO}/.venv/bin/python"
MODEL_PATH=/datasets/ai/qwen3/hub/models--Qwen--Qwen3-32B/snapshots/9216db5781bf21249d130ec9da846c4624c16137
SERVED_MODEL=Qwen/Qwen3-32B
OUTPUT_ROOT="${REPO}/outputs/agentdojo/mapped_progent_slack_user_task_12_qwen3_32b_2026-09-24"
OUTPUT="${OUTPUT_ROOT}/job-${SLURM_JOB_ID}"
JOB_TMP="${TMPDIR:-/tmp}/causalguard-mapped-progent-qwen3-${SLURM_JOB_ID}"
PORT=$((20000 + SLURM_JOB_ID % 20000))

mkdir -p "${OUTPUT}" "${JOB_TMP}/xdg-cache"
cd "${REPO}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export XDG_CACHE_HOME="${JOB_TMP}/xdg-cache"

{
  printf 'slurm_job_id=%s\n' "${SLURM_JOB_ID}"
  printf 'model=%s\n' "${SERVED_MODEL}"
  printf 'model_path=%s\n' "${MODEL_PATH}"
  printf 'port=%s\n' "${PORT}"
  printf 'repository_revision=%s\n' "$(git rev-parse HEAD)"
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} >"${OUTPUT}/job_metadata.txt"

"${SERVER_PYTHON}" -m vllm.entrypoints.openai.api_server \
  --model "${MODEL_PATH}" \
  --served-model-name "${SERVED_MODEL}" \
  --host 127.0.0.1 \
  --port "${PORT}" \
  --dtype bfloat16 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.95 \
  --max-num-seqs 4 \
  --seed 0 \
  --disable-log-requests \
  --enforce-eager \
  >"${OUTPUT}/vllm-server.log" 2>&1 &
SERVER_PID=$!

cleanup() {
  set +e
  kill "${SERVER_PID}" >/dev/null 2>&1 || true
  wait "${SERVER_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 240); do
  if curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    tail -n 200 "${OUTPUT}/vllm-server.log" >&2
    exit 1
  fi
  sleep 5
done
curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null

"${TASK_PYTHON}" -m scripts.run_mapped_progent_policy_eval \
  --port "${PORT}" \
  --model "${SERVED_MODEL}" \
  --output-dir "${OUTPUT}" \
  --require-clean-tool-call-preflight

printf 'completed_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >"${OUTPUT}/COMPLETED"
