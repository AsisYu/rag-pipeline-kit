# 环境与操作踩坑手册（gotchas）

按故障现象索引。每条都是真实踩过的坑，不是理论风险。

## 目录

1. [代理与模型下载](#代理与模型下载)
2. [MinerU 安装与运行](#mineru-安装与运行)
3. [进程被杀（内存压力）](#进程被杀内存压力)
4. [Python 版本](#python-版本)
5. [文本层乱码](#文本层乱码)
6. [嵌入与检索](#嵌入与检索)
7. [端口冲突](#端口冲突)

## 代理与模型下载

**现象：`ValueError: Unknown scheme for proxy URL URL('socks://...')`**
系统代理变量是 `socks://` 协头时，httpx（huggingface_hub 依赖）直接崩。rag_pipeline.py 已内建自防御（摘除 socks 项）；手工跑其他脚本时先 `env -u all_proxy -u ALL_PROXY ...`。

**现象：模型下载 401 Unauthorized（cas-server.xethub.hf.co）**
新版 HF 默认走 Xet 协议，hf-mirror.com 不支持。必须 `HF_HUB_DISABLE_XET=1`。

**现象：HF 直连超时/证书错误（国内网络）**
三条路按序试：① `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`（小文件 OK）；② 大文件镜像会重定向到真实 CDN 仍被墙 → 走本机 http 代理：`http_proxy=http://127.0.0.1:port https_proxy=同`（CONNECT 隧道端到端 TLS，没有证书问题）；③ ModelScope 源：MinerU 用 `MINERU_MODEL_SOURCE=modelscope`。

**模型缓存位置**：`~/.cache/huggingface`（bge 系）、`~/.cache/docling`、MinerU 在 `~/.mineru`。换机器可整目录拷贝，省重新下载。

## MinerU 安装与运行

**安装**（pip 包名 `mineru`，当前 4.x）：
```bash
uv venv ~/mineru-venv --python 3.13        # 必须 ≤3.13
uv pip install --python ~/mineru-venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
uv pip install --python ~/mineru-venv/bin/python "mineru"
~/mineru-venv/bin/mineru-kit models download --tier standard   # VLM 模型 ~1.5GB
```
先装 CPU torch 再装 mineru，避免 pip 拉 3-5GB 的 CUDA 全家桶（无 GPU 机器纯属浪费）。

**4.x 是"文档中心守护进程"架构，不是 2.x 的单命令工具**：
```bash
mineru config set parse_server.local.mode managed   # 开启本地 standard 档引擎
mineru server start                                  # 守护进程，脱离会话存活
mineru parse <pdf> --pages all --tier standard -o <输出文件>.md
```
- **`-o` 是输出文件路径不是目录**（传目录会得到假成功）
- **`--pages all` 必须显式给**——默认只解析前 10 页
- **standard 档才带 VLM**（MinerU2.5-Pro 1.2B GGUF，llama.cpp CPU 推理 ~140s/页）；flash 档快 30 倍但对设计字体/海报排版会产生：控制符乱码（字体缺 ToUnicode）、逐字拉间距碎片、康熙部首异体字——**入库源禁止用 flash**
- 报"Local parse-server is disabled/not ready"：先 config set + server restart，服务启动要 30-90 秒

## 进程被杀（内存压力）

**现象：长跑的批处理转换/嵌入任务"无故消失"，提示 system is running low on memory。**
Claude Code 等代理会话会在系统内存紧张时回收自己启动的后台任务。对策：
- 批解析：解析本体在 MinerU 守护进程里（不受影响），客户端脚本用 `setsid nohup ... & disown` 脱离托管
- 批转换：单文件短命进程（转一份退出一份），用"产物存在即跳过"保证可续跑
- 7-8GB 内存机器：嵌入/重排模型加载前先 `free -h` 看一眼，swap 用满时先停掉无关容器

## Python 版本

**MinerU 不支持 Python 3.14+**（截至 2026-10）。用 3.13。别把 RAG 依赖装进别的项目的 venv——独立 venv（`~/mineru-venv`）。

## 文本层乱码

| 现象 | 根因 | 处理（管线已内建） |
|------|------|--------------------|
| `␁␁␁␁` 控制符图画 | 设计字体缺 ToUnicode 映射，PDF 里本来就没有可读字符 | 整行 ≥3 个控制符直接丢弃（不可恢复） |
| `C e i l i n g` 字母间空格 | 海报逐字拉间距排版被逐字形提取 | 纯英文行且单字母 token≥4 → 合并空格 |
| `书⾹`/`⼈⺠` | OCR 输出康熙部首(U+2F00)或部首补充区(U+2E80)字形 | NFKC + 部首手动映射表（补充区部分字符无 NFKC 映射） |
| `<td>` 残渣混进 chunk | VLM 对复杂表格输出 HTML 而非 Markdown | flatten_html_tables：rowspan 值下沉到每行，一条记录一行 |
| 繁体 `書香` 混在简体正文 | 品牌 logo 用繁体，VLM 忠实转录 | 量小可接受；追求极致可加 opencc 繁转简 |

## 嵌入与检索

- **Chroma 默认嵌入是英文模型**（all-MiniLM）——中文语料必须显式传中文模型的 embeddings，否则检索质量废掉
- **bge-zh-v1.5 系列查询必须加官方指令前缀**（"为这个句子生成表示以用于检索相关文章："），不加召回明显下降；bge-m3 不需要前缀
- **长数字串（编码/型号）向量检索天然弱**——入库时用 extract_rules 抽进 metadata，查询走 where 等值过滤
- **同 ID 重复写入**：Chroma 同一请求内 ID 必须唯一（切片重叠会产生相同块）→ 按"来源+内容"哈希去重后再 upsert

## 端口冲突

起服务前 `ss -tln | grep <端口>`。本套件的默认端口只有 Chroma（无服务，纯文件），无内置端口；但 MinerU doclib 用 UDS socket 不占 TCP，LiteLLM 类网关常占 4000/8000，sub2api 等杂项容器可能占 8080——编排前先查。
