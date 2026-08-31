#!/usr/bin/env bash
#SBATCH --job-name=cg-ad-audit
#SBATCH --account=pi_juanzhai_umass_edu
#SBATCH --partition=superpod-a100
#SBATCH --qos=normal
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=01:30:00
#SBATCH --array=0-7%8
#SBATCH --output=outputs/agentdojo/benchmark_audit_2026-08-30/slurm/slurm-%A_%a.out
#SBATCH --error=outputs/agentdojo/benchmark_audit_2026-08-30/slurm/slurm-%A_%a.err

set -euo pipefail

REPO_DIR=/work/pi_juanzhai_umass_edu/chetan/CausalGuard
OUTPUT_ROOT=${REPO_DIR}/outputs/agentdojo/benchmark_audit_2026-08-30
MODEL_PATH=/datasets/ai/qwen3/hub/models--Qwen--Qwen3-32B/snapshots/9216db5781bf21249d130ec9da846c4624c16137
SERVED_MODEL=Qwen/Qwen3-32B
VLLM_PYTHON=${VLLM_PYTHON:-/work/pi_juanzhai_umass_edu/gehaozhang/conda/envs/Unsloth/bin/python}
SHARD_COUNT=8
BASE_SEED=20260830
PORT=$((20000 + (SLURM_ARRAY_JOB_ID + SLURM_ARRAY_TASK_ID * 997) % 20000))
SHARD_DIR=${OUTPUT_ROOT}/shards/shard-$(printf '%02d' "${SLURM_ARRAY_TASK_ID}")
JOB_TMP=${TMPDIR:-/tmp}/causalguard-agentdojo-audit-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}

mkdir -p "${SHARD_DIR}" "${JOB_TMP}/xdg-cache"
cd "${REPO_DIR}"

if [[ ! -f "${OUTPUT_ROOT}/run_manifest.json" ]]; then
    echo "Missing prepared run manifest: ${OUTPUT_ROOT}/run_manifest.json" >&2
    exit 1
fi

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export XDG_CACHE_HOME="${JOB_TMP}/xdg-cache"

{
    printf 'slurm_array_job_id=%s\n' "${SLURM_ARRAY_JOB_ID}"
    printf 'slurm_array_task_id=%s\n' "${SLURM_ARRAY_TASK_ID}"
    printf 'model=%s\n' "${SERVED_MODEL}"
    printf 'model_path=%s\n' "${MODEL_PATH}"
    printf 'provider=local\n'
    printf 'vllm_python=%s\n' "${VLLM_PYTHON}"
    printf 'port=%s\n' "${PORT}"
    printf 'shard_count=%s\n' "${SHARD_COUNT}"
    printf 'base_seed=%s\n' "${BASE_SEED}"
    printf 'observation_only=true\n'
    printf 'policy_enforcement_enabled=false\n'
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} >"${SHARD_DIR}/job_metadata.txt"

"${VLLM_PYTHON}" -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_PATH}" \
    --served-model-name "${SERVED_MODEL}" \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --dtype bfloat16 \
    --max-model-len 16384 \
    --gpu-memory-utilization 0.95 \
    --max-num-seqs 1 \
    --disable-log-requests \
    --enforce-eager \
    >"${SHARD_DIR}/vllm-server.log" 2>&1 &
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
        tail -n 200 "${SHARD_DIR}/vllm-server.log" >&2
        exit 1
    fi
    sleep 5
done

if ! curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
    echo "vLLM server did not become healthy within 20 minutes" >&2
    tail -n 200 "${SHARD_DIR}/vllm-server.log" >&2
    exit 1
fi

.venv/bin/python scripts/run_agentdojo_benchmark_audit.py \
    --output-dir "${OUTPUT_ROOT}" \
    --port "${PORT}" \
    --model "${SERVED_MODEL}" \
    --base-seed "${BASE_SEED}" \
    --shard-count "${SHARD_COUNT}" \
    --shard-index "${SLURM_ARRAY_TASK_ID}"

printf 'completed_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${SHARD_DIR}/COMPLETED"

