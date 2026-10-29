#!/usr/bin/env python3
"""RAG 入库流水线统一入口：parse → (clean) → ingest → eval，配置全部来自 config.yaml

子命令：
  parse           把 config.parse.pdf_dirs 里的 PDF 逐份喂给 MinerU standard 档(VLM)
  ingest          切片+去重+嵌入+写向量库（--rebuild 推倒重建，默认增量 upsert）
  eval            跑 eval_cases.yaml 黄金问题集，输出命中率
  review          导出全部 chunk 原文供人工审查
  run-all         parse → ingest → eval 一条龙

换公司/换项目：只改 config.yaml + eval_cases.yaml，不动本文件。
"""
import argparse
import hashlib
import html.parser
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import chromadb
import yaml
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

# 环境自防御(内建，不依赖调用者记得)：socks:// 协头的系统代理会让 httpx 崩；
# hf-mirror 不支持 Xet 协议会 401，默认禁用
for _k in ("all_proxy", "ALL_PROXY"):
    if os.environ.get(_k, "").startswith("socks"):
        os.environ.pop(_k, None)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

HERE = Path(__file__).resolve().parent


def _load_yaml(name):
    """config 与评测集从当前工作目录(语料旁)读取，回退到脚本目录——Docker 与本机两种布局都兼容"""
    for base in (Path.cwd(), HERE):
        p = base / name
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")), p
    raise FileNotFoundError(f"未找到 {name}（当前目录 {Path.cwd()} 或 {HERE}）")


CFG, CFG_PATH = _load_yaml("config.yaml")
# 语料/向量库根目录 = config.yaml 所在目录 + project_root(相对路径)
ROOT = (CFG_PATH.parent / CFG.get("project_root", ".")).resolve()


# ---------- 预处理步骤注册表：config 里按名字引用 ----------

def _nfkc(text):
    """NFKC 归一 + 部首补充区手动映射：异体字(书⾹/⼈⺠)变标准字形，否则检索失效"""
    radix = str.maketrans({
        "⺠": "民", "⺀": "冫", "⺄": "十", "⺊": "卜", "⺏": "亠",
        "⺓": "匚", "⺘": "匕", "⺥": "冬", "⺩": "况", "⺫": "几",
        "⺬": "凵", "⺰": "刀", "⺳": "匆", "⺶": "卩", "⺸": "厂",
        "⺹": "厶", "⺼": "又", "⻋": "车", "⻌": "巨", "⻐": "巛",
        "⻓": "干", "⻖": "廴", "⻘": "廾", "⻚": "弋", "⻝": "食",
        "⻞": "彡", "⻠": "彳", "⻣": "心", "⻦": "扌", "⻩": "日",
        "⻬": "月", "⻯": "木", "⻲": "欠", "⻵": "毛",
    })
    return unicodedata.normalize("NFKC", text).translate(radix)


def _page_markers(text):
    """剥页码注释，返回 (正文, 最后页码)"""
    pages = re.findall(r"<!--\s*page (\d+) of \d+\s*-->", text)
    return re.sub(r"<!--\s*page \d+ of \d+\s*-->", " ", text), (pages[-1] if pages else None)


def _flatten_html_tables(text):
    """VLM 对复杂报价表输出 HTML(含 rowspan/colspan)，直接切会拦腰碎——展开成每行一条自含记录"""
    class _Grid(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self._r, self._c, self._occ, self._buf = [], -1, 0, {}, None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._r += 1
                self._c = 0
                self.rows.append([])
                # 上方 rowspan 延续到本行的格子：值实体化落位，保证每条记录字段对齐且自含
                for (rr, cc), val in list(self._occ.items()):
                    if rr == self._r:
                        self.rows[self._r].append((cc, val))
            elif tag == "td":
                while (self._r, self._c) in self._occ:
                    self._c += 1
                a = dict(attrs)
                self._buf = [int(a.get("rowspan", 1)), int(a.get("colspan", 1)), []]

        def handle_data(self, d):
            if self._buf is not None:
                self._buf[2].append(d)

        def handle_endtag(self, tag):
            if tag == "td" and self._buf:
                rs, cs, txt = self._buf
                txt = re.sub(r"\s+", " ", "".join(txt)).strip()
                self.rows[self._r].append((self._c, txt))
                for dc in range(cs):
                    for dr in range(1, rs):
                        self._occ[(self._r + dr, self._c + dc)] = txt
                self._c += cs
                self._buf = None

    out, last = [], 0
    for m in re.finditer(r"<table>.*?</table>", text, re.S):
        out.append(text[last:m.start()])
        g = _Grid()
        g.feed(m.group(0))
        rows = [sorted(r) for r in g.rows if r]
        paras, header, data = [], None, []
        for r in rows:
            if len(r) <= 2:
                t = " ".join(x[1] for x in r if x[1])
                if t:
                    paras.append(t)
            elif header is None:
                header = [x[1] for x in r]
            else:
                data.append(r)
        body = [p + "\n" for p in paras]
        for r in data:
            parts = [f"{header[c] if header and c < len(header) else f'列{c}'}:{t}"
                     for c, t in r if t and t not in ("/", "-")]
            if parts:
                body.append("- " + " ｜ ".join(parts))
        out.append("\n".join(body))
        last = m.end()
    out.append(text[last:])
    return "\n".join(out)


def _strip_images(text):
    return re.sub(r"!\[Image block\]\(doc:[^)]*\)|<!--\s*image\s*-->", " ", text)


PREPROCESS = {
    "nfkc": lambda t: (_nfkc(t), None),
    "page_markers": _page_markers,
    "flatten_html_tables": lambda t: (_flatten_html_tables(t), None),
    "strip_image_placeholders": lambda t: (_strip_images(t), None),
}


# ---------- ingest：切片 → 抽取 → 去重 → 嵌入 → 入库 ----------

def iter_chunks(only_files=None):
    """按 config.sources 逐文件预处理+切片，产出 (文本, 元数据) 列表
    only_files: 增量模式下只处理这些相对路径的文件(None=全部)"""
    c = CFG["chunk"]
    # 标题层级映射为 h1/h2/h3：context 压入和 section 元数据都依赖这套键名
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[(h, f"h{i+1}") for i, h in enumerate(c["headers"])],
        strip_headers=False)
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=c["chunk_size"], chunk_overlap=c["overlap"])

    all_chunks = []
    for name, src in CFG["sources"].items():
        src_dir = ROOT / src["dir"]
        for f in sorted(src_dir.glob("*.md")):
            rel = str(f.relative_to(ROOT))
            if only_files is not None and rel not in only_files:
                continue
            text, last_page = f.read_text(encoding="utf-8"), None
            for step in src.get("preprocess", ["nfkc"]):
                text, page = PREPROCESS[step](text)
                if page:
                    last_page = page
            docs = header_splitter.split_text(text)
            brand = src.get("brand") or name   # 品牌词务必在 config 里显式给出，这里只是兜底
            for d in docs:
                content = d.page_content.strip()
                if c.get("prepend_parent_header") and d.metadata.get("h3"):
                    ctx = " ｜ ".join(filter(None, [d.metadata.get("h2"), d.metadata.get("h1")]))
                    if ctx:
                        content = f"【{ctx}】\n{content}"
                if len(re.sub(r"\s|\|[-:|]*\||[#*`>\-]", "", content)) < c["min_content_chars"]:
                    continue
                base = {
                    "source": str(f.relative_to(ROOT)),
                    "brand": brand,
                    "doc_type": src.get("doc_type", ""),
                    "section": (d.metadata.get("h2") or d.metadata.get("h1") or "")[:80],
                }
                if last_page:
                    base["pages"] = f"1-{last_page}"
                for sub in ([content] if len(content) <= c["chunk_size"]
                            else size_splitter.split_text(content)):
                    all_chunks.append((sub, dict(base)))
    return all_chunks


def _file_hashes():
    """全源文件的 MD5 指纹：增量更新的依据"""
    out = {}
    for name, src in CFG["sources"].items():
        for f in sorted((ROOT / src["dir"]).glob("*.md")):
            out[str(f.relative_to(ROOT))] = hashlib.md5(f.read_bytes()).hexdigest()
    return out


def cmd_ingest(rebuild):
    from sentence_transformers import SentenceTransformer
    st = CFG["store"]
    client = chromadb.PersistentClient(path=str(ROOT / st["path"]))

    # 文件指纹对比：只重建变动的文件；指纹文件不存在时首轮全量
    hash_file = ROOT / st["path"] / "file_hashes.json"
    if rebuild and hash_file.exists():
        hash_file.unlink()
    old_hashes = json.loads(hash_file.read_text()) if (hash_file.exists() and not rebuild) else {}
    new_hashes = _file_hashes()
    changed = sorted(rel for rel, h in new_hashes.items() if old_hashes.get(rel) != h)
    deleted = sorted(rel for rel in old_hashes if rel not in new_hashes)
    if not rebuild and not changed and not deleted:
        print(f"无文件变动（{len(new_hashes)} 个文件指纹一致），跳过入库")
        return

    col = client.get_or_create_collection(name=st["collection"], metadata={"hnsw:space": "cosine"})
    # 变动/删除的文件先按 source 元数据清掉旧块，再写新块——不是只加不删
    for rel in changed + deleted:
        try:
            col.delete(where={"source": rel})
        except Exception:
            pass
    print(f"增量：变动 {len(changed)} 个文件，删除 {len(deleted)} 个，跳过 {len(new_hashes) - len(changed)} 个")

    chunks = iter_chunks(only_files=set(changed))
    print(f"本轮切分：{len(chunks)} 个 chunk")

    # 元数据抽取规则(config.extract_rules)：如 SKU 编码，供 where 过滤做精确查询
    for text, meta in chunks:
        for rule in CFG.get("extract_rules", []):
            m = re.search(rule["regex"], text)
            if m:
                meta.setdefault(rule["field"], m.group(1))

    # 完全相同去重(来源+内容哈希)：切片重叠产生的复制块只留一条
    unique = {}
    for text, meta in chunks:
        cid = hashlib.md5(f"{meta['source']}|{text}".encode()).hexdigest()
        unique.setdefault(cid, (text, meta))
    dropped = len(chunks) - len(unique)
    print(f"去重：丢弃 {dropped} 条完全重复（稳定 ID 即内容哈希，重复块已被覆盖）")

    emb_cfg = CFG["embed"]
    model = SentenceTransformer(emb_cfg["model"], device="cpu")
    print(f"嵌入模型 {emb_cfg['model']} 加载完成，开始向量化...")
    ids = list(unique.keys())
    docs = [v[0] for v in unique.values()]
    metas = [v[1] for v in unique.values()]
    embs = model.encode(docs, batch_size=emb_cfg["batch_size"],
                        normalize_embeddings=True, show_progress_bar=False)

    B = 128
    for s in range(0, len(ids), B):
        col.upsert(ids=ids[s:s + B], documents=docs[s:s + B],
                   metadatas=metas[s:s + B], embeddings=embs[s:s + B].tolist())
    # 指纹文件在成功入库后才写盘：中途失败下次仍会重做
    hash_file.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=1))
    print(f"入库完成：{col.count()} 条 → {ROOT / st['path']}")


# ---------- eval：黄金问题集跑分 ----------

def cmd_eval():
    from sentence_transformers import SentenceTransformer
    cases = _load_yaml("eval_cases.yaml")[0]["cases"]
    emb_cfg = CFG["embed"]
    model = SentenceTransformer(emb_cfg["model"], device="cpu")
    st = CFG["store"]
    col = chromadb.PersistentClient(path=str(ROOT / st["path"])).get_collection(st["collection"])
    k = CFG.get("eval", {}).get("top_k", 3)
    rr = CFG.get("rerank", {})
    use_rr = bool(rr.get("enabled"))
    cross = None
    if use_rr:
        from sentence_transformers import CrossEncoder
        cross = CrossEncoder(rr["model"], device="cpu", max_length=512)
        print(f"重排模型 {rr['model']} 加载完成（fetch_k={rr.get('fetch_k', 8)}）")
    fetch_k = rr.get("fetch_k", 8) if use_rr else k
    stats = {"向量": {"h1": 0, "h3": 0}, "重排": {"h1": 0, "h3": 0}} if use_rr else {"向量": {"h1": 0, "h3": 0}}
    total = len(cases)

    def hits_at(docs_in_order, case):
        """量化口径：Hit@1=第1条即答案；Hit@3=前3条含答案（期望关键词命中即算答案）"""
        def hit_at_n(n):
            for doc, meta in docs_in_order[:n]:
                blob = doc + " " + meta["source"]
                if all(any(w in blob for w in (wl if isinstance(wl, list) else [wl]))
                       for wl in case.get("expect_text", [])):
                    return True
            return False
        return hit_at_n(1), hit_at_n(3)

    passed = 0
    for case in cases:
        # 元数据精确过滤通道：case 带 where 时直接按字段查(编码/型号等精确值类查询的正确姿势)
        if case.get("where"):
            # 精确值类查询(编码/型号)：走元数据等值过滤，col.get 不需要嵌入
            conds = [{f: {"$eq": v}} for f, v in case["where"].items()]
            w = conds[0] if len(conds) == 1 else {"$and": conds}
            got = col.get(where=w, limit=k, include=["documents", "metadatas"])
            res = {"documents": [got["documents"]], "metadatas": [got["metadatas"]]}
            route = "where 过滤"
        else:
            qv = model.encode([emb_cfg["query_prefix"] + case["query"]],
                              normalize_embeddings=True)[0].tolist()
            res = col.query(query_embeddings=[qv], n_results=k)
            route = "向量"
        pairs = list(zip(res["documents"][0], res["metadatas"][0]))
        v1, v3 = hits_at(pairs, case)
        stats["向量"]["h1"] += v1
        stats["向量"]["h3"] += v3
        r1, r3 = v1, v3
        if use_rr and cross is not None and route == "向量" and len(pairs) > 1:
            # 重排通道：cross-encoder 对 (查询, 文档) 逐对打分，按精准匹配度重排
            scores = cross.predict([(case["query"], d) for d, _ in pairs])
            order = sorted(range(len(pairs)), key=lambda i: scores[i], reverse=True)
            r1, r3 = hits_at([pairs[i] for i in order], case)
        stats["重排"]["h1"] += r1
        stats["重排"]["h3"] += r3

        hit, where_src = False, ""
        for doc, meta in pairs[:k]:
            blob = doc + " " + meta["source"]
            if all(any(w in blob for w in (wl if isinstance(wl, list) else [wl]))
                   for wl in case.get("expect_text", [])):
                hit, where_src = True, meta["source"].split("/")[-1]
                break
        # xfail 用例期望失败(文档化已知限制)：失败=基线通过，反而命中则提示可移除标记
        xfail = bool(case.get("xfail"))
        ok = (not hit) if xfail else hit
        passed += ok
        if xfail:
            note = " (xfail 如预期未命中)" if not hit else " (xfail 却命中——限制已消除，可移除标记)"
        else:
            note = "" if hit else "  ← 未命中期望"
        print(f"{'✓' if ok else '✗'} {case['query']} [{route}]{note}")
        if hit:
            print(f"     ↳ {where_src}")

    def pct(x):
        return f"{x}/{total} ({x * 100 // total}%)" if total else "0"
    print("\n── 量化口径（期望关键词命中即算答案）──")
    print(f"Hit@3（前3含答案）: 向量 {pct(stats['向量']['h3'])}" +
          (f" → 重排 {pct(stats['重排']['h3'])}" if use_rr else ""))
    print(f"Hit@1（第1即答案）: 向量 {pct(stats['向量']['h1'])}" +
          (f" → 重排 {pct(stats['重排']['h1'])}" if use_rr else ""))
    print(f"评测基线（xfail 口径）：{passed}/{len(cases)}")
    return passed


# ---------- parse：MinerU VLM 批量解析 ----------

def cmd_parse():
    p = CFG["parse"]
    out_dir = ROOT / p["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, MINERU_MODEL_SOURCE="modelscope")
    ok = fail = skip = 0
    for pdf_dir in p["pdf_dirs"]:
        for pdf in sorted((ROOT / pdf_dir).glob("*.pdf")):
            if pdf.stem in p.get("skip_stems", []):
                skip += 1
                continue
            out = out_dir / f"{pdf.stem}.md"
            if out.exists():
                skip += 1
                continue
            print(f"解析: {pdf.stem}")
            r = subprocess.run([CFG["paths"]["mineru_cli"], "parse", str(pdf),
                                "--pages", "all", "--tier", p["tier"],
                                "--wait", str(p["timeout"]), "-o", str(out)],
                               capture_output=True, text=True, env=env, timeout=p["timeout"] + 120)
            if out.exists():
                ok += 1
                print(f"  ✓ ({out.stat().st_size}B)")
            else:
                fail += 1
                print(f"  ✗ {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-120:]}")
    print(f"parse 完成：成功 {ok} / 失败 {fail} / 跳过 {skip}")


# ---------- review：全量 chunk 原文导出 ----------

def cmd_review():
    out = ROOT / CFG["review_output"]
    with open(out, "w", encoding="utf-8") as f:
        for i, (t, m) in enumerate(iter_chunks(), 1):
            f.write(f"===== chunk {i:03d} | {m['source']} | {len(t)}字 =====\n{t}\n\n")
    print(f"已导出 {out}")


def main():
    ap = argparse.ArgumentParser(description="RAG 入库流水线")
    ap.add_argument("command", choices=["parse", "ingest", "eval", "review", "run-all"])
    ap.add_argument("--rebuild", action="store_true", help="推倒重建集合")
    a = ap.parse_args()
    if a.command == "parse":
        cmd_parse()
    elif a.command == "ingest":
        cmd_ingest(a.rebuild)
    elif a.command == "eval":
        cmd_eval()
    elif a.command == "review":
        cmd_review()
    elif a.command == "run-all":
        cmd_parse()
        cmd_ingest(rebuild=True)
        cmd_eval()


if __name__ == "__main__":
    main()
