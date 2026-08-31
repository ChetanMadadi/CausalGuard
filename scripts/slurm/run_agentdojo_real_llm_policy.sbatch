#!/usr/bin/env bash
#SBATCH --job-name=cg-policy-qwen
#SBATCH --account=pi_juanzhai_umass_edu
#SBATCH --partition=gpu
#SBATCH --qos=normal
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=02:00:00
#SBATCH --output=outputs/agentdojo/policy_enforcement/real_llm/slurm-%j.out
#SBATCH --error=outputs/agentdojo/policy_enforcement/real_llm/slurm-%j.err

set -euo pipefail

REPO_DIR=/work/pi_juanzhai_umass_edu/chetan/CausalGuard
MODEL_PATH=/datasets/ai/qwen3/hub/models--Qwen--Qwen3-32B/snapshots/9216db5781bf21249d130ec9da846c4624c16137
SERVED_MODEL=Qwen/Qwen3-32B
VLLM_PYTHON=${VLLM_PYTHON:-/work/pi_juanzhai_umass_edu/gehaozhang/conda/envs/Unsloth/bin/python}
PORT=$((20000 + SLURM_JOB_ID % 20000))
OUTPUT_DIR=${REPO_DIR}/outputs/agentdojo/policy_enforcement/real_llm/job-${SLURM_JOB_ID}
JOB_TMP=${TMPDIR:-/tmp}/causalguard-policy-${SLURM_JOB_ID}

mkdir -p "${OUTPUT_DIR}" "${JOB_TMP}/xdg-cache"
cd "${REPO_DIR}"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export XDG_CACHE_HOME="${JOB_TMP}/xdg-cache"

{
    printf 'slurm_job_id=%s\n' "${SLURM_JOB_ID}"
    printf 'model=%s\n' "${SERVED_MODEL}"
    printf 'model_path=%s\n' "${MODEL_PATH}"
    printf 'provider=local\n'
    printf 'vllm_python=%s\n' "${VLLM_PYTHON}"
    printf 'port=%s\n' "${PORT}"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} >"${OUTPUT_DIR}/job_metadata.txt"

"${VLLM_PYTHON}" -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_PATH}" \
    --served-model-name "${SERVED_MODEL}" \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --dtype bfloat16 \
    --max-model-len 16384 \
    --gpu-memory-utilization 0.95 \
    --max-num-seqs 4 \
    --disable-log-requests \
    --enforce-eager \
    >"${OUTPUT_DIR}/vllm-server.log" 2>&1 &
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
        tail -n 200 "${OUTPUT_DIR}/vllm-server.log" >&2
        exit 1
    fi
    sleep 5
done

if ! curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "vLLM server did not become healthy within 20 minutes" >&2
    tail -n 200 "${OUTPUT_DIR}/vllm-server.log" >&2
    exit 1
fi

.venv/bin/python scripts/run_agentdojo_real_llm_policy.py \
    --port "${PORT}" \
    --model "${SERVED_MODEL}" \
    --output-dir "${OUTPUT_DIR}"

{
    .venv/bin/python -m pytest -q
    .venv/bin/python scripts/validate_agentdojo_examples.py \
        --output-dir "${OUTPUT_DIR}/regression_examples"
    .venv/bin/python scripts/run_agentdojo_policy_demo.py \
        --output-dir "${OUTPUT_DIR}/deterministic_policy_demo"
} >"${OUTPUT_DIR}/regression_results.txt"
