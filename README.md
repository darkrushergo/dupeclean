# Dupeclean

[![tests](https://github.com/darkrushergo/dupeclean/actions/workflows/tests.yml/badge.svg)](https://github.com/darkrushergo/dupeclean/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

只读的 **跨盘重复文件「扫描 / 对比」** 两阶段工具。

> 国内镜像（Gitee）：https://gitee.com/darkrushergo/dupeclean

> **铁律：全流程只读。** 本工具绝不删除、移动或修改任何原始文件；报告只输出清单与提示，删不删由你自己决定。

> ## ⚠️ 重要提醒 · 免责声明
> `scan` / `compare` / `relabel` / `web` 是只读的；但本项目生成的**删除名单 CSV** 与附带的**删除脚本会永久删除文件、不可恢复**（外置盘通常没有回收站）。重复判定为**启发式推断，可能误判**。**使用删除脚本前请务必：先演练（dry-run）、核对名单、保留至少一份、先备份。风险自负。**
> 完整说明见 **[DISCLAIMER.md](DISCLAIMER.md)**。

适用场景：有多块**无法同时挂载**的硬盘，想找出盘与盘之间（以及同盘内部）的重复文件。
做法是分两阶段：

1. **`scan`**：分别扫描每块盘，生成**自包含 CSV 索引**（一块盘一个 CSV）。
2. **`compare`**：不用插盘，把多个 CSV 一起读入，产出 **Markdown 报告 + 分类 CSV**。

---

## 1. 依赖

- Python 3.8+（主要面向 Ubuntu / 其它 Linux）。
- **零强制第三方依赖**，只用标准库。
- 可选加速（检测到就自动使用，否则自动降级）：
  - [`rapidfuzz`](https://pypi.org/project/rapidfuzz/)：相似度计算快很多（缺失时降级 `difflib`）。
  - [`xxhash`](https://pypi.org/project/xxhash/)：仅在开启 `--hash` 且选择 xxh64/xxh128 时需要。

```bash
pip install rapidfuzz        # 推荐（可选）
pip install xxhash           # 仅 --hash 且 --algo xxh64/xxh128 时需要
```

## 2. 快速开始

```bash
# 阶段一：插上盘 A，扫描（默认不算哈希，只收 >= 50MiB 的文件）
python3 dupeclean.py scan /media/user/DISK_A -o DISK_A.index.csv

# 插上盘 B，扫描
python3 dupeclean.py scan /media/user/DISK_B -o DISK_B.index.csv

# 阶段二：两块盘都不用插，同时对比多个索引
python3 dupeclean.py compare DISK_A.index.csv DISK_B.index.csv -o ./report
```

输出目录 `./report/`：

| 文件 | 内容 |
|---|---|
| `report.md` | 人工可读总报告（概览 + A + A2 + B） |
| `duplicates-exact.csv` | A / A2 分组明细 |
| `similar-candidates.csv` | B 相似候选明细 |
| `groups.json` | 机器可读的分组数据（供 Web UI「复核」页使用） |

---

## 3. Web 图形界面（可选，推荐新手）

不想敲命令行？启动内置的本地 Web UI（纯标准库 `http.server`，零依赖）：

```bash
python3 dupeclean.py web                       # 默认 http://127.0.0.1:8765/，并自动打开浏览器
python3 dupeclean.py web --port 9000          # 换端口
python3 dupeclean.py web --workdir /data/dc    # 指定结果目录（默认：脚本同级 results/）
python3 dupeclean.py web --no-open            # 不自动打开浏览器（服务器/无桌面环境）
```

所有索引与报告都统一放在**结果目录**里（默认是与脚本同级的 `results/`，可用 `--workdir` 或环境变量 `DUPCLEAN_WORKDIR` 覆盖）：

```
results/
├─ indexes/     # 每次扫描生成的 *.index.csv
└─ reports/     # 每次对比生成的 report.md / *.csv / groups.json
```

三个页面：

1. **① 扫描 Scan**：点选自动检测到的**真实磁盘**（已过滤 snap/loop、`overlay`/`tmpfs`、`/`、`/boot/efi` 等工作区；可勾选「显示系统挂载点」查看全部）。
   - 点磁盘时，**来源盘标签与索引名称会自动随所选磁盘变化**（仍可手动改）。
   - 索引自动保存到结果目录的 `indexes/` 下，无需手填路径。
2. **② 对比 Compare**：从下拉/标签里**点选**已有的索引 CSV（不用手输文件名），设置阈值与 B 形态 → 开始。
3. **③ 复核 · 删除名单**：把疑似重复候选按**分组卡片**展示（同一组一个框、框间留白）。
   - **按硬盘优先清理**：「硬盘」下拉可选某块盘，只列出与该盘相关的组（顶部显示各组数量），并可勾选「所选盘排最前」（该盘文件置顶、高亮）；「勾选全部所选盘副本」/「勾选本组【盘】的副本」两键快速标记。
   - **分类**：「显示」可选 **全部 / 精确相似 / 可疑相似**，把两类分开操作（精确相似 = 精确重复 + 同名不同内容；可疑相似 = 文件名相似候选）。硬盘筛选与分类、排序可叠加。
   - **排序**：可切换「组排序」（组内最大文件大小↓/文件数↓/相似度↓/类型）与「组内排序」（**大小↓**/大小↑/时间新→旧/旧→新/路径），方便你**按大小优先挑占空间大的**。
   - 每组可一键「只保留最大（勾选其余）」或「清空本组」。
   - **重复文件夹**：「重复文件夹」区按内容列出**完全重复的文件夹**。判定直接**回到原始扫描记录**（索引 CSV），并采用**递归整树比对**（Merkle 式）：只要两个文件夹的整棵子树结构、文件名、大小一致（若算了哈希则还要求哈希一致）就算重复，**与存放路径、层级深度无关**（例如 A 盘 `.../GAME/KOFXV/...` 与 B 盘 `.../GAME/...`）。只保留最高层的重复文件夹，不会在同一处层层重复。
     - 可勾选要删除的整个文件夹副本；勾选后，**其内部文件会从下面的文件比选中隐藏**（可用「隐藏已选文件夹内的文件」开关控制），避免重复处理。
     - 「只显示重复文件夹」可只看文件夹层；选中的文件夹在删除名单里会展开为「删文件 + 删空目录」。
   - 勾选要删的文件/文件夹，点「生成删除名单 CSV」→ 删除名单会**按硬盘自动拆成多个 CSV**（`delete-list-<盘名>.csv`）。

**多语言 / Language**：右上角可在 **中文 / English** 间切换（自动记住）。命令行用 `--lang auto|zh|en` 或环境变量 `DUPCLEAN_LANG`；默认按系统 locale 自动选择。

**护眼主题**：右上角「护眼主题」可切换 7 套配色，选择会记在浏览器里。长时间看上万条重复候选时建议：

| 主题 | 适合场景 |
|---|---|
| **暖夜 · 低蓝光**（默认） | 长时间、暗环境；低饱和暖色，减少蓝光与眩光 |
| 中性暗灰 · 低对比 | 纯深色偏好；避免纯黑强对比的光晕 |
| Solarized Dark / Gruvbox Dark / Nord | 成熟低刺激配色，程序员常用 |
| 暖米黄 · 明亮环境 | 白天/开灯房间，纸质感暖色，比纯白柔和 |
| Solarized Light | 明亮环境，低饱和暖白 |

> 依据：纯白（#FFFFFF）亮度高易眩光；纯黑+#FFF 对比过强易产生光晕；低饱和度暖色调/低对比深灰可降低眼疲劳。

说明与安全：

- 默认只绑定 `127.0.0.1`，**仅本机可访问**，无需鉴权；不要用 `--host 0.0.0.0` 暴露到公网。
- 如果 dupeclean 跑在远程主机上，在你自己的电脑上做端口转发即可安全访问：
  `ssh -L 8765:127.0.0.1:8765 用户名@主机`，再打开 `http://127.0.0.1:8765/`。
- 同一时刻只跑一个任务（避免磁盘/CPU 争抢），重复提交会提示“已有任务在运行”。
- 预览/下载仅限结果目录里的文件，其它路径一律拒绝。
- **依然是只读**：第三页只生成“删除名单”，本工具不会删除、移动任何文件。

---

## 4. 命令详解

### 3.1 `scan` —— 扫描并生成索引

```bash
python3 dupeclean.py scan <目录...> -o <输出.csv> [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | 必填 | 输出索引 CSV 路径（原子写入） |
| `--label` | 自动推断 | 来源盘标签；默认取挂载点 basename（读 `/proc/mounts` 最长匹配） |
| `--min-size` | `50M` | 忽略小于此大小的文件；支持 `50M` / `1.5G` / `1048576` |
| `--ignore` | 内置 | 额外忽略的目录名，可重复或逗号分隔 |
| `--ignore-ext` | 内置 | 额外忽略的扩展名 |
| `--ext` | 无 | 扩展名**白名单**，给了就只收这些扩展名 |
| `--include-hidden` | 关 | 包含隐藏文件/目录（默认忽略隐藏项） |
| `--follow-symlinks` | 关 | 跟随符号链接（默认跳过，防循环/重复） |
| `--hash` | **关** | 计算哈希。开启后 `compare` 的 A 组为哈希级证据 |
| `--algo` | `sha256` | 哈希算法（可切换 `sha1/md5/blake2b/...`；装了 xxhash 还有 `xxh64/xxh128`） |
| `--chunk-size` | `1M` | 哈希分块大小 |
| `--quiet` / `--print-path` | 关 | 减少 stderr 输出 / 结束时打印输出路径到 stdout |

内置默认忽略：

- 目录：`System Volume Information`、`$RECYCLE.BIN`、`.Trash`、`lost+found`、`.Spotlight-V100`、`.fseventsd` 等。
- 文件：`Thumbs.db`、`desktop.ini`、`.DS_Store` 等。
- 扩展名：`.tmp`、`.part`、`.crdownload`、`.download`、`.!ut`、`.bc!`、`.aria2` 等。
- 默认**跳过软链接、排除空文件（0 字节）、排除隐藏项**。

### 3.2 `compare` —— 对比多个索引

```bash
python3 dupeclean.py compare <索引.csv...> -o <输出目录> [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | 必填 | 输出目录（不存在会创建） |
| `--threshold` | `0.75` | 相似候选阈值（0~1） |
| `--cluster` | `pair` | B 输出形态：`pair`=成对列出；`group`=连通分组 |
| `--sim-algo` | `auto` | `auto` / `rapidfuzz` / `difflib`（auto 会探测 rapidfuzz） |
| `--max-bucket` | `100` | 倒排桶上限，超过则跳过（防 O(n²)，过大桶通常含高频词，跳过不遗漏真正的相似对） |
| `--max-pairs` | `5000000` | 候选对全局上限（内存保护） |
| `--report-limit` | `500` | `report.md` 最多展示的组/对数量（完整数据始终在 CSV 里） |
| `--max-size-diff` | `50M` | **非视频文件**大小差超过此值就不算相似候选；**视频文件忽略大小差** |
| `--no-size-gate` | 关 | 关闭大小差过滤（所有文件只按文件名相似度） |
| `--video-ext` | 内置 | 额外视为视频的扩展名（可重复/逗号分隔） |
| `--quiet` / `--print-path` | 关 | 同上 |

### 3.3 `relabel` —— 修正索引里的来源盘标签

如果早先生成的索引 `source_label` 错了，可用此命令把标签改为**与 CSV 文件名最前面的盘名一致**（如 `DISK_A.index.csv` → `DISK_A`）：

```bash
python3 dupeclean.py relabel work/indexes/*.csv            # 自动从文件名推断（默认生成 .bak 备份）
python3 dupeclean.py relabel --dry-run work/indexes/*.csv  # 只预览不写入
python3 dupeclean.py relabel --label MyDisk a.csv b.csv     # 强制指定
```

- 只改 dupeclean 自己生成的索引 CSV（元数据头 `# source_label:` 与 `source_label` 列），**不触碰任何原始数据**。
- 只处理你在命令行**显式列出**的文件（由 shell 通配），不会去扫描目录。
- 默认写前生成 `.bak`，可用 `--no-backup` 关闭。

### 3.4 `web` —— 本地图形界面

```bash
python3 dupeclean.py web [--host 127.0.0.1] [--port 8765] [--workdir DIR] [--no-open]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--workdir` | `<脚本目录>/results` | 结果目录（`indexes/` 与 `reports/`）；也支持环境变量 `DUPCLEAN_WORKDIR` |
| `--open` / `--no-open` | 开 | 是否自动打开浏览器 |

> 全局选项（放在子命令前）：`--lang auto|zh|en` 切换语言。

---

## 5. 报告怎么读

- **A. 疑似精确重复组**
  - 未开哈希：`归一化文件名相同 + 大小相同`（*推断*为同一文件，非字节级证明）。
  - 开了 `--hash`：`哈希完全相同`（字节级证据）。
- **A2. 同名但内容/大小不同**：例如同一部片的 720P 与 IMAX 版本（大小差异大），需人工判断。
- **B. 相似候选**：文件名相似度 ≥ 阈值。默认**成对列出**（`--cluster pair`），避免 A≈B、B≈C 被连锁合并成大杂烩；需要连通分组时用 `--cluster group`。
  - **大小差规则**：**非视频**文件若大小差 > `--max-size-diff`（默认 50MiB）则不算可疑；**视频文件忽略大小差**（为的是抓住 720P vs IMAX 这种体积差很大的同一部片）。

相似度算法做了：

- **Unicode NFC 归一化 + casefold**：`CAFÉ` 与 `café` 视为相同。
- **季集号识别与惩罚**：`S01E02`、`1x02`、`第3集`、`EP12` 等；同剧**不同集**会被排除（相似度判 0），避免误报。
- 文件名比较基于**去掉扩展名的词干**，可跨容器格式匹配（如 `.mkv` 与 `.mp4`）。

> 所有报告都只是清单与文字提示，**不会自动删任何东西**。

---

## 6. 删除名单与删除脚本

复核页生成的删除名单**按硬盘拆分**（因为一个报告可能包含多个盘的文件）：

```
reports/r1/
├─ delete-list-DISK_A.csv
├─ delete-list-DISK_B.csv
└─ ...
```

CSV 列：`group_id,kind,action,source_label,path,filename,size,size_human,mtime_iso,hash`。`action` 有两种：

- `delete`：删除该文件。
- `rmdir`：仅当目录为空时删除（用于清理重复文件夹；不会递归删除）。

**dupeclean 主程序永远只读、不会删除。** 真正删除由 `scripts/` 下的独立脚本完成：

| 系统 | 脚本 |
|---|---|
| Linux / macOS | `scripts/delete-files.sh` |
| Windows | `scripts/delete-files.ps1`（便捷启动器 `scripts/delete-files.bat`，可把 CSV/目录拖上去） |

两套脚本**行为一致且默认演练（dry-run）**，必须显式确认才真删：

```bash
# Linux：先演练（只列出）
./scripts/delete-files.sh --map "/run/media/user/DISK_A=/mnt/disk_a" results/reports/r1
# 确认后加 --yes 真正删除
./scripts/delete-files.sh --yes --map "/run/media/user/DISK_A=/mnt/disk_a" results/reports/r1
```

```powershell
# Windows：先演练
.\scripts\delete-files.ps1 -Map "/run/media/user/DISK_A=E:\" -Path results\reports\r1
# 确认后加 -Yes 真正删除
.\scripts\delete-files.ps1 -Yes -Map "/run/media/user/DISK_A=E:\" -Path results\reports\r1
```

安全设计：

- 传目录时自动读取其中所有 `delete-list-*.csv`。
- **只处理 `action=delete` / `action=rmdir` 的行**，`action=keep`（保留）的不会被删。
- `delete` 只删常规文件；`rmdir` **只删空目录、不递归**（先删文件、再删空目录，安全）。
- 默认演练；必须 `--yes`（Linux）/ `-Yes`（Windows）才真删。
- 路径前缀重映射：因为扫描记录的是当时的挂载路径，换机/换盘符后用 `--map OLD=NEW` / `-Map "OLD=NEW"` 替换；也可在同目录放 `delete-map.txt`（每行 `OLD=NEW`）自动加载，示例见 `scripts/delete-map.txt.sample`。
- 缺失文件、目录、失败项都会单独计数，不会误删。

---

## 7. 索引 CSV 格式（自包含，`schema_version = 1`）

```
# dupeclean-index
# schema_version: 1
# tool_version: 1.0.0
# generated_at: 2026-10-14T11:15:57+08:00
# source_label: DISK_A
# source_root: /media/user/DISK_A
# min_size: 52428800
# follow_symlinks: false
# include_hidden: false
# hashed: false
# hash_algo:
schema_version,source_label,source_root,rel_path,filename,ext,size,mtime_epoch,mtime_iso,inode,device,hash_algo,hash
```

- 以 `#` 开头的行是元数据；标准工具可用 `pandas.read_csv(path, comment="#")` 读取。
- 硬链接：**每个路径单独一行**（`device,inode` 可用于自行去重统计）。
- 写入是**原子**的（先写 `.tmp`，成功后 `os.replace`）。

---

## 8. 配置文件（可选）

支持 INI（`configparser`，零依赖）。默认尝试读取**当前目录的 `dupeclean.ini`**，也可 `--config 路径` 指定。命令行参数优先级更高。

示例见 [`examples/dupeclean.ini.sample`](examples/dupeclean.ini.sample)：

```ini
[scan]
min_size = 50M
ignore = .hidden, _trash
ignore_ext = .log, .bak
include_hidden = false
hash = false

[compare]
threshold = 0.75
cluster = pair
sim_algo = auto
```

```bash
python3 dupeclean.py --config dupeclean.ini scan /media/user/DISK_A -o A.csv
python3 dupeclean.py --config dupeclean.ini compare A.csv B.csv -o ./report
```

---

## 9. 性能建议

- 默认**不算哈希**（按你的需求），扫描很快；`compare` 主要开销在相似度打分。
- `pip install rapidfuzz` 可显著加速（纯 C 实现）。
- 候选生成采用**字符 n-gram + 分词倒排索引**，避免 O(n²)；`--max-bucket` 控制单桶规模。
- 实测：30,000 条记录、约 47 万候选对，`difflib` 降级下约 11 秒、峰值内存约 140MB。
- 目标使用场景下（`--min-size 50M`）实际参与比对的多为大体积媒体文件，数量通常远小于全盘文件数。

---

## 10. 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：工具函数（大小解析 / 归一化 / 季集解析 / 相似度）、扫描过滤（隐藏 / 空文件 / 软链 / 临时扩展名）、A/A2/B 分组、哈希模式、连通分组、配置读取、schema 校验、Web API（扫描/对比/报告/下载/越权 404/忙时 409），以及**只读校验**（对比前后原始文件内容不变）。

---

## 11. 只读保证

- 扫描只 `lstat` 与只读打开（`open(..., "rb")`），从不写入被扫目录。
- 对比阶段完全不访问原始文件，只读 CSV。
- 输出全部写入 `-o` 指定位置，且原子替换。
- 报告仅给“提示”，**没有任何删除/移动逻辑**。
- `relabel` 只改 dupeclean 自己生成的索引 CSV，不碰原始数据。
- 真删除只存在于 **独立脚本** `scripts/delete-files.*`，且默认演练、需显式确认；主程序永远不会调用它们。
