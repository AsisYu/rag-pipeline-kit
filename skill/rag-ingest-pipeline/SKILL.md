---
name: rag-ingest-pipeline
description: 把公司原始资料（PDF/JSON/数据库导出/扫描件）加工成经过量化评测的向量知识库（ChromaDB），全流程=转换→切片→去重→存库→召回测试。凡是用户想搭 RAG/知识库/向量库、想让文档可问答、问 PDF 怎么转 md、问 MinerU/Docling/切片/embedding/Chroma 怎么选、抱怨检索不准、或入职新公司要处理一批资料做检索——即使用户没说"RAG"这个词，也必须使用本 skill。本 skill 携带经过实测的管线脚本与环境踩坑清单，不要从零手写。
---

# RAG 入库流水线（rag-ingest-pipeline）

一套经过实测的资料→知识库流水线。核心纪律：**每一步可量化，入库前人工审样本，防回归靠评测集**。不要凭感觉说"应该可以"——跑 eval 出数字。

## 五阶段总览

```
parse(转换) → ingest(切片+去重+嵌入+存库) → review(人工审样本) → eval(召回测试) → 交付
```

统一入口：`scripts/rag_pipeline.py`，全部配置在 `config.yaml`，黄金问题在 `eval_cases.yaml`。**换公司/换项目 = 复制 `assets/` 两个模板改配置，不改代码。**

## Step 0：环境准备

先探测环境，二选一（优先 Docker）：

- **Docker 可用** → 用本包的 `Dockerfile` 建镜像，语料目录挂载到 `/work`，`project_root: .`。镜像内已内建环境自防御（socks 代理摘除、HF_Xet 禁用）
- **无 Docker** → `uv venv --python 3.13`（MinerU 不支持 3.14+！）+ `uv pip install -r requirements.txt`。若需 PDF 解析再加装 MinerU，步骤见 `references/gotchas.md` 的"MinerU 安装"节

首次验证环境：跑一次 eval 冒烟（模型会自动下载，需网络，见 gotchas 的代理节）。

## Step 1：资料摸底（intake survey）

动手前必须回答四个问题，答案决定 config 怎么写：

1. **资料形态是什么？** PDF 扫描件/电子版？JSON/数据库导出？Word/网页导出？混合？
2. **总量多大？** 决定 parse 耗时预估（VLM 档 CPU 约 140 秒/页）
3. **有没有"数字原生"的结构化数据？**（如商品库 JSON）——它是权威数据源，优先级高于 PDF 提取
4. **有没有内容重叠的资料？**（同一产品的手册+网页+报价单）——**单一来源纪律**：同一内容只允许一个源入库，否则检索结果成对刷屏。在 `parse.skip_stems` / sources 里显式排除

形态→管线映射：

| 形态 | 路径 |
|------|------|
| PDF（含扫描/设计排版） | `parse` 子命令 → MinerU VLM standard 档 |
| JSON/数据库导出 | 写几十行 schema 映射脚本转成"每块自含"的 md（参考 decisions.md 的 schema 映射节），放 `markdown_dir` 源 |
| Word/PPT/网页 | Docling 转 md（`references/gotchas.md` 有安装法），或先转 PDF 走 VLM |
| 纯文本/md | 直接进 `markdown_dir` 源 |

## Step 2：生成配置

复制 `assets/config.template.yaml` → 工作目录的 `config.yaml`，逐节填写：

- `sources`：按摸底结果列源，**brand 词必须显式写**（检索过滤和溯源依赖它）
- `extract_rules`：把资料里的精确标识（商品编码/型号/工单号）用正则抽进 metadata——精确值查询走 where 过滤，**不依赖向量**（向量对长数字串天然弱）
- `chunk` 参数保持默认（500/80），除非评测显示问题

## Step 3：parse（PDF 类资料）

```bash
python rag_pipeline.py parse
```

- MinerU 用 **standard 档**（VLM，乱码免疫）——flash 档对设计字体/海报排版会产生检索失效级噪音，只用于快速预览
- 解析是守护进程模式（doclib），批处理客户端务必 `setsid nohup` 脱离会话跑（长任务会被会话内存管理回收）
- 解析完抽查 2-3 个产物 md，确认没有整页乱码

## Step 4：ingest（切片+去重+存库）

```bash
python rag_pipeline.py ingest            # 增量(默认)：按文件指纹只处理变动
python rag_pipeline.py ingest --rebuild  # 全量重建(改了切片参数后必须用)
```

去重策略的两条边界，不要越界：
- **删**：一字不差的复制块（hash 去重，内建）
- **留**：索引行 vs 详情段、SKU 记录间重复的卖点文案——这是多粒度和自含性设计，语义去重会误删它们

## Step 5：review（人工审样本——入库前的必经关卡）

```bash
python rag_pipeline.py review   # 导出全部 chunk 原文
```

**把样本给用户看，等确认再定稿**。重点让用户抽查：产品/主文档的规格块是否自含、表格类内容是否被切碎、有没有乱码残留。审查中暴露的问题回 Step 2/3 修，而不是改 review 文件。

## Step 6：eval（召回测试）

1. **生成黄金问题集**：抽读 10-20 个样例 chunk → 站在"终端用户真会怎么问"的角度提案 10-15 条查询（含口语化、含精确编码类、含 1-2 条 xfail 文档化限制）→ **人工确认**期望关键词 → 写入 `eval_cases.yaml`
2. `python rag_pipeline.py eval` → 输出 Hit@3 / Hit@1 百分比（重排 A/B 自动对比）
3. **判读标准**：Hit@3 ≥90% 可交付；Hit@1 低说明排序问题（先看重排是否已修复）；编码类查询失败是向量天性，走 where 过滤
4. 分数不达标时的排查顺序：切片块是否自含 → 异体字/噪音是否清净 → chunk_size → 换嵌入模型（bge-m3）

## 查询侧规则（对接聊天服务时）

- 查询必须带 bge 前缀（`config.embed.query_prefix`），管线已内建
- 精确值查询（编码/型号/订单号）：用 metadata where 过滤，不要走向量
- 检索默认走重排层（fetch_k 条召回 → cross-encoder 重排取前 k）

## 深坑手册

环境与操作的全部踩坑记录在 `references/gotchas.md`——**遇到任何安装失败、下载报错、进程被杀、结果乱码，先查它**。设计决策的依据（为什么选 VLM 档、为什么 500 字、为什么不用语义去重）在 `references/decisions.md`——改默认参数前先读它，确认新语料真的偏离了当初的前提。
