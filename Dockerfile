# RAG 入库流水线运行时：转换(parse) → ingest → eval 全能力
# 国内构建： --build-arg BASE_IMAGE=<可用基础镜像> --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 使用： docker run --rm -v ~/myproject:/work -v ~/.cache/huggingface:/root/.cache/huggingface \
#           rag-pipeline-kit ingest --rebuild      (config.yaml 放在 ~/myproject 下)
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_XET=1 \
    MINERU_MODEL_SOURCE=modelscope

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# 依赖先装(变更最慢的层在前)：CPU torch → MinerU → 其余；脚本后 COPY，改脚本不触发重装依赖
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX_URL} && \
    pip install --no-cache-dir mineru --index-url ${PIP_INDEX_URL} && \
    pip install --no-cache-dir chromadb langchain-text-splitters sentence-transformers pyyaml --index-url ${PIP_INDEX_URL}

WORKDIR /app
COPY skill/rag-ingest-pipeline/scripts/rag_pipeline.py /app/
COPY skill/rag-ingest-pipeline/assets/config.template.yaml /app/config.template.yaml
COPY skill/rag-ingest-pipeline/assets/eval_cases.template.yaml /app/eval_cases.template.yaml

# 语料+config+向量库挂载点；模型缓存放卷里避免每次重建下载
WORKDIR /work
VOLUME ["/work", "/root/.cache/huggingface", "/root/.mineru"]

ENTRYPOINT ["python", "/app/rag_pipeline.py"]
CMD ["run-all"]
