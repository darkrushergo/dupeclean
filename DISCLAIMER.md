# 免责声明 / Disclaimer

## ⚠️ 重要警告（请务必阅读）

- 本项目的 `scan` / `compare` / `relabel` / `web` 是**只读**的，不会删除、移动或修改你的任何原始文件。
- 但本项目会生成**删除名单 CSV**，并附带**删除脚本**（`scripts/delete-files.sh`、`scripts/delete-files.ps1`）。这些脚本一旦确认执行，会**永久删除文件且不可恢复**——外置硬盘、U 盘、NAS 等通常**没有回收站**。
- **删除与否、删除什么，完全由你决定。** 运行删除脚本即表示你已理解并自行承担全部风险与后果。

---

## 为什么结果可能出错（重复判定并非 100% 可靠）

- 默认**不计算哈希**时，判定基于**文件名 + 大小**、**文件名相似度**、**文件夹内容签名**，属于**启发式推断**，可能**误判**（尤其是"相似候选"和"重复文件夹"）。
- 相似度算法与阈值、排序（大小/时间）、季集号处理、非视频大小差过滤等都会影响结果。
- 扫描时的过滤项（`--min-size`、隐藏文件、忽略的目录/扩展名）会**遗漏**部分文件，因此"没找到重复"**不等于**"一定没有重复"。
- 路径重映射（`--map` / `-Map` / `delete-map.txt`）若写错，可能指向**错误的文件**。
- 硬链接、符号链接、大小写敏感、Unicode 归一化等边界情况也可能带来意外。

---

## 强烈建议（动删除脚本之前）

1. **先演练**：删除脚本默认就是演练（dry-run）。先跑不带确认参数的版本，逐条核对将要删除的文件。
2. **保留至少一份**：同一组重复文件，务必始终保留一个可靠副本，再删除其余。
3. **先备份**：重要数据删除前请先备份。
4. **小范围测试**：先在一小块、非关键数据上验证整套流程。
5. **核对映射**：使用 `--map` 时，确认映射后的路径确实存在且正确。
6. **检查名单**：打开生成的 `delete-list-*.csv`，确认 `action`、`path`、来源盘无误后再执行。
7. **不要在只有一份的数据上操作。**

---

## 免责声明

本软件按"原样"（AS IS）提供，不附带任何明示或暗示的担保，包括但不限于对适销性、特定用途适用性及非侵权的担保。**作者与贡献者不对任何因使用本软件而产生的直接或间接损失负责**，包括但不限于数据丢失、误删、损坏或业务中断。使用本软件即表示你已阅读、理解并同意本声明。**删除操作不可逆，请自行承担全部风险。**

---

# Disclaimer (English)

## ⚠️ Important warning — please read

- `scan` / `compare` / `relabel` / `web` are **read-only** and never modify your files.
- However, this project produces **delete-list CSVs** and ships **delete scripts** (`scripts/delete-files.sh`, `scripts/delete-files.ps1`). Once confirmed, those scripts **permanently delete files with no recovery** — external drives, USB sticks and NAS volumes usually have **no trash**.
- **You decide what, if anything, gets deleted.** Running the delete scripts means you accept all risks and consequences.

---

## Why results can be wrong (detection is not 100% reliable)

- With hashing off (the default), detection is **heuristic**: filename + size, filename similarity, and folder content signatures. It **can produce false positives**, especially for "similar candidates" and "duplicate folders".
- Similarity algorithm/threshold, size/time sorting, season/episode handling, and the non-video size-difference filter all affect results.
- Scan filters (`--min-size`, hidden items, ignored dirs/exts) omit files, so **"no duplicate found" does not mean none exist**.
- A wrong path remap (`--map` / `-Map` / `delete-map.txt`) can point at the **wrong files**.
- Hardlinks, symlinks, case sensitivity and Unicode normalization can cause surprises.

---

## Strongly recommended (before using the delete scripts)

1. **Dry-run first.** The delete scripts are dry-run by default; review the exact list before confirming.
2. **Always keep one copy** of each duplicate group.
3. **Back up** important data before deleting.
4. **Test on a small, non-critical set** first.
5. **Verify mappings** — make sure remapped paths exist and are correct.
6. **Read the list** — open `delete-list-*.csv` and confirm `action` / `path` / disk before deleting.
7. **Never operate on data that exists in only one place.**

---

## Disclaimer

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. **IN NO EVENT SHALL THE AUTHORS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT OR INDIRECT DAMAGES** ARISING FROM THE USE OF THIS SOFTWARE, INCLUDING BUT NOT LIMITED TO DATA LOSS, ACCIDENTAL DELETION, CORRUPTION, OR BUSINESS INTERRUPTION. BY USING THIS SOFTWARE YOU ACKNOWLEDGE THAT YOU HAVE READ, UNDERSTOOD AND AGREED TO THIS DISCLAIMER. **DELETION IS IRREVERSIBLE — YOU ASSUME ALL RISK.**
