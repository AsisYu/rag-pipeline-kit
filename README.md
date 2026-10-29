# rag-pipeline-kit

把任意公司的资料（PDF/JSON/导出文件）加工成**经过量化评测**的向量知识库。
转换 → 切片 → 去重 → 存库 → 召回测试，五个环节全部工具化。

## 组成
- `skill/rag-ingest-pipeline/` — Claude Code skill（方法论+脚本+踩坑手册）。装入：`ln -s $(pwd)/skill/rag-ingest-pipeline ~/.claude/skills/` 或复制目录
- `Dockerfile` — 确定性运行时（含 MinerU 解析能力，CPU 推理）
- `requirements.txt` — 非 Docker 环境（Python 3.13，建议 uv）

## 快速开始（新公司/新语料）
1. `cp skill/rag-ingest-pipeline/assets/config.template.yaml work/config.yaml` 按注释填写
2. PDF 丢进 `work/corpus_pdfs/`
3. `docker compose run --rm rag run-all`（或本机 `python rag_pipeline.py run-all`）
4. `review` 导出 chunk 人工抽查 → `eval` 出 Hit@1/Hit@3 分数
5. 分数 ≥90% 交付；不达标按 SKILL.md 的排查顺序迭代

## 性能基线（香薰语料实测，2026-10）
Hit@3 90% / Hit@1 90%(重排后) / 增量更新单文件秒级 / PDF 解析 ~140s/页(CPU VLM)

## 已知边界
- JSON/数据库导出的 schema 映射需按结构写一次性小脚本（唯一不可通用环节，原则见 skill 的 decisions.md）
- 纯数字编码查询必须走 where 过滤通道（向量对数字串天然弱）
