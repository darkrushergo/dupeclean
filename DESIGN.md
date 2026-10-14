# Dupeclean 设计方案（已确认）

> 依据 `requirements-answers.md` 的 26 项确认结果。
> **用户已二次确认以下决策**（2026-10-14）：
> - A 组改为「归一化文件名相同 + 大小相同」（未开哈希时）；开 `--hash` 则升级为哈希级证据。
> - A2 = 同名但大小/内容不同。
> - B 默认**成对列出** `pair`，可选 `--cluster group` 连通分组。
> - **保留 `--hash` 可选开关，且默认关闭。**
>
> 实现已落地于 `dupeclean.py`，使用说明见 `README.md`。

---

## 1. 从问卷答案推导出的核心结论

| 编号 | 决策 | 对设计的影响 |
|---|---|---|
| form | 单脚本 + 子命令 | `dupeclean.py scan` / `dupeclean.py compare` |
| scope | 跨盘 + 同盘内 | 对比不区分盘，同盘重复也报 |
| batch | 不需要批次 | 一次传入多个 CSV 即可 |
| hash_algo | SHA-256 默认可切 | **保留为可选能力**（见下条被推翻） |
| **hash_prescreen** | **自定义**：不做大小分级，扫描时排除 < 50MB | 扫描默认 `--min-size 50M`，可改 |
| **hash_bigfile** | **自定义：不获取哈希值** | ⚠️ 扫描默认**不算哈希**；对比不再有"哈希相同"这一铁证 |
| **parallel** | **自定义：不做哈希** | 无多进程哈希；扫描为单线程遍历 + 顺序 IO |
| ignore_default | 推荐忽略规则 | 内置忽略目录 + 隐藏项 |
| ext_filter | 忽略临时/垃圾扩展 + 白名单 | `.tmp/.part/.crdownload/Thumbs.db/desktop.ini` 等 |
| symlink | 跳过 | `os.walk(followlinks=False)` + `lstat` 判定 |
| empty_file | 排除 | size==0 不入索引 |
| hardlink | 分别记录，统计去重 | 记录每条路径，用 `(st_dev, st_ino)` 去重 |
| sim_algo | rapidfuzz | 缺失时按 deps 决策降级 difflib |
| sim_threshold | 0.75 | `--threshold` 可调 |
| sim_normalize | NFC + casefold | 归一化后再算相似度 |
| season_episode | 识别并惩罚 | 解析 `S01E02/第1集/EP12` 等并降权 |
| sim_sizegate | 只看文件名相似度 | ⚠️ **不叠加大小条件**（正因如此需要"分块/倒排"防 O(n²)） |
| csv_format | 自定义自包含 + schema_version | 见 §4 |
| csv_encoding | 标准 UTF-8（无 BOM） | |
| report_format | Markdown 总报告 + 分类 CSV | |
| interactive_mark | 不做交互 | 纯非交互、可脚本化 |
| reclaim_stats | 不要 | 不统计可回收空间 |
| deps | 有 rapidfuzz/xxhash 就用，否则降级 | 运行时能力探测 |
| resume | V1 不做 | |
| label | 从挂载路径自动推断 | 最长匹配 `/proc/mounts` 挂载点 |
| modules | 单文件 `dupeclean.py` | |

---

## 2. ⚠️ 需要你拍板的 4 个语义变化（因"不算哈希"）

原需求是「A. 哈希完全相同组 + B. 文件名相似候选组」。现在没有哈希，A 组失去"字节级相同"的证据。建议改为：

1. **A 组定义（疑似精确重复）**：`归一化文件名相同 且 大小相同`。
   - 作为对照，单独列出 **A2：同名但大小不同**（很可能是不同清晰度/压制版本，正是你要看的 720P vs IMAX）。
   - 即 A 是"极可能是同一文件"，A2 是"同名不同版本"。
2. **B 组定义（相似候选）**：归一化文件名相似度 ≥ 0.75（不叠加大小条件），并做季集号惩罚。
3. **B 的输出形态**：
   - 方案甲（默认，推荐）：**成对列表 `pair`**，每行一对 + 相似度，避免 A≈B、B≈C 被连锁合并成大杂烩；
   - 方案乙：**连通分组 `group`**（transitive 聚类），更接近"组"但易误连。
   - 我建议默认 `pair`，提供 `--cluster group` 切换。
4. **是否保留哈希作为可选开关**：建议保留 `--hash`（默认关，符合你的决策）；一旦开启，A 组升级为"哈希相同"，报告更硬。你不想要就彻底删掉。

---

## 3. 架构与模块划分（单文件内部逻辑分区）

```
dupeclean.py
├─ 常量与默认值            DEFAULTS / IGNORE_DIRS / IGNORE_EXTS / SEASON_PATTERNS
├─ 通用工具
│   ├─ human_size()                     人类可读大小
│   ├─ infer_label(path)                /proc/mounts 最长匹配 → 盘标签
│   ├─ normalize_name(name)             NFC + casefold
│   ├─ parse_season_episode(name)       → (season, episode, kind) 或 None
│   ├─ ext_of(name) / is_ignored_ext()
│   └─ iter_target_files(roots, cfg)    生成器：递归+过滤+只读
├─ 哈希后端（可选）
│   ├─ detect_capabilities()            探测 rapidfuzz / xxhash
│   └─ hash_file(path, algo, chunk)     流式分块；默认不调用
├─ 相似度后端
│   ├─ similarity(a, b)                 能力探测：rapidfuzz.ratio 或 difflib
│   └─ adjusted_similarity(a, b)        施加季集号惩罚
├─ scan 子命令
│   ├─ cmd_scan(args)
│   └─ write_index_csv(rows, meta, path) 原子写 + 元数据头
├─ compare 子命令
│   ├─ load_indexes(paths)              流式读取多个 CSV + schema 校验
│   ├─ group_exact(records)             A / A2 分组
│   ├─ build_candidate_pairs(records)   n-gram/Token 倒排 → 候选对（防 O(n²)）
│   ├─ score_pairs(candidates)          精确相似度 + 季集惩罚
│   └─ cluster_pairs(pairs)             可选连通分组
├─ 报告渲染
│   ├─ render_markdown(...)
│   └─ write_groups_csv(...)
└─ main() / argparse（子命令 scan / compare）
```

---

## 4. CSV 索引格式（自包含，schema_version = 1）

```
# dupeclean-index
# schema_version: 1
# tool_version: 1.0.0
# generated_at: 2026-10-14T03:20:00Z
# hostname: ...
# source_label: DISK_A
# source_root: /media/user/DISK_A
# min_size: 52428800
# follow_symlinks: false
# include_hidden: false
# hashed: false
schema_version,source_label,source_root,rel_path,filename,ext,size,mtime_epoch,mtime_iso,inode,device,hash_algo,hash
```

- 元数据以 `#` 开头（`pandas.read_csv(..., comment='#')` 仍可读，`dupeclean` 自己跳过）。
- 默认 `hashed: false` → `hash_algo/hash` 为空。开启 `--hash` 才填充。
- 硬链接：每个路径一行；`device,inode` 用于去重与统计。
- 写入策略：先写 `*.csv.tmp`，结束后 `os.replace` 原子改名。

---

## 5. 扫描流程（伪代码）

```
cmd_scan(paths, out, label=None, min_size=50MB, hash=False, algo=sha256, ...):
    label = label or infer_label(paths[0])
    rows_meta = {...config snapshot...}
    with atomic_writer(out) as w:
        write_metadata(w, rows_meta); write_header(w)
        for root in paths:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [d for d in dirnames
                               if not ignored_dir(d) and not is_symlink_dir(join(dirpath,d))]
                for name in filenames:
                    p = join(dirpath, name)
                    st = os.lstat(p)
                    if S_ISLNK(st.st_mode): continue          # 跳过软链
                    if not S_ISREG(st.st_mode): continue
                    if st.st_size == 0: continue              # 排除空文件
                    if st.st_size < min_size: continue        # 排除 < 50MB
                    ext = ext_of(name).lower()
                    if ignored_ext(ext): continue
                    if whitelist and ext not in whitelist: continue
                    h = hash_file(p, algo) if hash else ""
                    w.writerow([... , st.st_size, st.st_mtime, ..., st.st_ino, st.st_dev, algo, h])
                    progress_report(every N files / every M bytes, to stderr)
```

保持只读：仅 `lstat` / `open(...,'rb')`，从不写入被扫目录。

---

## 6. 对比流程（伪代码，重点：避免 O(n²)）

```
cmd_compare(csvs, outdir, threshold=0.75, cluster="pair"):
    records = load_indexes(csvs)                    # 只留必要字段，流式读
    for r in records:
        r.norm  = normalize_name(r.filename)        # NFC + casefold
        r.stem  = strip_ext(r.norm)
        r.se    = parse_season_episode(r.norm)

    # ---- A / A2：无哈希时按 名字(+大小) ----
    exact_groups = group_by(records, key=lambda r: (r.norm, r.size))
    same_name    = group_by(records, key=lambda r: r.norm)   # 供 A2
    # 若 --hash 开启：key 换成 (r.hash,) 作为 A

    # ---- B：候选对生成（防 O(n²) 的关键）----
    index = inverted_index()                        # gram/token -> [record ids]
    for r in records:
        for g in grams(r.stem, n=2_or_3):           # 含 CJK 用 n=2，否则 n=3
            index[g].append(r.id)
        for t in tokens(r.stem):
            index["T:"+t].append(r.id)
    candidate_pairs = set()
    for ids in index.values():
        if len(ids) > HARD_CAP: ids = sample_or_skip(ids)  # 超高基名词不过度配对
        for a, b in combinations(sorted(ids), 2):
            candidate_pairs.add((a, b))

    # ---- 打分 ----
    results = []
    for a, b in candidate_pairs:
        score = similarity(records[a].stem, records[b].stem)   # rapidfuzz/difflib
        score = apply_season_penalty(score, records[a].se, records[b].se)
        if score >= threshold:
            results.append((score, a, b))
    results.sort(reverse=True)

    # ---- 输出 ----
    if cluster == "group": results = connected_components(results)
    render_markdown(outdir/"report.md", exact_groups, same_name, results, meta)
    write_groups_csv(outdir/"duplicates-exact.csv",      exact_groups)
    write_groups_csv(outdir/"similar-candidates.csv",    results)
```

**为什么这样不会爆炸**：核心是利用倒排索引（字符 n-gram + 分词 token）只对"至少共享一个片段"的文件配对，把理论 O(n²) 降到接近 O(n·平均桶大小)。对超大桶设 `HARD_CAP` 抽样/跳过并记 warning。季集号在做季集惩罚。

---

## 7. 报告产物

| 文件 | 内容 |
|---|---|
| `report.md` | 总报告：概览、A 疑似精确重复组、A2 同名不同大小、B 相似候选；每条含路径 / 来源盘 / 大小 / 相似度 |
| `duplicates-exact.csv` | A（+A2 用 `kind` 列区分）分组表 |
| `similar-candidates.csv` | B 候选对（或组）表，含 `similarity` |

报告只列"可安全保留一份（仅提示）"之类的**文字建议**，绝不执行删除；不统计可回收空间。

---

## 8. 命令行接口（草案）

```
# 扫描一块盘（默认不算哈希，排除 <50MB）
dupeclean.py scan /media/user/DISK_A -o DISK_A.index.csv
    [--label DISK_A] [--min-size 50M] [--ignore DIR ...] [--ignore-ext EXT ...]
    [--ext mkv,mp4] [--include-hidden] [--config dupeclean.ini]
    [--hash] [--algo sha256]            # 可选，默认关

# 同时对比多个索引（无需插盘）
dupeclean.py compare DISK_A.index.csv DISK_B.index.csv ... \
    -o ./report [--threshold 0.75] [--cluster pair|group]
    [--sim-algo auto|rapidfuzz|difflib] [--config dupeclean.ini]
```

配置：命令行优先；可选 `dupeclean.ini`（stdlib `configparser`，零依赖）。

---

## 9. 测试建议

1. **构造夹具**：小型临时目录树，含 `<50MB` 小文件、空文件、软链、隐藏目录、临时扩展名、大小写/重音/中日韩文件名、`S01E02` 同剧不同集、同名不同大小、同名同大小。
2. **单测点**：`normalize_name`、`parse_season_episode`、`infer_label`、扩展/忽略过滤、`adjusted_similarity`、倒排候选不遗漏。
3. **端到端**：`scan` 两棵树 → 生成两 CSV → `compare` → 校验 Markdown/CSV 内容与分组正确、且**原始文件 mtime/内容零变化**（用 `stat` 前后对比）。
4. **性能**：生成 ~20 万行合成 CSV，验证 compare 时间/内存可接受、无 O(n²) 卡死。
5. **只读校验**：全程用只读挂载或对比前后哈希/权限。

---

## 10. 请确认

- [ ] §2 的 4 个语义变化（尤其 A 组定义、B 输出 pair/group、是否保留 `--hash`）
- [ ] §4 CSV 列与 `#` 元数据头是否 OK
- [ ] §8 CLI 命名/默认值是否 OK
- [ ] 确认后我再编写 `dupeclean.py` + `README`（使用说明）+ 测试。
