#!/usr/bin/env bash
#SBATCH --job-name=cg-progent-policy
#SBATCH --account=pi_juanzhai_umass_edu
#SBATCH --partition=gpu
#SBATCH --qos=normal
#SBATCH --gres=gpu:2080_ti:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=02:00:00
#SBATCH --output=/work/pi_juanzhai_umass_edu/chetan/CausalGuard/outputs/agentdojo/mapped_progent_slack_user_task_12_2026-09-24/slurm-%j.out
#SBATCH --error=/work/pi_juanzhai_umass_edu/chetan/CausalGuard/outputs/agentdojo/mapped_progent_slack_user_task_12_2026-09-24/slurm-%j.err

set -euo pipefail
REPO=/work/pi_juanzhai_umass_edu/chetan/CausalGuard
SERVER_PYTHON=/work/pi_juanzhai_umass_edu/gehaozhang/conda/envs/Unsloth/bin/python
TASK_PYTHON="${REPO}/.venv/bin/python"
MODEL_PATH=/datasets/ai/qwen2/hub/models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1
OUTPUT="${REPO}/outputs/agentdojo/mapped_progent_slack_user_task_12_2026-09-24"
PORT=8122
mkdir -p "${OUTPUT}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
"${SERVER_PYTHON}" -m vllm.entrypoints.openai.api_server \
  --model "${MODEL_PATH}" --served-model-name Qwen/Qwen2.5-3B-Instruct \
  --host 127.0.0.1 --port "${PORT}" --dtype float16 --max-model-len 8192 \
  --gpu-memory-utilization 0.98 --max-num-seqs 4 --disable-log-requests --enforce-eager \
  >"${OUTPUT}/model-${SLURM_JOB_ID}.log" 2>&1 &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT
for _ in $(seq 1 240); do
  if curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then break; fi
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then exit 1; fi
  sleep 5
done
curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null
cd "${REPO}"
"${TASK_PYTHON}" -m scripts.run_mapped_progent_policy_eval \
  --port "${PORT}" --output-dir "${OUTPUT}"
