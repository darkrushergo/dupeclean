#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dupeclean.py — 只读的跨盘重复文件「扫描 / 对比」工具（两阶段）

阶段 1（scan）：把一块盘的元数据索引成自包含 CSV（默认不算哈希）。
阶段 2（compare）：不插盘，读多个 CSV，找出
    A  疑似精确重复组（归一化文件名相同 + 大小相同）
    A2 同名但大小不同
    B  文件名相似候选（相似度 >= 阈值，默认成对列出）

铁律：全流程只读；本脚本绝不删除、移动或修改任何原始文件。

用法：
    dupeclean.py scan    /media/user/DISK_A -o DISK_A.index.csv
    dupeclean.py compare DISK_A.index.csv DISK_B.index.csv -o ./report

依赖：仅标准库。若检测到 rapidfuzz / xxhash 会自动使用（可选加速）。
"""

from __future__ import annotations

import argparse
import configparser
import csv
import datetime as _dt
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import stat
import sys
import threading
import unicodedata
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse

# ───────────────────────────── 常量与默认值 ─────────────────────────────

TOOL_NAME = "dupeclean"
TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = 1

DEFAULT_MIN_SIZE = 50 * 1024 * 1024          # 50 MiB：扫描时排除更小的文件
DEFAULT_THRESHOLD = 0.75
DEFAULT_SIZE_DIFF = 50 * 1024 * 1024        # 非视频文件：大小差超过此值不算相似候选
DEFAULT_MIN_FOLDER_FILES = 2                # 重复文件夹至少包含的文件数
DEFAULT_ALGO = "sha256"
DEFAULT_CHUNK = 1024 * 1024                   # 哈希分块 1 MiB
DEFAULT_MAX_BUCKET = 100                      # 倒排桶超过此大小则跳过（防 O(n²)）
DEFAULT_MAX_PAIRS = 5_000_000                 # 候选对全局上限（内存保护）
DEFAULT_REPORT_LIMIT = 500                    # report.md 最多展示的组/对数量

# 默认忽略的目录名（大小写不敏感精确匹配）
IGNORE_DIRS = {
    "system volume information",
    "$recycle.bin",
    ".trash",
    ".trashes",
    "lost+found",
    ".spotlight-v100",
    ".fseventsd",
    ".documentrevisions-v100",
    ".temporaryitems",
}
# 默认忽略的文件名（大小写不敏感精确匹配）
IGNORE_FILES = {
    "thumbs.db",
    "desktop.ini",
    "ehthumbs.db",
    ".ds_store",
    "icon\r",
}
# 默认忽略的扩展名（含点，小写）
IGNORE_EXTS = {
    ".tmp", ".temp", ".part", ".partial", ".crdownload",
    ".download", ".!ut", ".bc!", ".aria2", ".opdownload",
}

# 视频扩展名（这些文件视为视频，相似候选时忽略大小差）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".mpe", ".m2v", ".mpv", ".ts", ".m2ts", ".mts",
    ".vob", ".rmvb", ".rm", ".3gp", ".3g2", ".ogv", ".divx", ".asf",
    ".f4v", ".mxf", ".vro", ".mod", ".tod", ".dvr-ms",
}


def build_video_ext_set(extra: Optional[Sequence[str]] = None) -> Set[str]:
    s = set(VIDEO_EXTS)
    if extra:
        s |= {("." + e.lstrip(".")).casefold() for e in extra}
    return s


def is_video(ext: str, video_exts: Set[str]) -> bool:
    return ext.casefold() in video_exts


# 相似度候选生成用的分词 / 字符 n-gram
_TOKEN_RE = re.compile(r"[0-9a-z]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_MIN_TOKEN_LEN = 3

# 季 / 集 解析
_SE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"s(\d{1,2})[\s._\-]*e(\d{1,3})", re.I), "s_e"),
    (re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I), "s_e"),
    (re.compile(r"第\s*(\d{1,2})\s*季[\s._\-]*第\s*(\d{1,4})\s*[集话話]", re.I), "s_e"),
    (re.compile(r"第\s*(\d{1,4})\s*[集话話]", re.I), "e"),
    (re.compile(r"第\s*(\d{1,2})\s*季", re.I), "s"),
    (re.compile(r"\bep?\.?\s*(\d{1,3})\b", re.I), "e"),
    (re.compile(r"\be(\d{2,3})\b", re.I), "e"),
]


# ───────────────────────────── 国际化（i18n） ─────────────────────────────

LANG = "zh"

MESSAGES = {
    "zh": {
        "err.not_dir": "[错误] 不是目录：{p}",
        "err.bad_algo": "[错误] 不支持的哈希算法：{algo}",
        "err.algo_avail": "       可用：{algos}",
        "warn.read_fail": "[警告] 无法读取 {p}: {e}",
        "scan.progress": "[扫描] {n} 个文件，{size} …",
        "scan.done": "[完成] 扫描 {n} 个文件（{size}），用时 {t:.1f}s → {out}",
        "scan.label_hash": "       来源盘标签：{label}；哈希：{hash}",
        "hash.on": "开（{algo}）",
        "hash.off": "关",
        "warn.maxpairs": "[警告] 候选对超过上限 {max}，已截断；可提高 --max-pairs 或调低 --max-bucket",
        "cmp.progress": "[对比] 打分进度 {k}/{total} …",
        "cmp.done": "[完成] A 组 {a}（{am} 个文件）；A2 组 {a2}；B 候选 {b}{unit} → {out}",
        "cmp.unit_pairs": " 对",
        "cmp.unit_groups": " 组",
        "cmp.gated": "       非视频大小差 > {size} 而剔除：{n} 对",
        "cmp.reports_hint": "       报告：report.md / duplicates-exact.csv / similar-candidates.csv / groups.json",
        "err.need_index": "[错误] compare 至少需要一个索引 CSV",
        "err.load_index": "[错误] 读取索引失败：{e}",
        "warn.empty_index": "[警告] 索引为空，无内容可比对。",
        "err.no_csv": "[错误] 不是文件：{p}",
        "relabel.done": "[完成] 处理 {n} 个文件，改动 {c} 个。",
        "relabel.dry": "[dry-run] {p} → label={label}（{rows} 行）",
        "relabel.applied": "[relabel] {p} → {label}（{rows} 行）",
        "relabel.backup_fail": "[警告] 备份失败 {p}: {e}",
        "relabel.no_header": "[跳过] {p}: 没有 CSV 表头",
        "relabel.no_col": "[跳过] {p}: 缺少 source_label 列",
        # 报告
        "rpt.title": "# Dupeclean 重复文件报告",
        "rpt.generated": "- 生成时间：{t}",
        "rpt.tool": "- 工具版本：{name} {ver}（schema_version={sv}）",
        "rpt.indexes": "- 索引文件：{n} 个",
        "rpt.index_line": "  - `{file}`（盘标签：{label}，根：`{root}`，哈希：{hashed}）",
        "rpt.records": "- 索引记录数：{n}（已排除空文件与小于 {size} 的文件）",
        "rpt.sim": "- 相似度算法：{backend}；阈值：{threshold}",
        "rpt.hash_on": "- 哈希：开（A 组为哈希级别证据）",
        "rpt.hash_off": "- 哈希：关（A 组为“同名同大小”推断）",
        "rpt.size_filter": "- 大小过滤：{rule}",
        "rpt.cand": "- 候选对生成：{pairs} 对；因桶过大跳过的桶：{skipped}",
        "rpt.disclaimer": "> ⚠️ 本工具只读，不删除、不移动任何文件。以下清单仅供人工判断。",
        "rpt.a_hash": "## A. 精确重复组（哈希完全相同）",
        "rpt.a_infer": "## A. 疑似精确重复组（归一化文件名相同 + 大小相同）",
        "rpt.a_count": "共 {n} 组，涉及 {m} 个文件。",
        "rpt.none": "_无。_",
        "rpt.a_group": "### A{gid}（{n} 份，每份 {size}）",
        "rpt.a_tip": "> 提示：极可能是同一文件，可安全保留一份（请自行核对后再决定）。",
        "rpt.rest_csv": "_其余 {n} 条见 `{file}`。_",
        "rpt.a2_hash": "## A2. 同名但内容/大小不同（可能是不同清晰度/压制版本）",
        "rpt.a2": "## A2. 同名但大小不同（可能是不同清晰度/压制版本）",
        "rpt.a2_count": "共 {n} 组。",
        "rpt.a2_group": "### A2-{gid}（{n} 份）",
        "rpt.b": "## B. 相似候选（文件名相似度 ≥ 阈值）",
        "rpt.b_pairs": "共 {n} 对候选。",
        "rpt.b_groups": "共 {n} 个相似分组（连通聚类）。",
        "rpt.b_group": "### B{gid}（{n} 份）",
        "rpt.b_tip": "> 提示：文件名相似但内容未必相同，需人工判断。",
        "rpt.hdr_disk": "来源盘", "rpt.hdr_path": "路径", "rpt.hdr_file": "文件名",
        "rpt.hdr_mtime": "修改时间", "rpt.hdr_hash": "哈希", "rpt.hdr_size": "大小",
        "rpt.hdr_sim": "相似度", "rpt.hdr_a": "盘A", "rpt.hdr_ap": "路径A",
        "rpt.hdr_as": "大小A", "rpt.hdr_b": "盘B", "rpt.hdr_bp": "路径B",
        "rpt.hdr_bs": "大小B",
        "size_rule.off": "已关闭（所有文件均只看文件名相似度）",
        "size_rule.on": "非视频文件大小差 > {size} 视为不同，不计入相似候选；视频文件忽略大小差",
        # 通用参数帮助
        "cli.desc": "只读的跨盘重复文件扫描/对比工具（两阶段）。",
        "cli.epilog": "示例：\n  dupeclean.py scan /media/user/DISK_A -o DISK_A.index.csv\n  dupeclean.py compare DISK_A.index.csv DISK_B.index.csv -o ./report\n",
        "cli.lang": "界面语言：auto/zh/en（默认 auto，按系统 locale 判断）",
        "cli.config": "可选配置文件（INI）。默认尝试读取 ./dupeclean.ini",
        "cli.scan_help": "扫描目录树，生成自包含索引 CSV",
        "cli.cmp_help": "读取多个索引 CSV，产出重复报告",
        "cli.relabel_help": "把索引 CSV 的标签改为与文件名最前面的盘名一致",
        "cli.web_help": "启动只读的本地 Web 图形界面",
    },
    "en": {
        "err.not_dir": "[error] not a directory: {p}",
        "err.bad_algo": "[error] unsupported hash algorithm: {algo}",
        "err.algo_avail": "       available: {algos}",
        "warn.read_fail": "[warn] cannot read {p}: {e}",
        "scan.progress": "[scan] {n} files, {size} …",
        "scan.done": "[done] scanned {n} files ({size}) in {t:.1f}s → {out}",
        "scan.label_hash": "       source label: {label}; hashing: {hash}",
        "hash.on": "on ({algo})",
        "hash.off": "off",
        "warn.maxpairs": "[warn] candidate pairs exceed limit {max}, truncated; raise --max-pairs or lower --max-bucket",
        "cmp.progress": "[compare] scoring {k}/{total} …",
        "cmp.done": "[done] A groups {a} ({am} files); A2 groups {a2}; B candidates {b}{unit} → {out}",
        "cmp.unit_pairs": " pairs",
        "cmp.unit_groups": " groups",
        "cmp.gated": "       dropped by non-video size diff > {size}: {n} pairs",
        "cmp.reports_hint": "       reports: report.md / duplicates-exact.csv / similar-candidates.csv / groups.json",
        "err.need_index": "[error] compare needs at least one index CSV",
        "err.load_index": "[error] failed to read index: {e}",
        "warn.empty_index": "[warn] index is empty, nothing to compare.",
        "err.no_csv": "[error] not a file: {p}",
        "relabel.done": "[done] processed {n} files, changed {c}.",
        "relabel.dry": "[dry-run] {p} → label={label} ({rows} rows)",
        "relabel.applied": "[relabel] {p} → {label} ({rows} rows)",
        "relabel.backup_fail": "[warn] backup failed for {p}: {e}",
        "relabel.no_header": "[skip] {p}: no CSV header",
        "relabel.no_col": "[skip] {p}: missing source_label column",
        "rpt.title": "# Dupeclean duplicate report",
        "rpt.generated": "- Generated: {t}",
        "rpt.tool": "- Tool: {name} {ver} (schema_version={sv})",
        "rpt.indexes": "- Index files: {n}",
        "rpt.index_line": "  - `{file}` (label: {label}, root: `{root}`, hashed: {hashed})",
        "rpt.records": "- Records: {n} (empty files and files smaller than {size} excluded)",
        "rpt.sim": "- Similarity: {backend}; threshold: {threshold}",
        "rpt.hash_on": "- Hashing: on (group A is hash-level evidence)",
        "rpt.hash_off": "- Hashing: off (group A inferred from same name + size)",
        "rpt.size_filter": "- Size filter: {rule}",
        "rpt.cand": "- Candidate pairs: {pairs}; oversized buckets skipped: {skipped}",
        "rpt.disclaimer": "> ⚠️ This tool is read-only. It never deletes or moves anything. Review manually.",
        "rpt.a_hash": "## A. Exact duplicates (identical hash)",
        "rpt.a_infer": "## A. Likely exact duplicates (same normalized name + size)",
        "rpt.a_count": "{n} groups, {m} files.",
        "rpt.none": "_none._",
        "rpt.a_group": "### A{gid} ({n} copies, {size} each)",
        "rpt.a_tip": "> Tip: very likely the same file; keeping one copy is usually safe (verify first).",
        "rpt.rest_csv": "_{n} more in `{file}`._",
        "rpt.a2_hash": "## A2. Same name but different content/size (e.g. different quality/rip)",
        "rpt.a2": "## A2. Same name but different size (e.g. different quality/rip)",
        "rpt.a2_count": "{n} groups.",
        "rpt.a2_group": "### A2-{gid} ({n} copies)",
        "rpt.b": "## B. Similar candidates (filename similarity ≥ threshold)",
        "rpt.b_pairs": "{n} candidate pairs.",
        "rpt.b_groups": "{n} similar groups (connected components).",
        "rpt.b_group": "### B{gid} ({n} copies)",
        "rpt.b_tip": "> Tip: similar filenames do not guarantee identical content; review manually.",
        "rpt.hdr_disk": "Disk", "rpt.hdr_path": "Path", "rpt.hdr_file": "Filename",
        "rpt.hdr_mtime": "Modified", "rpt.hdr_hash": "Hash", "rpt.hdr_size": "Size",
        "rpt.hdr_sim": "Similarity", "rpt.hdr_a": "Disk A", "rpt.hdr_ap": "Path A",
        "rpt.hdr_as": "Size A", "rpt.hdr_b": "Disk B", "rpt.hdr_bp": "Path B",
        "rpt.hdr_bs": "Size B",
        "size_rule.off": "disabled (filename similarity only)",
        "size_rule.on": "non-video files differing by more than {size} are not candidates; video files ignore size differences",
        "cli.desc": "Read-only cross-disk duplicate file scanner/comparer (two-phase).",
        "cli.epilog": "Examples:\n  dupeclean.py scan /media/user/DISK_A -o DISK_A.index.csv\n  dupeclean.py compare DISK_A.index.csv DISK_B.index.csv -o ./report\n",
        "cli.lang": "UI language: auto/zh/en (default auto, from locale)",
        "cli.config": "Optional INI config file. Tries ./dupeclean.ini by default",
        "cli.scan_help": "Scan a directory tree into a self-contained index CSV",
        "cli.cmp_help": "Read multiple index CSVs and produce a duplicate report",
        "cli.relabel_help": "Rewrite an index CSV's label to match the disk name at the front of its filename",
        "cli.web_help": "Start the read-only local web UI",
    },
}


def resolve_lang(value: Optional[str] = None) -> str:
    if value and value != "auto":
        return value if value in MESSAGES else "en"
    env = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
           or os.environ.get("LANG") or "")
    env = env.lower()
    if "zh" in env or "chinese" in env:
        return "zh"
    if env:
        return "en"
    return "zh"   # 无 locale 信息时默认中文（本项目主要用户）


def set_lang(value: Optional[str] = None) -> str:
    global LANG
    LANG = resolve_lang(value)
    return LANG


def t(key: str, **kw: object) -> str:
    """取当前语言的文案并格式化。"""
    table = MESSAGES.get(LANG) or MESSAGES["en"]
    text = table.get(key)
    if text is None:
        text = MESSAGES["en"].get(key, key)
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


MESSAGES["zh"].update({
    "help.paths": "要扫描的目录（可多个）",
    "help.output_csv": "输出索引 CSV 路径（原子写入）",
    "help.label": "来源盘标签（默认从挂载路径自动推断）",
    "help.min_size": "忽略小于此大小的文件（默认 50M；支持 50M/1G 等写法）",
    "help.ignore": "额外忽略的目录名（可重复，也可逗号分隔）",
    "help.ignore_ext": "额外忽略的扩展名（可重复，也可逗号分隔）",
    "help.ext": "扩展名白名单（给了就只收这些扩展名）",
    "help.include_hidden": "包含隐藏文件/目录（默认忽略隐藏项）",
    "help.follow_symlinks": "跟随符号链接（默认跳过）",
    "help.hash": "计算哈希（默认关闭；开启后 compare 的 A 组为哈希证据）",
    "help.algo": "哈希算法（默认 sha256；可选 sha1/md5/blake2b/xxh64…）",
    "help.chunk": "哈希分块大小（默认 1M）",
    "help.quiet": "减少 stderr 输出",
    "help.print_path": "结束时把输出路径打印到 stdout",
    "help.indexes": "一个或多个索引 CSV 路径",
    "help.outdir": "输出目录（不存在会创建）",
    "help.threshold": "相似候选阈值（默认 0.75）",
    "help.cluster": "B 输出形态：pair=成对列出（默认），group=连通分组",
    "help.sim_algo": "相似度算法（默认自动探测 rapidfuzz）",
    "help.max_bucket": "倒排桶上限，超过则跳过（防止 O(n²)）",
    "help.max_pairs": "候选对全局上限（内存保护）",
    "help.report_limit": "report.md 最多展示的组/对数量",
    "help.max_size_diff": "非视频文件：大小差超过此值不算相似候选（默认 50M）",
    "help.no_size_gate": "关闭大小差过滤（所有文件只按文件名相似度）",
    "help.video_ext": "额外视为视频的扩展名；视频忽略大小差",
    "help.csvs": "要修复的索引 CSV（请显式给出，例如 results/indexes/*.csv）",
    "help.dry_run": "只预览，不写入",
    "help.backup": "写入前生成 .bak 备份（默认开启）",
    "help.no_backup": "不生成备份",
    "help.host": "监听地址（默认 127.0.0.1，仅本机可访问）",
    "help.port": "监听端口（默认 8765）",
    "help.workdir": "结果目录（默认：脚本同级 results/，也可用环境变量 DUPCLEAN_WORKDIR）",
    "help.open": "启动后自动打开浏览器（默认开启）",
    "help.no_open": "不自动打开浏览器",
    "web.started": "[web] Dupeclean Web UI 已启动：{url}",
    "web.workdir": "[web] 结果目录：{dir}",
    "web.bind": "[web] 绑定：{host}:{port}（本地只读工具，无鉴权，请勿暴露到公网）",
    "web.opened": "[web] 已尝试打开浏览器；若未弹出，请手动访问上面的地址。",
    "web.open_fail": "[web] 未能自动打开浏览器，请手动复制上面的地址到浏览器。",
    "web.stop": "[web] 按 Ctrl+C 停止。",
    "web.stopped": "[web] 已停止。",
    "web.workdir_fail": "[错误] 无法创建结果目录 {dir}: {e}",
    "warn.cfg": "[警告] 配置项 [{section}] {key} 无效：{e}",
    "api.job_not_found": "任务不存在",
    "api.file_denied": "文件不可访问（仅允许下载本工具生成的文件）",
    "api.not_found": "未找到",
    "api.bad_json": "请求体不是合法 JSON",
    "api.no_report": "该报告不存在或缺少 groups.json",
    "api.read_groups_fail": "读取 groups.json 失败：{e}",
    "api.dir_denied": "报告目录不存在或不可访问",
    "api.no_groups": "该报告缺少 groups.json（请重新运行对比）",
    "api.paths_array": "paths 必须是数组",
    "api.folders_array": "folders 必须是数组",
    "api.no_selection": "没有可写入的选中项",
    "api.busy": "已有任务正在运行，请等待完成后再试",
    "api.need_path": "请至少填写一个要扫描的目录",
    "api.bad_minsize": "最小文件大小格式无效（示例：50M / 1.5G / 1048576）",
    "api.need_indexes": "请至少填写一个索引 CSV",
    "api.bad_threshold": "相似度阈值必须是数字",
    "api.threshold_range": "相似度阈值需在 0~1 之间",
    "api.bad_sizediff": "非视频大小差阈值格式无效（示例：50M）",
})

MESSAGES["en"].update({
    "help.paths": "directories to scan (one or more)",
    "help.output_csv": "output index CSV path (atomic write)",
    "help.label": "source disk label (default: inferred from mount path)",
    "help.min_size": "ignore files smaller than this (default 50M; accepts 50M/1G)",
    "help.ignore": "extra directory names to ignore (repeatable or comma-separated)",
    "help.ignore_ext": "extra extensions to ignore (repeatable or comma-separated)",
    "help.ext": "extension whitelist (only these are indexed)",
    "help.include_hidden": "include hidden files/dirs (default: ignore hidden)",
    "help.follow_symlinks": "follow symlinks (default: skip)",
    "help.hash": "compute hashes (default off; when on, group A is hash-level evidence)",
    "help.algo": "hash algorithm (default sha256; e.g. sha1/md5/blake2b/xxh64…)",
    "help.chunk": "hash chunk size (default 1M)",
    "help.quiet": "reduce stderr output",
    "help.print_path": "print the output path to stdout at the end",
    "help.indexes": "one or more index CSV paths",
    "help.outdir": "output directory (created if missing)",
    "help.threshold": "similarity threshold (default 0.75)",
    "help.cluster": "B output: pair (default) or group (connected components)",
    "help.sim_algo": "similarity algorithm (auto-detects rapidfuzz by default)",
    "help.max_bucket": "max inverted-index bucket size before skipping (avoids O(n^2))",
    "help.max_pairs": "global cap on candidate pairs (memory guard)",
    "help.report_limit": "max groups/pairs shown in report.md",
    "help.max_size_diff": "non-video files: size diff above this is not a candidate (default 50M)",
    "help.no_size_gate": "disable the size-diff filter (filename similarity only)",
    "help.video_ext": "extra extensions treated as video; video ignores size diff",
    "help.csvs": "index CSV files to fix (list them explicitly, e.g. results/indexes/*.csv)",
    "help.dry_run": "preview only, do not write",
    "help.backup": "write a .bak backup before editing (default on)",
    "help.no_backup": "do not create a backup",
    "help.host": "listen address (default 127.0.0.1, localhost only)",
    "help.port": "listen port (default 8765)",
    "help.workdir": "results directory (default: results/ next to the script; env DUPCLEAN_WORKDIR)",
    "help.open": "open the browser on start (default on)",
    "help.no_open": "do not open the browser",
    "web.started": "[web] Dupeclean Web UI started: {url}",
    "web.workdir": "[web] results directory: {dir}",
    "web.bind": "[web] bound to {host}:{port} (local read-only tool, no auth; do not expose to the internet)",
    "web.opened": "[web] tried to open the browser; if it did not appear, open the URL manually.",
    "web.open_fail": "[web] could not open the browser automatically; copy the URL above manually.",
    "web.stop": "[web] press Ctrl+C to stop.",
    "web.stopped": "[web] stopped.",
    "web.workdir_fail": "[error] cannot create results directory {dir}: {e}",
    "warn.cfg": "[warn] invalid config [{section}] {key}: {e}",
    "api.job_not_found": "job not found",
    "api.file_denied": "file not accessible (only tool-generated files can be downloaded)",
    "api.not_found": "not found",
    "api.bad_json": "request body is not valid JSON",
    "api.no_report": "report missing or has no groups.json",
    "api.read_groups_fail": "failed to read groups.json: {e}",
    "api.dir_denied": "report directory missing or inaccessible",
    "api.no_groups": "this report has no groups.json (re-run compare)",
    "api.paths_array": "paths must be an array",
    "api.folders_array": "folders must be an array",
    "api.no_selection": "nothing selected to write",
    "api.busy": "a job is already running, please wait",
    "api.need_path": "provide at least one directory to scan",
    "api.bad_minsize": "invalid minimum size (e.g. 50M / 1.5G / 1048576)",
    "api.need_indexes": "provide at least one index CSV",
    "api.bad_threshold": "threshold must be a number",
    "api.threshold_range": "threshold must be between 0 and 1",
    "api.bad_sizediff": "invalid non-video size-diff (e.g. 50M)",
})


# ───────────────────────────── 小工具 ─────────────────────────────

def eprint(*args: object, **kwargs: object) -> None:
    """输出到 stderr；若当前线程设置了日志接收器（Web 任务），则写入接收器。"""
    sink = getattr(_LOG_LOCAL, "sink", None)
    if sink is not None:
        sink.write(" ".join(str(a) for a in args) + "\n")
    else:
        print(*args, file=sys.stderr, **kwargs)


# 每个线程可绑定一个日志接收器（供 Web 任务实时抓取扫描/对比输出）
_LOG_LOCAL = threading.local()


def parse_size(text: str) -> int:
    """解析 '50M' / '1.5G' / '1048576' 之类的大小。"""
    s = str(text).strip().lower().replace("_", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?", s)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析大小：{text!r}")
    value = float(m.group(1))
    unit = m.group(2)
    factor = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3,
              "t": 1024 ** 4, "p": 1024 ** 5}[unit]
    return int(value * factor)


def human_size(n: int) -> str:
    """人类可读大小。"""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{n} B"


def iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def iso_from_epoch(epoch: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def normalize_name(name: str) -> str:
    """Unicode NFC 归一化 + casefold 统一小写。"""
    return unicodedata.normalize("NFC", name).casefold()


def strip_ext(name: str) -> str:
    """去掉最后一个扩展名（用于文件名相似度比较）。"""
    base, _ext = os.path.splitext(name)
    return base or name


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def is_hidden(name: str) -> bool:
    return name.startswith(".") and name not in (".", "..")


def infer_label(path: str) -> str:
    """从挂载路径自动推断来源盘标签（最长挂载点匹配）。"""
    target = os.path.realpath(path)
    best_mount = ""
    best_dev = ""
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mount = parts[0], parts[1]
                # 解码 /proc/mounts 的八进制转义
                mount = re.sub(r"\\([0-7]{3})",
                               lambda m: chr(int(m.group(1), 8)), mount)
                if mount == target or target.startswith(mount.rstrip("/") + "/") or mount == "/":
                    if len(mount) >= len(best_mount):
                        best_mount, best_dev = mount, dev
    except OSError:
        pass

    if best_mount and best_mount != "/":
        base = os.path.basename(best_mount.rstrip("/"))
        if base:
            return base
    if best_dev:
        base = os.path.basename(best_dev)
        if base:
            return base
    base = os.path.basename(target.rstrip("/"))
    return base or "UNKNOWN"


# ───────────────────────────── 能力探测 ─────────────────────────────

def _try_import_rapidfuzz():
    try:
        from rapidfuzz import fuzz  # type: ignore
        return fuzz
    except Exception:
        return None


def _try_import_xxhash():
    try:
        import xxhash  # type: ignore
        return xxhash
    except Exception:
        return None


_RAPIDFUZZ = _try_import_rapidfuzz()
_XXHASH = _try_import_xxhash()


def sim_backend_name(prefer: str = "auto") -> str:
    if prefer == "difflib":
        return "difflib"
    if prefer == "rapidfuzz":
        return "rapidfuzz" if _RAPIDFUZZ is not None else "difflib"
    return "rapidfuzz" if _RAPIDFUZZ is not None else "difflib"


def similarity(a: str, b: str, backend: str = "auto") -> float:
    """返回 0.0~1.0 的相似度。"""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    name = sim_backend_name(backend)
    if name == "rapidfuzz" and _RAPIDFUZZ is not None:
        return float(_RAPIDFUZZ.ratio(a, b)) / 100.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


# ───────────────────────────── 哈希（默认关闭） ─────────────────────────────

_HASHLIB_ALGOS = {"sha256", "sha1", "md5", "blake2b", "blake2s", "sha512", "sha3_256"}
_XXHASH_ALGOS = {"xxh64", "xxh128"}


def available_algos() -> List[str]:
    algos = sorted(_HASHLIB_ALGOS)
    if _XXHASH is not None:
        algos += sorted(_XXHASH_ALGOS)
    return algos


def hash_file(path: str, algo: str, chunk_size: int = DEFAULT_CHUNK) -> str:
    """流式分块读取并返回十六进制摘要。只读打开。"""
    algo = algo.lower()
    if algo in _XXHASH_ALGOS:
        if _XXHASH is None:
            raise RuntimeError(f"算法 {algo} 需要第三方库 xxhash（pip install xxhash）")
        h = _XXHASH.xxh64() if algo == "xxh64" else _XXHASH.xxh128()
    else:
        if algo not in _HASHLIB_ALGOS:
            raise RuntimeError(f"不支持的哈希算法：{algo}（可选：{', '.join(available_algos())}）")
        h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ───────────────────────────── 季 / 集 解析与惩罚 ─────────────────────────────

def parse_season_episode(name: str) -> Tuple[Optional[int], Optional[int]]:
    """从（已归一化的）文件名解析 (season, episode)，缺失为 None。"""
    for pattern, kind in _SE_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            if kind == "s_e":
                return int(m.group(1)), int(m.group(2))
            if kind == "s":
                return int(m.group(1)), None
            if kind == "e":
                return None, int(m.group(1))
        except (IndexError, ValueError):
            continue
    return None, None


def adjust_score(base: float,
                 se_a: Tuple[Optional[int], Optional[int]],
                 se_b: Tuple[Optional[int], Optional[int]]) -> float:
    """用季集号惩罚避免同剧不同集互相误报。"""
    sa, ea = se_a
    sb, eb = se_b
    if ea is not None and eb is not None:
        if ea != eb:
            return 0.0
        if sa is not None and sb is not None and sa != sb:
            return 0.0
        return base
    if (ea is None) != (eb is None):
        return base * 0.95
    if sa is not None and sb is not None and sa != sb:
        return 0.0
    return base


# ───────────────────────────── 扫描 ─────────────────────────────

def iter_target_files(roots: Sequence[str], *,
                      min_size: int,
                      include_hidden: bool,
                      ignore_dirs: Set[str],
                      ignore_files: Set[str],
                      ignore_exts: Set[str],
                      whitelist_exts: Set[str],
                      follow_symlinks: bool) -> Iterator[Tuple[str, os.stat_result]]:
    """生成器：递归产出 (路径, lstat 结果)，只读，已应用全部过滤。"""
    for root in roots:
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                   followlinks=follow_symlinks):
            kept_dirs = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if not include_hidden and is_hidden(d):
                    continue
                if d.casefold() in ignore_dirs:
                    continue
                if not follow_symlinks and os.path.islink(full):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs

            for fname in filenames:
                if not include_hidden and is_hidden(fname):
                    continue
                if fname.casefold() in ignore_files:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) and not follow_symlinks:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size == 0:
                    continue
                if st.st_size < min_size:
                    continue
                ext = ext_of(fname)
                if ext in ignore_exts:
                    continue
                if whitelist_exts and ext not in whitelist_exts:
                    continue
                yield full, st


class _AtomicWriter:
    """上下文管理器：写临时文件，成功后原子替换；失败则删除临时文件。"""

    def __init__(self, path: str):
        self.path = path
        self.tmp = path + ".tmp"
        self.fh = None

    def __enter__(self):
        self.fh = open(self.tmp, "w", encoding="utf-8", newline="")
        return self.fh

    def __exit__(self, exc_type, exc, tb):
        assert self.fh is not None
        self.fh.close()
        if exc_type is None:
            os.replace(self.tmp, self.path)
        else:
            try:
                os.unlink(self.tmp)
            except OSError:
                pass
        return False


INDEX_COLUMNS = [
    "schema_version", "source_label", "source_root", "rel_path", "filename",
    "ext", "size", "mtime_epoch", "mtime_iso", "inode", "device",
    "hash_algo", "hash",
]


def cmd_scan(args: argparse.Namespace) -> int:
    roots = [os.path.abspath(p) for p in args.paths]
    for r in roots:
        if not os.path.isdir(r):
            eprint(t("err.not_dir", p=r))
            return 2

    label = args.label or infer_label(roots[0])
    out_path = os.path.abspath(args.output)

    ignore_dirs = set(IGNORE_DIRS)
    ignore_files = set(IGNORE_FILES)
    ignore_exts = set(IGNORE_EXTS)
    if args.ignore:
        ignore_dirs |= {d.casefold() for d in args.ignore}
    if args.ignore_ext:
        ignore_exts |= {("." + e.lstrip(".")).casefold() for e in args.ignore_ext}
    include_hidden = bool(args.include_hidden)
    whitelist_exts = {("." + e.lstrip(".")).casefold() for e in (args.ext or [])}
    follow_symlinks = bool(args.follow_symlinks)
    do_hash = bool(args.hash)
    algo = (args.algo or DEFAULT_ALGO).lower()
    if do_hash and algo not in _HASHLIB_ALGOS and algo not in _XXHASH_ALGOS:
        eprint(t("err.bad_algo", algo=algo))
        eprint(t("err.algo_avail", algos=", ".join(available_algos())))
        return 2

    meta = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": iso_now(),
        "source_label": label,
        "source_root": "|".join(roots),
        "min_size": args.min_size,
        "follow_symlinks": bool_str(follow_symlinks),
        "include_hidden": bool_str(include_hidden),
        "hashed": bool_str(do_hash),
        "hash_algo": algo if do_hash else "",
    }

    scanned = 0
    total_bytes = 0
    started = _dt.datetime.now()

    with _AtomicWriter(out_path) as fh:
        # 自包含元数据头（以 '#' 开头，标准 CSV 读取器可用 comment='#' 跳过）
        fh.write(f"# {TOOL_NAME}-index\n")
        for key in ("schema_version", "tool_version", "generated_at", "source_label",
                    "source_root", "min_size", "follow_symlinks", "include_hidden",
                    "hashed", "hash_algo"):
            fh.write(f"# {key}: {meta[key]}\n")
        writer = csv.writer(fh)
        writer.writerow(INDEX_COLUMNS)

        for full, st in iter_target_files(
                roots,
                min_size=args.min_size,
                include_hidden=include_hidden,
                ignore_dirs=ignore_dirs,
                ignore_files=ignore_files,
                ignore_exts=ignore_exts,
                whitelist_exts=whitelist_exts,
                follow_symlinks=follow_symlinks):
            fname = os.path.basename(full)
            rel_path = os.path.relpath(full, roots[0])
            digest = ""
            if do_hash:
                try:
                    digest = hash_file(full, algo, args.chunk_size)
                except OSError as exc:
                    eprint(t("warn.read_fail", p=full, e=exc))
                    digest = ""
            writer.writerow([
                SCHEMA_VERSION, label, roots[0], rel_path, fname,
                ext_of(fname), st.st_size, f"{st.st_mtime:.6f}",
                iso_from_epoch(st.st_mtime), st.st_ino, st.st_dev,
                algo if do_hash else "", digest,
            ])
            scanned += 1
            total_bytes += st.st_size
            if not args.quiet and scanned % 1000 == 0:
                eprint(t("scan.progress", n=scanned, size=human_size(total_bytes)))

    elapsed = (_dt.datetime.now() - started).total_seconds()
    if not args.quiet:
        eprint(t("scan.done", n=scanned, size=human_size(total_bytes),
                 t=elapsed, out=out_path))
        eprint(t("scan.label_hash", label=label,
                 hash=(t("hash.on", algo=algo) if do_hash else t("hash.off"))))
    if args.print_path:
        print(out_path)
    return 0


def bool_str(value: bool) -> str:
    return "true" if value else "false"


# ───────────────────────────── 读取索引 ─────────────────────────────

class Record:
    __slots__ = ("idx", "source_label", "source_root", "rel_path", "filename",
                 "ext", "size", "mtime_iso", "inode", "device", "hash_algo", "hash",
                 "norm", "stem", "se")

    def __init__(self, idx: int, row: Dict[str, str]) -> None:
        self.idx = idx
        self.source_label = row.get("source_label", "")
        self.source_root = row.get("source_root", "")
        self.rel_path = row.get("rel_path", "")
        self.filename = row.get("filename", "")
        self.ext = row.get("ext", "")
        try:
            self.size = int(row.get("size") or 0)
        except ValueError:
            self.size = 0
        self.mtime_iso = row.get("mtime_iso", "")
        self.inode = row.get("inode", "")
        self.device = row.get("device", "")
        self.hash_algo = row.get("hash_algo", "")
        self.hash = row.get("hash", "")
        self.norm = normalize_name(self.filename)
        self.stem = strip_ext(self.norm)
        self.se = parse_season_episode(self.stem)

    @property
    def display(self) -> str:
        return os.path.join(self.source_root, self.rel_path) if self.source_root else self.rel_path


def load_indexes(paths: Sequence[str]) -> Tuple[List[Record], List[dict]]:
    """读取一个或多个索引 CSV，返回 (records, metas)。"""
    records: List[Record] = []
    metas: List[dict] = []
    for path in paths:
        meta: dict = {"file": path}
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            lines = []
            while True:
                pos = fh.tell()
                line = fh.readline()
                if not line:
                    break
                if line.lstrip().startswith("#"):
                    m = re.match(r"#\s*([A-Za-z0-9_]+)\s*:\s*(.*)$", line.strip())
                    if m:
                        meta[m.group(1)] = m.group(2)
                    continue
                fh.seek(pos)
                break
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ValueError(f"{path}: 空文件或缺少表头")
            missing = [c for c in INDEX_COLUMNS if c not in reader.fieldnames]
            if missing:
                raise ValueError(f"{path}: 缺少列 {missing}")
            sv = meta.get("schema_version")
            if sv is not None and str(sv) != str(SCHEMA_VERSION):
                raise ValueError(
                    f"{path}: schema_version={sv}，本工具支持 {SCHEMA_VERSION}")
            for row in reader:
                records.append(Record(len(records), row))
        metas.append(meta)
    return records, metas


def _iter_index_data_rows(path: str) -> Iterator[Dict[str, str]]:
    """流式产出索引 CSV 的数据行（跳过 # 元数据行），不构建 Record。"""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        header = None
        for line in fh:
            if line.lstrip().startswith("#"):
                continue
            header = line
            break
        if header is None:
            return
        reader = csv.DictReader(itertools.chain([header], fh))
        for row in reader:
            yield row


def _collect_folder_files(node: dict, base: str, out: List[dict]) -> None:
    for name in sorted(node["files"]):
        size, digest = node["files"][name]
        out.append({"filename": name, "path": os.path.join(base, name),
                    "size": size, "size_human": human_size(size), "hash": digest})
    for cname in sorted(node["dirs"]):
        _collect_folder_files(node["dirs"][cname], os.path.join(base, cname), out)


def compute_duplicate_folders(index_paths: Sequence[str], *,
                              min_files: int = DEFAULT_MIN_FOLDER_FILES,
                              include_files: bool = False
                              ) -> Tuple[List[dict], bool]:
    """从原始扫描记录（索引 CSV）中找出**完全重复的文件夹**（递归、与路径无关）。

    判定方式：对每个文件夹计算其**整棵子树**的 Merkle 摘要——递归地纳入子目录
    名与其摘要、以及直接包含文件的“名称 + 大小 +（若有）哈希”。摘要相同即认为
    两棵树完全重复。因此即使两个文件夹的存放路径、层级深度不同，只要内容一致
    就会被识别（例如一个游戏目录在 A 盘是 `.../GAME/KOFXV/...`，在 B 盘是
    `.../GAME/...`）。

    只返回“最高层”的重复文件夹：若某个已报告的文件夹内部还有子文件夹也重复，
    子文件夹会被抑制，避免同一份重复被层层重复列出。

    返回 (folder_groups, hashed)。folder_groups 每项形如::

        {"file_count": int, "total_size": int, "total_size_human": str,
         "exact": bool, "copies": [{"source_label","path","file_count",
                                    "total_size","total_size_human"[,"files"]}]}
    """
    # 1) 按 (来源盘, 扫描根) 建目录树
    trees: Dict[Tuple[str, str], dict] = {}
    hashed = False
    for path in index_paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            for row in _iter_index_data_rows(path):
                label = row.get("source_label") or ""
                root = row.get("source_root") or ""
                rel = (row.get("rel_path") or "").replace("\\", "/")
                name = row.get("filename") or (rel.rsplit("/", 1)[-1] if rel else "")
                if not name:
                    continue
                try:
                    size = int(row.get("size") or 0)
                except ValueError:
                    size = 0
                digest = row.get("hash") or ""
                if digest:
                    hashed = True
                tree = trees.setdefault((label, root), {"files": {}, "dirs": {}})
                node = tree
                for comp in rel.split("/")[:-1]:
                    node = node["dirs"].setdefault(comp, {"files": {}, "dirs": {}})
                node["files"][name] = (size, digest)
        except OSError:
            continue

    # 2) 递归计算每个文件夹的 Merkle 摘要与统计
    entries: List[Tuple[str, int, int, str, str, Tuple[str, ...], dict]] = []

    def walk(node: dict, label: str, root: str, parts: Tuple[str, ...]
             ) -> Tuple[str, int, int]:
        h = hashlib.sha1()
        count = 0
        total = 0
        for name in sorted(node["files"]):
            size, digest = node["files"][name]
            h.update(("f\x00%s\x00%d\x00%s\n" % (name, size, digest)).encode("utf-8"))
            count += 1
            total += size
        for cname in sorted(node["dirs"]):
            cdig, ccount, csize = walk(node["dirs"][cname], label, root, parts + (cname,))
            h.update(("d\x00%s\x00%s\n" % (cname, cdig)).encode("utf-8"))
            count += ccount
            total += csize
        digest = h.hexdigest()
        entries.append((digest, count, total, label, root, parts, node))
        return digest, count, total

    for (label, root), tree in trees.items():
        walk(tree, label, root, ())

    # 3) 按摘要分组
    groups: Dict[str, List[Tuple[str, int, int, str, str, Tuple[str, ...], dict]]] = {}
    for e in entries:
        groups.setdefault(e[0], []).append(e)

    candidates = []
    for members in groups.values():
        if len(members) < 2:
            continue
        count = members[0][1]
        if count < min_files:
            continue
        candidates.append((members[0][2], min(len(e[5]) for e in members), count, members))
    # 总量大的优先；并列时更浅（层级更少，更靠近顶层）的优先，
    # 保证祖先先被接受、其子文件夹随后被抑制
    candidates.sort(key=lambda c: (-c[0], c[1]))

    # 4) 只保留最高层：抑制位于已接受文件夹内部的子文件夹
    accepted_paths: List[str] = []
    result: List[dict] = []
    for total, _depth, count, members in candidates:
        kept = []
        group_paths = []
        for e in members:
            _dig, _cnt, _size, label, root, parts, node = e
            fpath = os.path.join(root, *parts) if parts else root
            real = os.path.realpath(fpath)
            if any(real == ap or real.startswith(ap + os.sep) for ap in accepted_paths):
                continue
            kept.append((label, fpath, node))
            group_paths.append(real)
        if len(kept) < 2:
            continue
        accepted_paths.extend(group_paths)
        copies = []
        for label, fpath, node in sorted(kept, key=lambda k: k[1]):
            copy = {
                "source_label": label,
                "path": fpath,
                "file_count": count,
                "total_size": total,
                "total_size_human": human_size(total),
            }
            if include_files:
                files: List[dict] = []
                _collect_folder_files(node, fpath, files)
                copy["files"] = files
            copies.append(copy)
        result.append({
            "file_count": count,
            "total_size": total,
            "total_size_human": human_size(total),
            "copies": copies,
            "exact": hashed,
        })
    return result, hashed

# ───────────────────────────── 候选生成 ─────────────────────────────

def char_grams(s: str, n: int) -> Set[str]:
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def make_grams(name: str) -> Set[str]:
    grams = char_grams(name, 3)
    if _CJK_RE.search(name):
        grams |= char_grams(name, 2)
    return grams


def make_tokens(name: str) -> Set[str]:
    return {t for t in _TOKEN_RE.findall(name.casefold()) if len(t) >= _MIN_TOKEN_LEN}


def build_candidate_pairs(records: Sequence[Record], *,
                          max_bucket: int,
                          max_pairs: int,
                          quiet: bool) -> Tuple[List[Tuple[int, int]], int]:
    """用倒排索引生成候选对，避免 O(n²)。返回 (pairs, skipped_buckets)。"""
    index: Dict[str, List[int]] = {}
    for r in records:
        for g in make_grams(r.stem):
            index.setdefault("G:" + g, []).append(r.idx)
        for t in make_tokens(r.stem):
            index.setdefault("T:" + t, []).append(r.idx)

    pairs: Set[Tuple[int, int]] = set()
    skipped = 0
    overflow = False
    for ids in index.values():
        if len(ids) < 2:
            continue
        if len(ids) > max_bucket:
            skipped += 1
            continue
        ids_sorted = sorted(ids)
        for i in range(len(ids_sorted)):
            a = ids_sorted[i]
            for j in range(i + 1, len(ids_sorted)):
                b = ids_sorted[j]
                pairs.add((a, b))
                if len(pairs) > max_pairs:
                    overflow = True
                    break
            if overflow:
                break
        if overflow:
            break
    if overflow and not quiet:
        eprint(t("warn.maxpairs", max=max_pairs))
    return sorted(pairs), skipped


# ───────────────────────────── 分组与聚类 ─────────────────────────────

class DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def group_by(records: Sequence[Record], key) -> List[List[Record]]:
    buckets: Dict[object, List[Record]] = {}
    for r in records:
        buckets.setdefault(key(r), []).append(r)
    return [g for g in buckets.values() if len(g) > 1]


# ───────────────────────────── 报告输出 ─────────────────────────────

def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|")


def _md_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return out


def render_markdown(path: str, *,
                    metas: Sequence[dict],
                    records: Sequence[Record],
                    exact_groups: List[List[Record]],
                    same_name_only: List[List[Record]],
                    pairs: List[Tuple[float, int, int]],
                    clusters: Optional[List[List[Record]]],
                    threshold: float,
                    backend: str,
                    hashed: bool,
                    report_limit: int,
                    n_pairs_generated: int,
                    skipped_buckets: int,
                    size_rule: str = "") -> None:
    n_exact_members = sum(len(g) for g in exact_groups)

    # 索引记录数所用的 min_size（取第一个索引的元数据）
    try:
        min_size_meta = int(str(metas[0].get("min_size"))) if metas else DEFAULT_MIN_SIZE
    except (ValueError, TypeError, KeyError):
        min_size_meta = DEFAULT_MIN_SIZE

    lines: List[str] = []
    lines.append(t("rpt.title"))
    lines.append("")
    lines.append(t("rpt.generated", t=iso_now()))
    lines.append(t("rpt.tool", name=TOOL_NAME, ver=TOOL_VERSION, sv=SCHEMA_VERSION))
    lines.append(t("rpt.indexes", n=len(metas)))
    for m in metas:
        lines.append(t("rpt.index_line", file=m.get("file"),
                       label=m.get("source_label", "?"),
                       root=m.get("source_root", "?"), hashed=m.get("hashed", "?")))
    lines.append(t("rpt.records", n=len(records), size=human_size(min_size_meta)))
    lines.append(t("rpt.sim", backend=backend, threshold=threshold))
    lines.append(t("rpt.hash_on") if hashed else t("rpt.hash_off"))
    if size_rule:
        lines.append(t("rpt.size_filter", rule=size_rule))
    lines.append(t("rpt.cand", pairs=n_pairs_generated, skipped=skipped_buckets))
    lines.append("")
    lines.append(t("rpt.disclaimer"))
    lines.append("")

    # ── A ──
    lines.append(t("rpt.a_hash") if hashed else t("rpt.a_infer"))
    lines.append("")
    lines.append(t("rpt.a_count", n=len(exact_groups), m=n_exact_members))
    lines.append("")
    if not exact_groups:
        lines.append(t("rpt.none"))
    else:
        shown = exact_groups[:report_limit]
        for gi, group in enumerate(shown, 1):
            size = group[0].size
            lines.append(t("rpt.a_group", gid=gi, n=len(group), size=human_size(size)))
            lines.append("")
            rows = [[r.source_label, r.display, r.filename, r.mtime_iso,
                     r.hash[:16] if r.hash else "-"] for r in group]
            lines += _md_table([t("rpt.hdr_disk"), t("rpt.hdr_path"), t("rpt.hdr_file"),
                                t("rpt.hdr_mtime"), t("rpt.hdr_hash")], rows)
            lines.append("")
            lines.append(t("rpt.a_tip"))
            lines.append("")
        if len(exact_groups) > len(shown):
            lines.append(t("rpt.rest_csv", n=len(exact_groups) - len(shown),
                           file="duplicates-exact.csv"))
            lines.append("")

    # ── A2 ──
    lines.append(t("rpt.a2_hash") if hashed else t("rpt.a2"))
    lines.append("")
    lines.append(t("rpt.a2_count", n=len(same_name_only)))
    lines.append("")
    if not same_name_only:
        lines.append(t("rpt.none"))
    else:
        shown = same_name_only[:report_limit]
        for gi, group in enumerate(shown, 1):
            lines.append(t("rpt.a2_group", gid=gi, n=len(group)))
            lines.append("")
            rows = [[r.source_label, human_size(r.size), r.display, r.mtime_iso]
                    for r in sorted(group, key=lambda r: -r.size)]
            lines += _md_table([t("rpt.hdr_disk"), t("rpt.hdr_size"),
                                t("rpt.hdr_path"), t("rpt.hdr_mtime")], rows)
            lines.append("")
        if len(same_name_only) > len(shown):
            lines.append(t("rpt.rest_csv", n=len(same_name_only) - len(shown),
                           file="duplicates-exact.csv"))
            lines.append("")

    # ── B ──
    lines.append(t("rpt.b"))
    lines.append("")
    if clusters is not None:
        lines.append(t("rpt.b_groups", n=len(clusters)))
    else:
        lines.append(t("rpt.b_pairs", n=len(pairs)))
    lines.append("")
    if not pairs and not clusters:
        lines.append(t("rpt.none"))
    else:
        if clusters is not None:
            shown = clusters[:report_limit]
            for gi, group in enumerate(shown, 1):
                lines.append(t("rpt.b_group", gid=gi, n=len(group)))
                lines.append("")
                rows = [[r.source_label, human_size(r.size), r.display, r.mtime_iso]
                        for r in group]
                lines += _md_table([t("rpt.hdr_disk"), t("rpt.hdr_size"),
                                    t("rpt.hdr_path"), t("rpt.hdr_mtime")], rows)
                lines.append("")
                lines.append(t("rpt.b_tip"))
                lines.append("")
            if len(clusters) > len(shown):
                lines.append(t("rpt.rest_csv", n=len(clusters) - len(shown),
                               file="similar-candidates.csv"))
                lines.append("")
        else:
            shown = pairs[:report_limit]
            rows = []
            for score, a, b in shown:
                ra, rb = records[a], records[b]
                rows.append([f"{score:.3f}", ra.source_label, ra.display,
                             human_size(ra.size), rb.source_label, rb.display,
                             human_size(rb.size)])
            lines += _md_table([t("rpt.hdr_sim"), t("rpt.hdr_a"), t("rpt.hdr_ap"),
                                t("rpt.hdr_as"), t("rpt.hdr_b"), t("rpt.hdr_bp"),
                                t("rpt.hdr_bs")], rows)
            lines.append("")
            if len(pairs) > len(shown):
                lines.append(t("rpt.rest_csv", n=len(pairs) - len(shown),
                               file="similar-candidates.csv"))
                lines.append("")
            lines.append(t("rpt.b_tip"))
            lines.append("")

    with _AtomicWriter(path) as fh:
        fh.write("\n".join(lines) + "\n")


def write_exact_csv(path: str, exact_groups: List[List[Record]],
                    same_name_only: List[List[Record]]) -> None:
    with _AtomicWriter(path) as fh:
        writer = csv.writer(fh)
        writer.writerow(["group_id", "kind", "member_index", "source_label",
                         "path", "filename", "size", "size_human", "mtime_iso", "hash"])
        gid = 0
        for kind, groups in (("exact", exact_groups),
                             ("same-name-different", same_name_only)):
            for group in groups:
                gid += 1
                for mi, r in enumerate(group, 1):
                    writer.writerow([gid, kind, mi, r.source_label, r.display,
                                     r.filename, r.size, human_size(r.size),
                                     r.mtime_iso, r.hash])


def write_similar_csv(path: str, *,
                      records: Sequence[Record],
                      pairs: List[Tuple[float, int, int]],
                      clusters: Optional[List[List[Record]]]) -> None:
    with _AtomicWriter(path) as fh:
        writer = csv.writer(fh)
        writer.writerow(["group_id", "kind", "group_size", "similarity",
                         "source_label", "path", "filename", "size", "size_human",
                         "mtime_iso"])
        if clusters is None:
            for gid, (score, a, b) in enumerate(pairs, 1):
                for r in (records[a], records[b]):
                    writer.writerow([gid, "pair", 2, f"{score:.4f}", r.source_label,
                                     r.display, r.filename, r.size,
                                     human_size(r.size), r.mtime_iso])
        else:
            # 记录每对相似度，用于给分组标注最大相似度
            pair_scores: Dict[Tuple[int, int], float] = {}
            for score, a, b in pairs:
                pair_scores[(min(a, b), max(a, b))] = max(
                    pair_scores.get((min(a, b), max(a, b)), 0.0), score)
            for gid, group in enumerate(clusters, 1):
                best = 0.0
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        key = (min(group[i].idx, group[j].idx),
                               max(group[i].idx, group[j].idx))
                        best = max(best, pair_scores.get(key, 0.0))
                for r in group:
                    writer.writerow([gid, "group", len(group), f"{best:.4f}",
                                     r.source_label, r.display, r.filename, r.size,
                                     human_size(r.size), r.mtime_iso])


def _record_to_member(r: Record) -> dict:
    return {
        "source_label": r.source_label,
        "path": r.display,
        "filename": r.filename,
        "size": r.size,
        "size_human": human_size(r.size),
        "mtime_iso": r.mtime_iso,
        "hash": r.hash,
    }


def build_groups_payload(*, exact_groups: List[List[Record]],
                         same_name_only: List[List[Record]],
                         records: Sequence[Record],
                         pairs: List[Tuple[float, int, int]],
                         clusters: Optional[List[List[Record]]],
                         threshold: float, backend: str, hashed: bool) -> dict:
    """构建给 Web UI / 外部程序使用的分组 JSON。"""
    groups: List[dict] = []
    gid = 0
    exact_label = ("精确重复（哈希相同）" if hashed
                   else "疑似精确重复（同名同大小）")
    for g in exact_groups:
        gid += 1
        groups.append({
            "id": gid, "kind": "exact", "label": exact_label, "similarity": 1.0,
            "members": [_record_to_member(r) for r in sorted(g, key=lambda x: x.display)],
        })
    for g in same_name_only:
        gid += 1
        groups.append({
            "id": gid, "kind": "same-name-different",
            "label": "同名但内容/大小不同", "similarity": None,
            "members": [_record_to_member(r) for r in sorted(g, key=lambda x: -x.size)],
        })
    if clusters is not None:
        pair_best: Dict[Tuple[int, int], float] = {}
        for score, a, b in pairs:
            key = (min(a, b), max(a, b))
            pair_best[key] = max(pair_best.get(key, 0.0), score)
        for group in clusters:
            best = 0.0
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    key = (min(group[i].idx, group[j].idx),
                           max(group[i].idx, group[j].idx))
                    best = max(best, pair_best.get(key, 0.0))
            gid += 1
            groups.append({
                "id": gid, "kind": "similar", "label": "文件名相似候选",
                "similarity": round(best, 4),
                "members": [_record_to_member(r) for r in group],
            })
    else:
        for score, a, b in pairs:
            gid += 1
            groups.append({
                "id": gid, "kind": "similar", "label": "文件名相似候选",
                "similarity": round(score, 4),
                "members": [_record_to_member(records[a]), _record_to_member(records[b])],
            })
    return {
        "schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION,
        "generated_at": iso_now(), "threshold": threshold, "backend": backend,
        "hashed": hashed, "group_count": len(groups), "groups": groups,
    }


def write_groups_json(path: str, payload: dict) -> None:
    with _AtomicWriter(path) as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ───────────────────────────── 对比 ─────────────────────────────

def cmd_compare(args: argparse.Namespace) -> int:
    if not args.indexes:
        eprint(t("err.need_index"))
        return 2
    outdir = os.path.abspath(args.output)
    os.makedirs(outdir, exist_ok=True)

    try:
        records, metas = load_indexes(args.indexes)
    except (OSError, ValueError) as exc:
        eprint(t("err.load_index", e=exc))
        return 2

    if not records:
        eprint(t("warn.empty_index"))

    hashed = any((r.hash or "") for r in records)
    backend = sim_backend_name(args.sim_algo)

    # A（有哈希则按哈希，无哈希则按「同名同大小」推断；混合时逐条回退）
    def exact_key(r: Record):
        if r.hash:
            return ("h", r.hash, r.size)
        return ("n", r.norm, r.size)

    exact_groups = group_by(records, key=exact_key)
    same_name_groups = group_by(records, key=lambda r: r.norm)

    # A2：同名但未构成「同一 exact 组」的（大小不同，或哈希不同）
    same_name_only: List[List[Record]] = []
    for g in same_name_groups:
        sizes = {r.size for r in g}
        hashes = {r.hash for r in g if r.hash}
        if len(sizes) > 1 or len(hashes) > 1:
            same_name_only.append(g)

    # 生成候选对
    pairs_raw, skipped = build_candidate_pairs(
        records, max_bucket=args.max_bucket, max_pairs=args.max_pairs,
        quiet=args.quiet)

    # 非视频文件的大小差过滤（视频文件忽略大小差）
    video_exts = build_video_ext_set(getattr(args, "video_ext", None))
    size_gate = None if getattr(args, "no_size_gate", False) else args.max_size_diff
    gated = 0

    # 打分
    scored: List[Tuple[float, int, int]] = []
    total = len(pairs_raw)
    for k, (a, b) in enumerate(pairs_raw):
        ra, rb = records[a], records[b]
        # A/A2 已单独覆盖的同名组合，不再进 B
        if ra.norm == rb.norm:
            continue
        base = similarity(ra.stem, rb.stem, backend)
        if base < args.threshold:
            continue
        # 仅当“双方都是视频”时才忽略大小差；否则大小差 > 阈值的不算可疑
        if size_gate is not None and not (is_video(ra.ext, video_exts)
                                          and is_video(rb.ext, video_exts)):
            if abs(ra.size - rb.size) > size_gate:
                gated += 1
                continue
        score = adjust_score(base, ra.se, rb.se)
        if score >= args.threshold:
            scored.append((score, a, b))
        if not args.quiet and total and k % 20000 == 0 and k:
            eprint(t("cmp.progress", k=k, total=total))

    scored.sort(key=lambda x: (-x[0], records[x[1]].display, records[x[2]].display))

    # 连通聚类（可选）
    clusters: Optional[List[List[Record]]] = None
    if args.cluster == "group":
        dsu = DSU(len(records))
        for _score, a, b in scored:
            dsu.union(a, b)
        buckets: Dict[int, List[Record]] = {}
        for _score, a, b in scored:
            for idx in (a, b):
                buckets.setdefault(dsu.find(idx), []).append(records[idx])
        clusters = []
        seen = set()
        for root, members in buckets.items():
            uniq = []
            for r in members:
                if r.idx in seen:
                    continue
                seen.add(r.idx)
                uniq.append(r)
            uniq.sort(key=lambda r: r.display)
            clusters.append(uniq)
        clusters.sort(key=lambda g: (-len(g), g[0].display))

    # 输出
    report_path = os.path.join(outdir, "report.md")
    exact_csv = os.path.join(outdir, "duplicates-exact.csv")
    similar_csv = os.path.join(outdir, "similar-candidates.csv")
    if size_gate is None:
        size_rule = t("size_rule.off")
    else:
        size_rule = t("size_rule.on", size=human_size(size_gate))

    render_markdown(
        report_path, metas=metas, records=records,
        exact_groups=exact_groups, same_name_only=same_name_only,
        pairs=scored, clusters=clusters,
        threshold=args.threshold, backend=backend, hashed=hashed,
        report_limit=args.report_limit,
        n_pairs_generated=len(pairs_raw), skipped_buckets=skipped,
        size_rule=size_rule)
    write_exact_csv(exact_csv, exact_groups, same_name_only)
    write_similar_csv(similar_csv, records=records, pairs=scored, clusters=clusters)
    groups_json = os.path.join(outdir, "groups.json")
    payload = build_groups_payload(
        exact_groups=exact_groups, same_name_only=same_name_only, records=records,
        pairs=scored, clusters=clusters, threshold=args.threshold,
        backend=backend, hashed=hashed)
    payload["size_gate"] = (None if size_gate is None else size_gate)
    payload["size_rule"] = size_rule
    payload["gated_pairs"] = gated
    payload["indexes"] = [os.path.abspath(p) for p in args.indexes]
    write_groups_json(groups_json, payload)

    if not args.quiet:
        n_exact_members = sum(len(g) for g in exact_groups)
        n_b = len(clusters) if clusters is not None else len(scored)
        unit = t("cmp.unit_groups") if clusters is not None else t("cmp.unit_pairs")
        eprint(t("cmp.done", a=len(exact_groups), am=n_exact_members,
                 a2=len(same_name_only), b=n_b, unit=unit, out=outdir + "/"))
        if size_gate is not None:
            eprint(t("cmp.gated", size=human_size(size_gate), n=gated))
        eprint(t("cmp.reports_hint"))
    if args.print_path:
        print(outdir)
    return 0


def derive_label_from_filename(path: str) -> str:
    """从 CSV 文件名推断盘名：取文件名最前面（第一个点之前）的部分。

    例：DISK_A.index.csv → DISK_A；DISK_B.csv → DISK_B。
    """
    base = os.path.basename(path)
    base = re.sub(r"\.csv$", "", base, flags=re.I)
    base = re.sub(r"\.index$", "", base, flags=re.I)
    label = base.split(".")[0].strip()
    return label or base or "UNKNOWN"


def cmd_relabel(args: argparse.Namespace) -> int:
    """把索引 CSV 的 source_label 改为与文件名最前面的盘名一致。

    只处理 dupeclean 自己生成的索引 CSV；不会触碰任何原始文件。
    只处理显式给出的文件，不扫描目录（尊重“不读文件列表”）。
    """
    changed = 0
    for path in args.csvs:
        if not os.path.isfile(path):
            eprint(t("err.no_csv", p=path))
            continue
        label = args.label or derive_label_from_filename(path)
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                lines = fh.readlines()
        except OSError as exc:
            eprint(t("warn.read_fail", p=path, e=exc))
            continue
        header_idx = None
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("#"):
                header_idx = i
                break
        if header_idx is None:
            eprint(t("relabel.no_header", p=path))
            continue
        comments = lines[:header_idx]
        body = lines[header_idx:]
        for i, line in enumerate(comments):
            m = re.match(r"^(\s*#\s*source_label\s*:\s*).*$", line.rstrip("\n"))
            if m:
                comments[i] = m.group(1) + label + "\n"
        reader = csv.DictReader(io.StringIO("".join(body)))
        if not reader.fieldnames or "source_label" not in reader.fieldnames:
            eprint(t("relabel.no_col", p=path))
            continue
        fields = list(reader.fieldnames)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(fields)
        rows = 0
        for row in reader:
            row["source_label"] = label
            writer.writerow([row.get(c, "") for c in fields])
            rows += 1
        new_text = "".join(comments) + buf.getvalue()
        if args.dry_run:
            eprint(t("relabel.dry", p=path, label=label, rows=rows))
            continue
        if args.backup:
            try:
                shutil.copy2(path, path + ".bak")
            except OSError as exc:
                eprint(t("relabel.backup_fail", p=path, e=exc))
        with _AtomicWriter(path) as fh:
            fh.write(new_text)
        changed += 1
        if not args.quiet:
            eprint(t("relabel.applied", p=path, label=label, rows=rows))
    if not args.quiet:
        eprint(t("relabel.done", n=len(args.csvs), c=changed))
    return 0


# ───────────────────────────── Web UI（只读本地图形界面） ─────────────────────────────

_ORIG_STDERR = sys.stderr


class WebJob:
    """一个扫描/对比后台任务。"""

    def __init__(self, jid: str, kind: str, result: dict) -> None:
        self.id = jid
        self.kind = kind
        self.status = "running"          # running / done / error
        self.result = result or {}
        self.error = ""
        self.started = iso_now()
        self.finished = ""
        self._lines: List[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self._lines.append(text)

    def log_text(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "log": self.log_text(), "result": self.result, "error": self.error,
            "started": self.started, "finished": self.finished,
        }


_WEB_JOBS: Dict[str, WebJob] = {}
_WEB_LOCK = threading.Lock()
_ALLOWED_DIRS: Set[str] = set()
_WEB_ACTIVE: Optional[str] = None
# 统一的结果目录：所有扫描索引与对比报告都放这里。
# 默认与脚本同级：<脚本目录>/results（不随 cwd 变化，可用 --workdir / 环境变量覆盖）
def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_workdir(explicit: Optional[str] = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("DUPCLEAN_WORKDIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(_script_dir(), "results")


_WORKDIR = _resolve_workdir()


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def _sanitize_name(name: object) -> str:
    name = re.sub(r"[^\w.\-]+", "_", str(name), flags=re.UNICODE).strip("._-")
    return name or "unnamed"


def _indexes_dir() -> str:
    return os.path.join(_WORKDIR, "indexes")


def _reports_dir() -> str:
    return os.path.join(_WORKDIR, "reports")


def _default_index_path(payload: dict) -> str:
    name = payload.get("name") or payload.get("label")
    if not name:
        paths = _payload_list(payload.get("paths"))
        name = os.path.basename(paths[0].rstrip("/")) if paths else "index"
    name = _sanitize_name(name)
    d = _indexes_dir()
    os.makedirs(d, exist_ok=True)
    candidate = os.path.join(d, name + ".index.csv")
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(d, f"{name}-{i}.index.csv")
        i += 1
    return candidate


def _default_report_dir(payload: dict) -> str:
    name = payload.get("name") or _dt.datetime.now().strftime("report-%Y%m%d-%H%M%S")
    name = _sanitize_name(name)
    d = _reports_dir()
    os.makedirs(d, exist_ok=True)
    candidate = os.path.join(d, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(d, f"{name}-{i}")
        i += 1
    return candidate


def _list_indexes() -> List[dict]:
    d = _indexes_dir()
    out: List[dict] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.lower().endswith(".csv"):
            continue
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        label = ""
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(15):
                    line = fh.readline()
                    if not line:
                        break
                    m = re.match(r"#\s*source_label\s*:\s*(.*)$", line.strip())
                    if m:
                        label = m.group(1)
                        break
        except OSError:
            pass
        try:
            size = os.path.getsize(p)
            mtime = os.path.getmtime(p)
        except OSError:
            size, mtime = 0, 0.0
        out.append({"path": p, "name": name, "label": label, "size": size,
                    "size_human": human_size(size),
                    "mtime_iso": iso_from_epoch(mtime)})
    return out


def _list_reports() -> List[dict]:
    d = _reports_dir()
    out: List[dict] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d), reverse=True):
        sub = os.path.join(d, name)
        gj = os.path.join(sub, "groups.json")
        if not os.path.isfile(gj):
            continue
        try:
            mtime = os.path.getmtime(gj)
        except OSError:
            mtime = 0.0
        out.append({"path": sub, "name": name, "mtime_iso": iso_from_epoch(mtime)})
    return out


def _payload_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [x.strip() for x in re.split(r"[,\n]", str(value)) if x.strip()]


def _scan_args_from_payload(p: dict) -> argparse.Namespace:
    paths = _payload_list(p.get("paths"))
    if not paths:
        raise ValueError(t("api.need_path"))
    output = str(p.get("output") or "").strip() or _default_index_path(p)
    try:
        min_size = parse_size(str(p.get("min_size") or "").strip() or str(DEFAULT_MIN_SIZE))
    except Exception:
        raise ValueError(t("api.bad_minsize"))
    return argparse.Namespace(
        paths=paths, output=output,
        label=str(p.get("label") or "").strip() or None,
        min_size=min_size,
        ignore=_payload_list(p.get("ignore")),
        ignore_ext=_payload_list(p.get("ignore_ext")),
        ext=_payload_list(p.get("ext")),
        include_hidden=bool(p.get("include_hidden")),
        follow_symlinks=bool(p.get("follow_symlinks")),
        hash=bool(p.get("hash")),
        algo=str(p.get("algo") or "").strip() or DEFAULT_ALGO,
        chunk_size=DEFAULT_CHUNK, quiet=False, print_path=False,
    )


def _compare_args_from_payload(p: dict) -> argparse.Namespace:
    indexes = _payload_list(p.get("indexes"))
    if not indexes:
        raise ValueError(t("api.need_indexes"))
    output = str(p.get("output") or "").strip() or _default_report_dir(p)
    try:
        threshold = float(p.get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        raise ValueError(t("api.bad_threshold"))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(t("api.threshold_range"))
    cluster = str(p.get("cluster") or "pair")
    if cluster not in ("pair", "group"):
        cluster = "pair"
    sim_algo = str(p.get("sim_algo") or "auto")
    if sim_algo not in ("auto", "rapidfuzz", "difflib"):
        sim_algo = "auto"
    try:
        max_size_diff = parse_size(str(p.get("max_size_diff") or "").strip()
                                   or str(DEFAULT_SIZE_DIFF))
    except Exception:
        raise ValueError(t("api.bad_sizediff"))
    return argparse.Namespace(
        indexes=indexes, output=output, threshold=threshold, cluster=cluster,
        sim_algo=sim_algo, max_bucket=DEFAULT_MAX_BUCKET,
        max_pairs=DEFAULT_MAX_PAIRS, report_limit=DEFAULT_REPORT_LIMIT,
        max_size_diff=max_size_diff,
        no_size_gate=bool(p.get("no_size_gate")),
        video_ext=_payload_list(p.get("video_ext")),
        quiet=False, print_path=False,
    )


def _run_web_job(job: WebJob, func, args: argparse.Namespace) -> None:
    global _WEB_ACTIVE
    _LOG_LOCAL.sink = job
    try:
        code = func(args)
        job.status = "done" if code == 0 else "error"
        if code != 0 and not job.error:
            job.error = f"任务退出码 {code}"
    except Exception as exc:  # noqa: BLE001 - 把异常反馈给界面
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        import traceback
        job.write(traceback.format_exc())
    finally:
        _LOG_LOCAL.sink = None
        job.finished = iso_now()
        with _WEB_LOCK:
            _WEB_ACTIVE = None


def _start_web_job(kind: str, payload: dict) -> WebJob:
    global _WEB_ACTIVE
    if kind == "scan":
        args = _scan_args_from_payload(payload)
        func = cmd_scan
        out = os.path.abspath(args.output)
        result = {"index": out}
        allow = os.path.dirname(out) or "."
    elif kind == "compare":
        args = _compare_args_from_payload(payload)
        func = cmd_compare
        outdir = os.path.abspath(args.output)
        result = {
            "output_dir": outdir,
            "report.md": os.path.join(outdir, "report.md"),
            "duplicates-exact.csv": os.path.join(outdir, "duplicates-exact.csv"),
            "similar-candidates.csv": os.path.join(outdir, "similar-candidates.csv"),
            "groups.json": os.path.join(outdir, "groups.json"),
        }
        allow = outdir
    else:
        raise ValueError(f"未知任务类型：{kind}")

    with _WEB_LOCK:
        if _WEB_ACTIVE is not None:
            raise RuntimeError(t("api.busy"))
        job = WebJob(_new_job_id(), kind, result)
        _WEB_JOBS[job.id] = job
        _WEB_ACTIVE = job.id
        _ALLOWED_DIRS.add(os.path.realpath(allow))

    threading.Thread(target=_run_web_job, args=(job, func, args), daemon=True).start()
    return job


def _allowed_real_path(path: str) -> Optional[str]:
    real = os.path.realpath(path)
    roots = set(_ALLOWED_DIRS)
    roots.add(os.path.realpath(_WORKDIR))
    for d in roots:
        if real == d or real.startswith(d + os.sep):
            return real
    return None


_PSEUDO_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup",
    "cgroup2", "pstore", "securityfs", "debugfs", "tracefs", "configfs",
    "fusectl", "mqueue", "hugetlbfs", "bpf", "autofs", "binfmt_misc",
    "rpc_pipefs", "nsfs", "devpts", "ramfs", "efivarfs", "fuse.gvfsd-fuse",
    "fuse.portal", "fuse.gvfs", "fuse.rclone", "nfs", "nfs4", "cifs",
    "smb3", "9p", "vboxsf", "vmhgfs",
}
# 不当作“真实磁盘”的设备（回环 / 内存盘 / 光驱 / 软驱）
_EXCLUDED_DEV_RE = re.compile(r"^/dev/(loop|zram|ram|fd|sr|nbd)\d*")
# 系统/工作区挂载点前缀（默认隐藏）
_SYSTEM_MOUNT_PREFIXES = (
    "/boot", "/snap", "/var/snap", "/var/lib/snapd", "/proc", "/sys",
    "/dev", "/run", "/tmp", "/var/tmp", "/var/lib/docker",
    "/var/lib/containers", "/var/lib/libvirt", "/var/lib/machines",
)


def _is_system_mount(mountpoint: str) -> bool:
    if mountpoint == "/":
        return True
    if mountpoint == "/run/media" or mountpoint.startswith("/run/media/"):
        return False  # 许多发行版把可移动磁盘挂在这里
    for prefix in _SYSTEM_MOUNT_PREFIXES:
        if mountpoint == prefix or mountpoint.startswith(prefix + "/"):
            return True
    return False


def _list_mounts(include_system: bool = False) -> List[dict]:
    """列出真实磁盘挂载点（默认隐藏系统/工作区挂载）。"""
    out: List[dict] = []
    seen_dev: Set[str] = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mount, fstype = parts[0], parts[1], parts[2]
                mount = re.sub(r"\\([0-7]{3})",
                               lambda m: chr(int(m.group(1), 8)), mount)
                if fstype.lower() in _PSEUDO_FSTYPES:
                    continue
                if _EXCLUDED_DEV_RE.match(dev):
                    continue
                if not dev.startswith("/dev/"):
                    continue
                if not include_system and _is_system_mount(mount):
                    continue
                if dev in seen_dev:
                    continue  # 同一设备只显示一次
                seen_dev.add(dev)
                out.append({"device": dev, "mount": mount, "fstype": fstype})
    except OSError:
        pass
    out.sort(key=lambda m: m["mount"])
    return out


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dupeclean</title>
<script>window.DC_LANG="__DC_LANG__";</script>
<style>
:root{--bg:#262421;--card:#302C28;--bg2:#1F1D1A;--fg:#D9D2C6;--muted:#A79E8E;--accent:#D8A657;--accent-fg:#241E12;--ok:#86C664;--err:#E08A7A;--bd:#433D36;--bd2:#3A342E}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
header{padding:18px 24px 8px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:20px}.badge{font-size:12px;background:var(--bd);color:var(--fg);padding:2px 8px;border-radius:999px;vertical-align:middle}
.muted{color:var(--muted);font-size:13px;margin:4px 0 0}
.hdr-right{display:flex;gap:16px;flex-wrap:wrap}
.picker{font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center}
.picker select{width:auto;padding:5px 8px}
.tabs{display:flex;gap:8px;padding:12px 24px 0}
.tab{background:transparent;color:var(--muted);border:1px solid var(--bd);border-bottom:none;border-radius:8px 8px 0 0;padding:8px 16px;cursor:pointer;font-size:14px}
.tab.active{background:var(--card);color:var(--fg)}
.panel{display:none;background:var(--card);margin:0 24px;padding:18px;border-radius:0 12px 12px 12px;border:1px solid var(--bd)}
.panel.active{display:block}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
textarea,input,select{width:100%;background:var(--bg2);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px;font:inherit}
textarea{resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 16px}
.checks{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.checks label{margin:0;color:var(--fg)}
button.primary{margin-top:16px;background:var(--accent);color:var(--accent-fg);border:none;border-radius:8px;padding:10px 20px;font-weight:600;cursor:pointer}
button.primary:hover{filter:brightness(1.06)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chip{background:var(--bg2);border:1px solid var(--bd);color:var(--fg);border-radius:999px;padding:4px 12px;cursor:pointer;font-size:12px}
.chip:hover{border-color:var(--accent)}
.chip.sel-on{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);font-weight:600}
.row-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}
.inline-check{display:inline-flex;gap:6px;align-items:center;margin:0;color:var(--fg);font-size:13px}
.job{margin:20px 24px 40px}h2{font-size:16px;display:flex;align-items:center;gap:10px}
.status{font-size:13px;color:var(--muted)}
pre#job-log{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:12px;max-height:300px;overflow:auto;font-size:12.5px;white-space:pre-wrap}
#job-result a,.card a{color:var(--accent)}
.err{color:var(--err)}code{background:var(--bg2);padding:1px 6px;border-radius:6px}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin-bottom:18px}
.card h3{margin:0 0 10px;font-size:14px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.kind{font-size:12px;color:var(--muted);font-weight:400}
table.members{width:100%;border-collapse:collapse;font-size:13px}
table.members th,table.members td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd2);word-break:break-all;vertical-align:top}
table.members tr:last-child td{border-bottom:none}
table.members th:first-child,table.members td:first-child{width:60px;text-align:center}
.tag{font-size:11px;padding:2px 9px;border-radius:999px}
.tag.exact{background:#14532d;color:#a7f3c0}.tag.same-name-different{background:#78350f;color:#fcd34d}.tag.similar{background:#1e3a8a;color:#a9c7ff}
.delbox{width:18px;height:18px}
tr.on-disk{background:rgba(216,166,87,.10)}
#rev-groups{margin-top:6px}
</style>
</head>
<body>
<header>
  <div>
    <h1>Dupeclean <span class="badge" data-i18n="hdr.badge">只读 · 不会删除任何文件</span></h1>
    <p class="muted" data-i18n="hdr.subtitle">扫描每块盘生成索引 → 同时对比多个索引 → 复核并生成删除名单。</p>
  </div>
  <div class="hdr-right">
    <div class="picker"><span data-i18n="hdr.theme">护眼主题</span>
      <select id="theme" onchange="onThemeChange()"></select>
    </div>
    <div class="picker"><span data-i18n="hdr.lang">语言</span>
      <select id="lang" onchange="onLangChange()">
        <option value="zh">中文</option>
        <option value="en">English</option>
      </select>
    </div>
  </div>
</header>
<nav class="tabs">
  <button class="tab active" data-tab="scan" data-i18n="tab.scan">① 扫描 Scan</button>
  <button class="tab" data-tab="compare" data-i18n="tab.compare">② 对比 Compare</button>
  <button class="tab" data-tab="review" data-i18n="tab.review">③ 复核 · 删除名单</button>
</nav>

<section id="scan" class="panel active">
  <label data-i18n="scan.paths">要扫描的目录（每行一个，也可点击下方磁盘）</label>
  <textarea id="scan-paths" rows="3" placeholder="/run/media/user/DISK_A"></textarea>
  <div id="mounts" class="chips"></div>
  <label class="inline-check" style="margin-top:8px"><input type="checkbox" id="mounts-all" onchange="loadMounts()"> <span data-i18n="scan.show_sys">显示系统挂载点（/、/boot、/snap 等）</span></label>
  <div class="grid">
    <div><label data-i18n="scan.name">索引名称（自动生成，可改）</label><input id="scan-name" value=""></div>
    <div><label data-i18n="scan.label">来源盘标签（随所选磁盘自动变化）</label><input id="scan-label" placeholder="auto"></div>
    <div><label data-i18n="scan.minsize">最小文件大小</label><input id="scan-minsize" value="50M"></div>
    <div><label data-i18n="scan.algo">哈希算法</label><input id="scan-algo" value="sha256"></div>
  </div>
  <div class="checks">
    <label><input type="checkbox" id="scan-hidden"> <span data-i18n="scan.hidden">包含隐藏项</span></label>
    <label><input type="checkbox" id="scan-symlink"> <span data-i18n="scan.symlink">跟随软链</span></label>
    <label><input type="checkbox" id="scan-hash"> <span data-i18n="scan.hash">计算哈希（慢，默认关闭）</span></label>
  </div>
  <div class="grid">
    <div><label data-i18n="scan.ignore">额外忽略目录（逗号分隔）</label><input id="scan-ignore"></div>
    <div><label data-i18n="scan.ignoreext">额外忽略扩展名</label><input id="scan-ignoreext"></div>
    <div><label data-i18n="scan.ext">扩展名白名单（可空）</label><input id="scan-ext"></div>
  </div>
  <p class="muted"><span data-i18n="scan.workdir">结果目录：</span><code id="wd">…</code> <span data-i18n="scan.workdir2">（索引存 indexes/，报告存 reports/）</span></p>
  <button class="primary" onclick="runScan()" data-i18n="scan.start">开始扫描</button>
</section>

<section id="compare" class="panel">
  <label data-i18n="cmp.avail">可用索引 CSV（点击选择 / 取消）</label>
  <div id="indexes" class="chips"></div>
  <div class="row-actions">
    <button class="chip" onclick="loadIndexes()" data-i18n="cmp.refresh">刷新</button>
    <button class="chip" onclick="selectAllIndexes(true)" data-i18n="cmp.select_all">全选</button>
    <button class="chip" onclick="selectAllIndexes(false)" data-i18n="cmp.clear">清空</button>
    <span class="muted" id="idx-summary"></span>
  </div>
  <div class="grid">
    <div><label data-i18n="cmp.name">报告名称（留空=按时间自动命名）</label><input id="cmp-name" placeholder="report-YYYYmmdd-HHMMSS"></div>
    <div><label data-i18n="cmp.threshold">相似度阈值</label><input id="cmp-threshold" value="0.75"></div>
    <div><label data-i18n="cmp.cluster">B 输出形态</label><select id="cmp-cluster"><option value="pair" data-i18n="cmp.pair">pair（成对）</option><option value="group" data-i18n="cmp.group">group（连通分组）</option></select></div>
    <div><label data-i18n="cmp.simalgo">相似度算法</label><select id="cmp-simalgo"><option value="auto">auto</option><option value="rapidfuzz">rapidfuzz</option><option value="difflib">difflib</option></select></div>
    <div><label data-i18n="cmp.sizediff">非视频大小差上限</label><input id="cmp-sizediff" value="50M"></div>
  </div>
  <button class="primary" onclick="runCompare()" data-i18n="cmp.start">开始对比</button>
</section>

<section id="review" class="panel">
  <label data-i18n="rev.report">报告（对比结果）</label>
  <div class="row-actions">
    <select id="rev-report" style="max-width:420px" onchange="loadGroups()"></select>
    <button class="chip" onclick="loadReports()" data-i18n="rev.refresh">刷新</button>
    <span class="muted" id="rev-summary"></span>
  </div>
  <div class="row-actions">
    <label style="margin:0"><span data-i18n="rev.disk">硬盘</span>
      <select id="rev-disk" style="width:auto" onchange="renderGroups()"></select>
    </label>
    <label class="inline-check"><input type="checkbox" id="rev-disk-first" checked onchange="renderGroups()"> <span data-i18n="rev.disk_first">所选盘排最前</span></label>
    <button class="chip" onclick="checkAllDisk()" data-i18n="rev.check_disk_all">勾选全部所选盘副本</button>
  </div>
  <div class="row-actions">
    <label style="margin:0"><span data-i18n="rev.show">显示</span>
      <select id="rev-kind" style="width:auto" onchange="renderGroups()">
        <option value="all" data-i18n="rev.show_all">全部</option>
        <option value="exact" data-i18n="rev.show_exact">精确相似</option>
        <option value="suspect" data-i18n="rev.show_suspect">可疑相似</option>
      </select>
    </label>
    <label style="margin:0"><span data-i18n="rev.group_sort">组排序</span>
      <select id="rev-group-sort" style="width:auto" onchange="renderGroups()">
        <option value="size" data-i18n="rev.gs_size">按组内最大文件大小 ↓</option>
        <option value="count" data-i18n="rev.gs_count">按文件数 ↓</option>
        <option value="sim" data-i18n="rev.gs_sim">按相似度 ↓</option>
        <option value="kind" data-i18n="rev.gs_kind">按类型</option>
      </select>
    </label>
    <label style="margin:0"><span data-i18n="rev.member_sort">组内排序</span>
      <select id="rev-member-sort" style="width:auto" onchange="renderGroups()">
        <option value="size-desc" data-i18n="rev.ms_size_desc">大小 ↓（优先删大的）</option>
        <option value="size-asc" data-i18n="rev.ms_size_asc">大小 ↑</option>
        <option value="time-desc" data-i18n="rev.ms_time_desc">时间 新→旧</option>
        <option value="time-asc" data-i18n="rev.ms_time_asc">时间 旧→新</option>
        <option value="path" data-i18n="rev.ms_path">路径</option>
      </select>
    </label>
  </div>
  <div class="row-actions">
    <label class="inline-check"><input type="checkbox" id="rev-folders-only" onchange="renderReview()"> <span data-i18n="rev.folders_only">只显示重复文件夹</span></label>
    <label class="inline-check"><input type="checkbox" id="rev-hide-folder-files" checked onchange="renderGroups()"> <span data-i18n="rev.hide_folder_files">隐藏已选文件夹内的文件</span></label>
    <button class="chip" onclick="loadFolders()" data-i18n="rev.refresh_folders">刷新重复文件夹</button>
    <span class="muted" id="rev-folder-summary"></span>
  </div>
  <div id="rev-folders"></div>
  <div id="rev-groups"></div>
  <div class="row-actions">
    <button class="primary" onclick="generateDeleteList()" data-i18n="rev.gen">生成删除名单 CSV</button>
    <span class="muted" data-i18n="rev.note">勾选要删除的文件/文件夹（同一组建议保留一份）。本工具只生成名单，不会删除。</span>
  </div>
  <div id="rev-result"></div>
</section>

<section class="job">
  <h2><span data-i18n="job.title">运行日志</span> <span id="job-status" class="status"></span></h2>
  <pre id="job-log" data-i18n="job.waiting">等待任务…</pre>
  <div id="job-result"></div>
</section>

<script>
/* ---------------- i18n ---------------- */
const I18N = {
  zh: {
    "hdr.badge":"只读 · 不会删除任何文件","hdr.subtitle":"扫描每块盘生成索引 → 同时对比多个索引 → 复核并生成删除名单。","hdr.theme":"护眼主题","hdr.lang":"语言",
    "tab.scan":"① 扫描 Scan","tab.compare":"② 对比 Compare","tab.review":"③ 复核 · 删除名单",
    "scan.paths":"要扫描的目录（每行一个，也可点击下方磁盘）","scan.show_sys":"显示系统挂载点（/、/boot、/snap 等）","scan.name":"索引名称（自动生成，可改）","scan.label":"来源盘标签（随所选磁盘自动变化）","scan.minsize":"最小文件大小","scan.algo":"哈希算法","scan.hidden":"包含隐藏项","scan.symlink":"跟随软链","scan.hash":"计算哈希（慢，默认关闭）","scan.ignore":"额外忽略目录（逗号分隔）","scan.ignoreext":"额外忽略扩展名","scan.ext":"扩展名白名单（可空）","scan.workdir":"结果目录：","scan.workdir2":"（索引存 indexes/，报告存 reports/）","scan.start":"开始扫描",
    "cmp.avail":"可用索引 CSV（点击选择 / 取消）","cmp.refresh":"刷新","cmp.select_all":"全选","cmp.clear":"清空","cmp.name":"报告名称（留空=按时间自动命名）","cmp.threshold":"相似度阈值","cmp.cluster":"B 输出形态","cmp.pair":"pair（成对）","cmp.group":"group（连通分组）","cmp.simalgo":"相似度算法","cmp.sizediff":"非视频大小差上限","cmp.start":"开始对比",
    "rev.report":"报告（对比结果）","rev.refresh":"刷新","rev.disk":"硬盘","rev.disk_first":"所选盘排最前","rev.check_disk_all":"勾选全部所选盘副本","rev.show":"显示","rev.show_all":"全部","rev.show_exact":"精确相似","rev.show_suspect":"可疑相似","rev.group_sort":"组排序","rev.gs_size":"按组内最大文件大小 ↓","rev.gs_count":"按文件数 ↓","rev.gs_sim":"按相似度 ↓","rev.gs_kind":"按类型","rev.member_sort":"组内排序","rev.ms_size_desc":"大小 ↓（优先删大的）","rev.ms_size_asc":"大小 ↑","rev.ms_time_desc":"时间 新→旧","rev.ms_time_asc":"时间 旧→新","rev.ms_path":"路径","rev.folders_only":"只显示重复文件夹","rev.hide_folder_files":"隐藏已选文件夹内的文件","rev.refresh_folders":"刷新重复文件夹","rev.gen":"生成删除名单 CSV","rev.note":"勾选要删除的文件/文件夹（同一组建议保留一份）。本工具只生成名单，不会删除。",
    "job.title":"运行日志","job.waiting":"等待任务…","job.running":"⏳ 运行中","job.done":"✅ 完成","job.error":"❌ 出错",
    "kind.exact":"疑似精确重复","kind.same":"同名不同内容","kind.similar":"相似候选",
    "btn.keep_largest":"只保留最大（勾选其余）","btn.keep_first":"只保留第一个（勾选其余）","btn.clear_group":"清空本组","btn.check_disk":"勾选本组【{d}】的副本",
    "msg.no_mounts":"未检测到真实磁盘，请手动填写路径。","msg.no_indexes":"还没有索引，请先在「扫描」页扫描一块盘。","msg.no_reports":"还没有报告，请先在「对比」页运行一次对比。","msg.pick_report":"请选择一个报告。","msg.none_filter":"当前筛选下没有条目。","msg.none":"没有发现重复或相似候选。","msg.no_folders":"未发现重复文件夹",
    "msg.selected":"已选 {n} 个","msg.summary":"精确相似 {e} 组 · 可疑相似 {s} 组 · 当前硬盘 {d} 组 · {h} · 阈值 {t}","msg.hash_on":"哈希模式","msg.hash_off":"未哈希",
    "msg.folders_n":  "重复文件夹 {n} 组","msg.folders_title":"重复文件夹","msg.folders_note":"（基于原始扫描记录的内容比对得出）","msg.exact_hash":"哈希完全一致","msg.exact_infer":"按“文件名+大小”推断",
    "msg.folder_group":"重复文件夹 组 #{g} · {c} 个副本 · 每个 {n} 个文件","msg.folder_total":"共 {size} · {exact}",
    "msg.copies":"{n} 个文件","msg.gen_ok":"已生成 <b>{total}</b> 条删除名单，按硬盘分为 {files} 个 CSV{pf}：","msg.gen_pf":"（含 {n} 个文件夹：先删文件，再删空目录）","msg.gen_hint":"真正删除请用 scripts/delete-files.sh（Linux）或 scripts/delete-files.ps1（Windows）。默认演练，加确认参数才删除。",
    "msg.scan_done":"索引已生成：","msg.report_dir":"报告目录：","msg.download":"下载","msg.select_disk_first":"请先在「硬盘」里选择一块盘","msg.pick_report2":"请先选择报告","msg.pick_one":"请至少勾选一个文件或一个重复文件夹","msg.start_fail":"无法启动任务：","msg.gen_fail":"生成失败：","msg.pick_index":"请先点选至少一个索引 CSV","msg.idx_summary":"已选 {n} 个","msg.hdr_disk":"来源盘","msg.hdr_path":"路径","msg.hdr_size":"大小","msg.hdr_mtime":"修改时间","msg.hdr_count":"文件数","msg.hdr_folder":"文件夹"
  },
  en: {
    "hdr.badge":"read-only · deletes nothing","hdr.subtitle":"Scan each disk into an index → compare several indexes → review and produce a delete list.","hdr.theme":"Theme","hdr.lang":"Language",
    "tab.scan":"① Scan","tab.compare":"② Compare","tab.review":"③ Review · delete list",
    "scan.paths":"Directories to scan (one per line; or click a disk below)","scan.show_sys":"Show system mounts (/ , /boot, /snap …)","scan.name":"Index name (auto, editable)","scan.label":"Source disk label (updates with the chosen disk)","scan.minsize":"Minimum file size","scan.algo":"Hash algorithm","scan.hidden":"Include hidden","scan.symlink":"Follow symlinks","scan.hash":"Compute hashes (slow, default off)","scan.ignore":"Extra ignored dirs (comma-separated)","scan.ignoreext":"Extra ignored extensions","scan.ext":"Extension whitelist (optional)","scan.workdir":"Results dir:","scan.workdir2":"(indexes/ and reports/)","scan.start":"Start scan",
    "cmp.avail":"Available index CSVs (click to select)","cmp.refresh":"Refresh","cmp.select_all":"Select all","cmp.clear":"Clear","cmp.name":"Report name (blank = timestamp)","cmp.threshold":"Similarity threshold","cmp.cluster":"B output","cmp.pair":"pair","cmp.group":"group (connected)","cmp.simalgo":"Similarity algorithm","cmp.sizediff":"Non-video max size diff","cmp.start":"Start compare",
    "rev.report":"Report (compare result)","rev.refresh":"Refresh","rev.disk":"Disk","rev.disk_first":"Chosen disk first","rev.check_disk_all":"Check all copies on chosen disk","rev.show":"Show","rev.show_all":"All","rev.show_exact":"Exact-like","rev.show_suspect":"Suspicious","rev.group_sort":"Group sort","rev.gs_size":"by max file size ↓","rev.gs_count":"by file count ↓","rev.gs_sim":"by similarity ↓","rev.gs_kind":"by kind","rev.member_sort":"Member sort","rev.ms_size_desc":"size ↓ (big first)","rev.ms_size_asc":"size ↑","rev.ms_time_desc":"time new→old","rev.ms_time_asc":"time old→new","rev.ms_path":"path","rev.folders_only":"Only duplicate folders","rev.hide_folder_files":"Hide files inside selected folders","rev.refresh_folders":"Refresh folders","rev.gen":"Generate delete list CSV","rev.note":"Check files/folders to delete (keep one per group). This only writes a list; it never deletes.",
    "job.title":"Log","job.waiting":"waiting…","job.running":"⏳ running","job.done":"✅ done","job.error":"❌ error",
    "kind.exact":"Likely exact duplicate","kind.same":"Same name, different content","kind.similar":"Similar candidate",
    "btn.keep_largest":"Keep largest (check the rest)","btn.keep_first":"Keep first (check the rest)","btn.clear_group":"Clear group","btn.check_disk":"Check copies on [{d}]",
    "msg.no_mounts":"No real disk detected; type a path manually.","msg.no_indexes":"No index yet; scan a disk on the Scan tab first.","msg.no_reports":"No report yet; run a compare on the Compare tab first.","msg.pick_report":"Select a report.","msg.none_filter":"Nothing under the current filter.","msg.none":"No duplicates or similar candidates found.","msg.no_folders":"No duplicate folders found",
    "msg.selected":"{n} selected","msg.summary":"exact-like {e} groups · suspicious {s} groups · current disk {d} groups · {h} · threshold {t}","msg.hash_on":"hashed","msg.hash_off":"not hashed",
    "msg.folders_n":"Duplicate folders: {n} groups","msg.folders_title":"Duplicate folders","msg.folders_note":"(from original scan records, content-based)","msg.exact_hash":"identical hash","msg.exact_infer":"inferred by name+size",
    "msg.folder_group":"Duplicate folder group #{g} · {c} copies · {n} files each","msg.folder_total":"total {size} · {exact}",
    "msg.copies":"{n} files","msg.gen_ok":"Wrote <b>{total}</b> delete-list rows into {files} CSV file(s){pf}:","msg.gen_pf":" (incl. {n} folders: delete files, then empty dirs)","msg.gen_hint":"To actually delete, use scripts/delete-files.sh (Linux) or scripts/delete-files.ps1 (Windows). Dry-run by default.",
    "msg.scan_done":"Index written:","msg.report_dir":"Report dir:","msg.download":"download","msg.select_disk_first":"Pick a disk in the Disk selector first","msg.pick_report2":"Select a report first","msg.pick_one":"Check at least one file or folder","msg.start_fail":"Cannot start job: ","msg.gen_fail":"Failed: ","msg.pick_index":"Select at least one index CSV","msg.idx_summary":"{n} selected","msg.hdr_disk":"Disk","msg.hdr_path":"Path","msg.hdr_size":"Size","msg.hdr_mtime":"Modified","msg.hdr_count":"Files","msg.hdr_folder":"Folder"
  }
};
let LANG = (window.DC_LANG || 'zh');
function T(k, vars){ let s=(I18N[LANG]&&I18N[LANG][k])||(I18N.zh[k])||k; if(vars){ Object.keys(vars).forEach(function(v){ s=s.split('{'+v+'}').join(vars[v]); }); } return s; }
function applyI18n(){ document.querySelectorAll('[data-i18n]').forEach(function(el){ el.textContent=T(el.dataset.i18n); }); document.documentElement.lang=(LANG==='zh'?'zh-CN':'en'); }
function onLangChange(){ LANG=$('lang').value; try{ localStorage.setItem('dc-lang', LANG); }catch(e){} applyI18n(); renderIndexes(); renderReview(); }

/* ---------------- 护眼主题 ---------------- */
const THEMES = {
  warm:   {name:'暖夜 · 低蓝光（推荐）', bg:'#262421', card:'#302C28', bg2:'#1F1D1A', fg:'#D9D2C6', muted:'#A79E8E', accent:'#D8A657', accentFg:'#241E12', bd:'#433D36', bd2:'#3A342E'},
  dark:   {name:'中性暗灰 · 低对比', bg:'#1E1E1E', card:'#252526', bg2:'#181818', fg:'#CFCFCF', muted:'#8C8C8C', accent:'#5AA9D6', accentFg:'#0B1A24', bd:'#3A3A3A', bd2:'#303030'},
  solariz:{name:'Solarized Dark', bg:'#002B36', card:'#073642', bg2:'#00212B', fg:'#93A1A1', muted:'#657B83', accent:'#2AA198', accentFg:'#002B36', bd:'#0F4A57', bd2:'#0B3D49'},
  gruvbox:{name:'Gruvbox Dark', bg:'#282828', card:'#3C3836', bg2:'#1D2021', fg:'#EBDBB2', muted:'#A89984', accent:'#83A598', accentFg:'#1D2021', bd:'#504945', bd2:'#45403D'},
  nord:   {name:'Nord', bg:'#2E3440', card:'#3B4252', bg2:'#292E39', fg:'#D8DEE9', muted:'#9AA5B5', accent:'#88C0D0', accentFg:'#2E3440', bd:'#4C566A', bd2:'#434C5E'},
  paper:  {name:'暖米黄 · 明亮环境', bg:'#F3EDE0', card:'#FBF7EE', bg2:'#EDE6D6', fg:'#3B352B', muted:'#7A7264', accent:'#2C7A7B', accentFg:'#FFFFFF', bd:'#D9D0BD', bd2:'#E4DCCB'},
  solight:{name:'Solarized Light', bg:'#FDF6E3', card:'#EEE8D5', bg2:'#F7F0DD', fg:'#586E75', muted:'#93A1A1', accent:'#268BD2', accentFg:'#FDF6E3', bd:'#D8D0BB', bd2:'#E3DBC7'}
};
function applyTheme(name){ const t=THEMES[name]||THEMES.warm; const r=document.documentElement.style; r.setProperty('--bg',t.bg); r.setProperty('--card',t.card); r.setProperty('--bg2',t.bg2); r.setProperty('--fg',t.fg); r.setProperty('--muted',t.muted); r.setProperty('--accent',t.accent); r.setProperty('--accent-fg',t.accentFg); r.setProperty('--bd',t.bd); r.setProperty('--bd2',t.bd2); }
function initThemes(){ const sel=$('theme'); Object.keys(THEMES).forEach(function(k){ const o=document.createElement('option'); o.value=k; o.textContent=THEMES[k].name; sel.appendChild(o); }); let saved='warm'; try{ saved=localStorage.getItem('dc-theme')||'warm'; }catch(e){} if(!THEMES[saved]) saved='warm'; sel.value=saved; applyTheme(saved); }
function onThemeChange(){ const v=$('theme').value; applyTheme(v); try{ localStorage.setItem('dc-theme', v); }catch(e){} }

/* ---------------- 通用 ---------------- */
let currentJob = null, timer = null;
let indexList = [];
let selectedIndexes = new Set();
let groupsData = null;
let dupFolders = [];
let selectedFolders = new Set();
function $(id){return document.getElementById(id);}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
function lines(id){ return $(id).value.split('\n').map(function(s){return s.trim();}).filter(Boolean); }
function switchTab(name){ document.querySelectorAll('.tab').forEach(function(x){x.classList.toggle('active', x.dataset.tab===name);}); document.querySelectorAll('.panel').forEach(function(x){x.classList.toggle('active', x.id===name);}); }
document.querySelectorAll('.tab').forEach(function(t){ t.onclick=function(){ switchTab(t.dataset.tab); }; });
async function api(path, opts){ const r = await fetch(path, opts); const j = await r.json().catch(function(){return {};}); if(!r.ok) throw new Error(j.error||('HTTP '+r.status)); return j; }

/* ---------------- 磁盘 ---------------- */
async function loadMounts(){ try{ const all=($('mounts-all')&&$('mounts-all').checked)?'?all=1':''; const j = await api('/api/mounts'+all); const box=$('mounts'); box.innerHTML=''; (j.mounts||[]).forEach(function(m){ const b=document.createElement('button'); b.className='chip'; b.textContent=m.mount+' ('+m.fstype+')'; b.onclick=function(){ const ta=$('scan-paths'); ta.value=(ta.value.trim()?ta.value.trim()+'\n':'')+m.mount; const base=m.mount.split('/').filter(Boolean).pop()||'disk'; $('scan-label').value=base; $('scan-name').value=base; }; box.appendChild(b); }); if(!(j.mounts||[]).length) box.innerHTML='<span class="muted">'+esc(T('msg.no_mounts'))+'</span>'; }catch(e){} }

/* ---------------- 索引 ---------------- */
async function loadIndexes(){ try{ const j = await api('/api/indexes'); indexList = j.indexes||[]; if(j.workdir && $('wd')) $('wd').textContent=j.workdir; renderIndexes(); }catch(e){} }
function renderIndexes(){ const box=$('indexes'); box.innerHTML=''; if(!indexList.length){ box.innerHTML='<span class="muted">'+esc(T('msg.no_indexes'))+'</span>'; } indexList.forEach(function(it){ const b=document.createElement('button'); b.className='chip'+(selectedIndexes.has(it.path)?' sel-on':''); b.textContent=it.name+(it.label?'（'+it.label+'）':''); b.title=it.path; b.onclick=function(){ if(selectedIndexes.has(it.path)) selectedIndexes.delete(it.path); else selectedIndexes.add(it.path); renderIndexes(); }; box.appendChild(b); }); $('idx-summary').textContent=T('msg.idx_summary',{n:selectedIndexes.size}); }
function selectAllIndexes(on){ if(on) indexList.forEach(function(it){selectedIndexes.add(it.path);}); else selectedIndexes.clear(); renderIndexes(); }

/* ---------------- 报告与分组 ---------------- */
async function loadReports(prefer){ try{ const j = await api('/api/reports'); const sel=$('rev-report'); const cur=prefer||sel.value; sel.innerHTML=''; (j.reports||[]).forEach(function(r){ const o=document.createElement('option'); o.value=r.path; o.textContent=r.name+'  ('+r.mtime_iso+')'; sel.appendChild(o); }); if(cur && Array.from(sel.options).some(function(o){return o.value===cur;})) sel.value=cur; if(!sel.options.length){ $('rev-groups').innerHTML='<span class="muted">'+esc(T('msg.no_reports'))+'</span>'; $('rev-summary').textContent=''; } else { loadGroups(); } }catch(e){} }
async function loadGroups(){ const dir=$('rev-report').value; const box=$('rev-groups'); if(!dir){ box.innerHTML='<span class="muted">'+esc(T('msg.pick_report'))+'</span>'; return; } try{ groupsData = await api('/api/groups?dir='+encodeURIComponent(dir)); populateDisks(); selectedFolders.clear(); await loadFolders(); renderReview(); }catch(e){ box.innerHTML='<p class="err">'+esc(e.message)+'</p>'; } }
async function loadFolders(){ const dir=$('rev-report').value; if(!dir){ dupFolders=[]; renderFolders(); return; } try{ const j=await api('/api/dupfolders?dir='+encodeURIComponent(dir)); dupFolders=j.folders||[]; }catch(e){ dupFolders=[]; } renderFolders(); }
function renderReview(){ renderFolders(); const only=($('rev-folders-only')||{}).checked; const gbox=$('rev-groups'); if(only){ if(gbox) gbox.style.display='none'; } else { if(gbox) gbox.style.display=''; renderGroups(); } }
function folderHasSelected(p){ let r=false; selectedFolders.forEach(function(f){ if(p===f||p.indexOf(f+'/')===0) r=true; }); return r; }
function renderFolders(){ const box=$('rev-folders'); if(!box) return; const n=dupFolders.length; if($('rev-folder-summary')) $('rev-folder-summary').textContent = n? T('msg.folders_n',{n:n}):T('msg.no_folders'); if(!n){ box.innerHTML=''; return; } let h='<h2 style="font-size:15px;margin:6px 0 8px">'+esc(T('msg.folders_title'))+' <span class="muted">'+esc(T('msg.folders_note'))+'</span></h2>'; dupFolders.forEach(function(g,gi){ const ex=g.exact?T('msg.exact_hash'):T('msg.exact_infer'); h+='<div class="card" data-fgid="'+gi+'"><h3><span>'+esc(T('msg.folder_group',{g:gi+1,c:g.copies.length,n:g.file_count}))+'</span><span class="kind">'+esc(T('msg.folder_total',{size:g.total_size_human,exact:ex}))+'</span></h3>'; h+='<div class="row-actions" style="margin:0 0 8px"><button class="chip" onclick="keepFirstFolder('+gi+')">'+esc(T('btn.keep_first'))+'</button><button class="chip" onclick="clearFolder('+gi+')">'+esc(T('btn.clear_group'))+'</button></div>'; h+='<table class="members"><thead><tr><th>?</th><th>'+esc(T('msg.hdr_disk'))+'</th><th>'+esc(T('msg.hdr_folder'))+'</th><th>'+esc(T('msg.hdr_count'))+'</th><th>'+esc(T('msg.hdr_size'))+'</th></tr></thead><tbody>'; g.copies.forEach(function(c){ const sel=selectedFolders.has(c.path); h+='<tr><td><input class="fbox" type="checkbox" '+(sel?'checked':'')+' data-folder="'+esc(c.path)+'" onchange="onFolderToggle(this)"></td><td>'+esc(c.source_label)+'</td><td>'+esc(c.path)+'</td><td>'+c.file_count+'</td><td>'+esc(c.total_size_human)+'</td></tr>'; }); h+='</tbody></table></div>'; }); box.innerHTML=h; }
function onFolderToggle(el){ const p=el.dataset.folder; if(el.checked) selectedFolders.add(p); else selectedFolders.delete(p); renderGroups(); }
function keepFirstFolder(gi){ const g=dupFolders[gi]; if(!g) return; const card=document.querySelector('.card[data-fgid="'+gi+'"]'); if(!card) return; selectedFolders.clear(); card.querySelectorAll('.fbox').forEach(function(b){ if(b.dataset.folder!==g.copies[0].path){ b.checked=true; selectedFolders.add(b.dataset.folder); } else { b.checked=false; } }); renderGroups(); }
function clearFolder(gi){ const card=document.querySelector('.card[data-fgid="'+gi+'"]'); if(!card) return; card.querySelectorAll('.fbox').forEach(function(b){ b.checked=false; selectedFolders.delete(b.dataset.folder); }); renderGroups(); }
function populateDisks(){ const sel=$('rev-disk'); const cur=sel.value; const labels=new Set(); (groupsData.groups||[]).forEach(function(g){ (g.members||[]).forEach(function(m){ labels.add(m.source_label); }); }); sel.innerHTML='<option value="all">'+esc(T('rev.show_all'))+'</option>'; Array.from(labels).sort().forEach(function(l){ const o=document.createElement('option'); o.value=l; o.textContent=l; sel.appendChild(o); }); if(Array.from(sel.options).some(function(o){return o.value===cur;})) sel.value=cur; }
function kindName(k){ return k==='exact'?T('kind.exact'):(k==='same-name-different'?T('kind.same'):T('kind.similar')); }
function memberSize(m){ return Number(m.size)||0; }
function memberTime(m){ const t=Date.parse(m.mtime_iso); return isNaN(t)?0:t; }
function sortMembers(arr, mode){ const a=arr.slice(); if(mode==='size-desc')a.sort(function(x,y){return memberSize(y)-memberSize(x);}); else if(mode==='size-asc')a.sort(function(x,y){return memberSize(x)-memberSize(y);}); else if(mode==='time-desc')a.sort(function(x,y){return memberTime(y)-memberTime(x);}); else if(mode==='time-asc')a.sort(function(x,y){return memberTime(x)-memberTime(y);}); else a.sort(function(x,y){return String(x.path).localeCompare(String(y.path));}); return a; }
function groupMax(g){ return Math.max.apply(null, (g.members||[]).map(memberSize).concat([0])); }
function sortGroups(arr, mode){ const a=arr.slice(); if(mode==='size')a.sort(function(x,y){return groupMax(y)-groupMax(x);}); else if(mode==='count')a.sort(function(x,y){return y.members.length-x.members.length;}); else if(mode==='sim')a.sort(function(x,y){return (Number(y.similarity)||0)-(Number(x.similarity)||0);}); else a.sort(function(x,y){return String(x.kind).localeCompare(String(y.kind))||x.id-y.id;}); return a; }
function renderGroups(){ const box=$('rev-groups'); if(!groupsData){ return; } if(($('rev-folders-only')||{}).checked){ box.innerHTML=''; return; } const all=groupsData.groups||[]; const kindf=($('rev-kind')||{}).value||'all'; const disk=($('rev-disk')||{}).value||'all'; const diskFirst=($('rev-disk-first')||{}).checked; const hideSel=($('rev-hide-folder-files')||{}).checked; const nExact=all.filter(function(g){return g.kind==='exact'||g.kind==='same-name-different';}).length; const nSuspect=all.filter(function(g){return g.kind==='similar';}).length; const nDisk=(disk==='all')?all.length:all.filter(function(g){return (g.members||[]).some(function(m){return m.source_label===disk;});}).length; $('rev-summary').textContent=T('msg.summary',{e:nExact,s:nSuspect,d:nDisk,h:(groupsData.hashed?T('msg.hash_on'):T('msg.hash_off')),t:groupsData.threshold}); const gs=all.filter(function(g){ if(kindf==='exact'&&!(g.kind==='exact'||g.kind==='same-name-different'))return false; if(kindf==='suspect'&&g.kind!=='similar')return false; if(disk!=='all'&&!(g.members||[]).some(function(m){return m.source_label===disk;}))return false; return true; }); if(!gs.length){ box.innerHTML='<span class="muted">'+esc(T('msg.none_filter'))+'</span>'; return; } const gsort=($('rev-group-sort')||{}).value||'size'; const msort=($('rev-member-sort')||{}).value||'size-desc'; const groups=sortGroups(gs, gsort); let h=''; groups.forEach(function(g){ const sim=(g.similarity!=null)?('sim '+Number(g.similarity).toFixed(3)):''; let members=sortMembers(g.members, msort); if(hideSel){ members=members.filter(function(m){ return !folderHasSelected(m.path); }); } if(!members.length){ return; } if(diskFirst && disk!=='all'){ members=members.filter(function(m){return m.source_label===disk;}).concat(members.filter(function(m){return m.source_label!==disk;})); } h+='<div class="card" data-gid="'+g.id+'"><h3><span><span class="tag '+esc(g.kind)+'">'+kindName(g.kind)+'</span> #'+g.id+' · '+g.members.length+' '+esc(T('msg.copies',{n:g.members.length}))+'</span><span class="kind">'+sim+'</span></h3>'; h+='<div class="row-actions" style="margin:0 0 8px">'; if(disk!=='all') h+='<button class="chip" onclick="checkDisk('+g.id+')">'+esc(T('btn.check_disk',{d:disk}))+'</button>'; h+='<button class="chip" onclick="keepLargest('+g.id+')">'+esc(T('btn.keep_largest'))+'</button><button class="chip" onclick="clearGroup('+g.id+')">'+esc(T('btn.clear_group'))+'</button></div>'; h+='<table class="members"><thead><tr><th>?</th><th>'+esc(T('msg.hdr_disk'))+'</th><th>'+esc(T('msg.hdr_path'))+'</th><th>'+esc(T('msg.hdr_size'))+'</th><th>'+esc(T('msg.hdr_mtime'))+'</th></tr></thead><tbody>'; members.forEach(function(m){ const on=(disk!=='all'&&m.source_label===disk); h+='<tr class="'+(on?'on-disk':'')+'"><td><input class="delbox" type="checkbox" data-size="'+memberSize(m)+'" data-label="'+esc(m.source_label)+'" value="'+esc(m.path)+'"></td><td>'+esc(m.source_label)+'</td><td>'+esc(m.path)+'</td><td>'+esc(m.size_human)+'</td><td>'+esc(m.mtime_iso)+'</td></tr>'; }); h+='</tbody></table></div>'; }); box.innerHTML=h; }
function keepLargest(gid){ const card=document.querySelector('.card[data-gid="'+gid+'"]'); if(!card) return; const boxes=Array.prototype.slice.call(card.querySelectorAll('.delbox')); let max=-1, maxBox=null; boxes.forEach(function(b){ const s=Number(b.dataset.size)||0; if(s>max){max=s;maxBox=b;} }); boxes.forEach(function(b){ b.checked=(b!==maxBox); }); }
function clearGroup(gid){ const card=document.querySelector('.card[data-gid="'+gid+'"]'); if(card) card.querySelectorAll('.delbox').forEach(function(b){ b.checked=false; }); }
function checkDisk(gid){ const disk=($('rev-disk')||{}).value; if(!disk||disk==='all'){ alert(T('msg.select_disk_first')); return; } const card=document.querySelector('.card[data-gid="'+gid+'"]'); if(!card) return; card.querySelectorAll('.delbox').forEach(function(b){ if(b.dataset.label===disk) b.checked=true; }); }
function checkAllDisk(){ const disk=($('rev-disk')||{}).value; if(!disk||disk==='all'){ alert(T('msg.select_disk_first')); return; } document.querySelectorAll('.delbox').forEach(function(b){ if(b.dataset.label===disk) b.checked=true; }); }
async function generateDeleteList(){ const dir=$('rev-report').value; if(!dir){ alert(T('msg.pick_report2')); return; } const boxes=document.querySelectorAll('.delbox:checked'); const paths=Array.prototype.map.call(boxes,function(b){return b.value;}); const folders=Array.from(selectedFolders); if(!paths.length && !folders.length){ alert(T('msg.pick_one')); return; } try{ const j=await api('/api/deletelist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir:dir,paths:paths,folders:folders})}); const pf=folders.length?T('msg.gen_pf',{n:folders.length}):''; let h='<p>'+T('msg.gen_ok',{total:j.total,files:j.files.length,pf:pf})+'</p><ul>'; j.files.forEach(function(f){ h+='<li><code>'+esc(f.label)+'</code> · '+f.count+' · <code>'+esc(f.path)+'</code> · <a href="/api/download?path='+encodeURIComponent(f.path)+'">'+esc(T('msg.download'))+'</a></li>'; }); h+='</ul><p class="muted">'+esc(T('msg.gen_hint'))+'</p>'; $('rev-result').innerHTML=h; }catch(e){ alert(T('msg.gen_fail')+e.message); } }

/* ---------------- 任务 ---------------- */
function poll(){ if(timer) clearTimeout(timer); timer=setTimeout(poll, 600); if(!currentJob) return; api('/api/job?id='+currentJob).then(function(j){ $('job-status').textContent = j.status==='running'?T('job.running'):(j.status==='done'?T('job.done'):T('job.error')); $('job-log').textContent = j.log || ''; $('job-log').scrollTop = $('job-log').scrollHeight; if(j.status!=='running'){ const done=j; currentJob=null; renderResult(done); } }).catch(function(){}); }
function renderResult(j){ const r=j.result||{}; const box=$('job-result'); let h=''; if(j.error) h+='<p class="err">'+esc(j.error)+'</p>'; if(j.kind==='scan' && r.index){ selectedIndexes.add(r.index); h+='<p>'+esc(T('msg.scan_done'))+'<code>'+esc(r.index)+'</code> · <a href="/api/download?path='+encodeURIComponent(r.index)+'">'+esc(T('msg.download'))+'</a></p>'; loadIndexes(); } if(j.kind==='compare'){ h+='<p>'+esc(T('msg.report_dir'))+'<code>'+esc(r.output_dir)+'</code></p>'; ['report.md','duplicates-exact.csv','similar-candidates.csv','groups.json'].forEach(function(f){ if(r[f]) h+='<a href="/api/report?path='+encodeURIComponent(r[f])+'" target="_blank">'+f+'</a> · <a href="/api/download?path='+encodeURIComponent(r[f])+'">'+esc(T('msg.download'))+'</a><br>'; }); loadReports(r.output_dir); switchTab('review'); } box.innerHTML=h; }
async function start(kind, payload){ try{ $('job-result').innerHTML=''; const j=await api('/api/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); currentJob=j.id; $('job-status').textContent=T('job.running'); poll(); }catch(e){ alert(T('msg.start_fail')+e.message); } }
function runScan(){ start('scan',{paths:lines('scan-paths'),name:$('scan-name').value.trim(),label:$('scan-label').value.trim(),min_size:$('scan-minsize').value.trim(),algo:$('scan-algo').value.trim(),include_hidden:$('scan-hidden').checked,follow_symlinks:$('scan-symlink').checked,hash:$('scan-hash').checked,ignore:$('scan-ignore').value,ignore_ext:$('scan-ignoreext').value,ext:$('scan-ext').value}); }
function runCompare(){ const indexes=Array.from(selectedIndexes); if(!indexes.length){ alert(T('msg.pick_index')); return; } start('compare',{indexes:indexes,name:$('cmp-name').value.trim(),threshold:$('cmp-threshold').value.trim(),cluster:$('cmp-cluster').value,sim_algo:$('cmp-simalgo').value,max_size_diff:$('cmp-sizediff').value.trim()}); }

/* ---------------- 初始化 ---------------- */
try{ const savedLang=localStorage.getItem('dc-lang'); if(savedLang && I18N[savedLang]) LANG=savedLang; }catch(e){}
if(!I18N[LANG]) LANG='zh';
$('lang').value=LANG;
initThemes();
applyI18n();
loadMounts();
loadIndexes();
loadReports();
</script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "dupeclean-web/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        try:
            print(fmt % args, file=_ORIG_STDERR)
        except Exception:
            pass

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, code: int, data: bytes, ctype: str,
               extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path in ("/", "/index.html"):
            page = INDEX_HTML.replace("__DC_LANG__", LANG)
            self._bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/mounts":
            include_system = (q.get("all") or ["0"])[0].lower() in ("1", "true", "yes")
            self._json(200, {"mounts": _list_mounts(include_system=include_system)})
            return
        if path == "/api/indexes":
            self._json(200, {"indexes": _list_indexes(), "workdir": _WORKDIR})
            return
        if path == "/api/reports":
            self._json(200, {"reports": _list_reports(), "workdir": _WORKDIR})
            return
        if path == "/api/groups":
            d = (q.get("dir") or [""])[0]
            realdir = _allowed_real_path(d) if d else None
            gj = os.path.join(realdir, "groups.json") if realdir else ""
            if not realdir or not os.path.isfile(gj):
                self._json(404, {"error": t("api.no_report")})
                return
            try:
                with open(gj, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                self._json(500, {"error": t("api.read_groups_fail", e=exc)})
                return
            data["dir"] = realdir
            self._json(200, data)
            return
        if path == "/api/dupfolders":
            d = (q.get("dir") or [""])[0]
            realdir = _allowed_real_path(d) if d else None
            gj = os.path.join(realdir, "groups.json") if realdir else ""
            if not realdir or not os.path.isfile(gj):
                self._json(404, {"error": t("api.no_report")})
                return
            try:
                with open(gj, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError) as exc:
                self._json(500, {"error": t("api.read_groups_fail", e=exc)})
                return
            indexes = meta.get("indexes") or []
            try:
                min_files = int((q.get("min_files") or [str(DEFAULT_MIN_FOLDER_FILES)])[0])
            except ValueError:
                min_files = DEFAULT_MIN_FOLDER_FILES
            folders, hashed = compute_duplicate_folders(indexes, min_files=max(1, min_files))
            self._json(200, {"folders": folders, "hashed": hashed,
                             "indexes": indexes, "dir": realdir})
            return
        if path == "/api/job":
            jid = (q.get("id") or [""])[0]
            job = _WEB_JOBS.get(jid)
            if not job:
                self._json(404, {"error": t("api.job_not_found")})
                return
            self._json(200, job.to_dict())
            return
        if path in ("/api/report", "/api/download"):
            p = (q.get("path") or [""])[0]
            real = _allowed_real_path(p) if p else None
            if not real or not os.path.isfile(real):
                self._json(404, {"error": t("api.file_denied")})
                return
            try:
                if path == "/api/report":
                    with open(real, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(4_000_000)
                    self._bytes(200, text.encode("utf-8"), "text/plain; charset=utf-8")
                    return
                with open(real, "rb") as fh:
                    data = fh.read()
                name = os.path.basename(real)
                self._bytes(200, data, "application/octet-stream",
                            {"Content-Disposition": f'attachment; filename="{name}"'})
                return
            except OSError as exc:
                self._json(500, {"error": str(exc)})
                return
        self._json(404, {"error": t("api.not_found")})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": t("api.bad_json")})
            return
        if path == "/api/deletelist":
            self._handle_deletelist(payload)
            return
        if path not in ("/api/scan", "/api/compare"):
            self._json(404, {"error": t("api.not_found")})
            return
        kind = path.rsplit("/", 1)[1]
        try:
            job = _start_web_job(kind, payload)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
            return
        self._json(200, job.to_dict())

    def _handle_deletelist(self, payload: dict) -> None:
        """根据用户勾选（文件 + 文件夹）生成删除名单 CSV。只写名单，绝不删除任何文件。"""
        directory = str(payload.get("dir") or "")
        realdir = _allowed_real_path(directory) if directory else None
        if not realdir or not os.path.isdir(realdir):
            self._json(400, {"error": t("api.dir_denied")})
            return
        gj = os.path.join(realdir, "groups.json")
        if not os.path.isfile(gj):
            self._json(400, {"error": t("api.no_groups")})
            return
        try:
            with open(gj, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            self._json(500, {"error": t("api.read_groups_fail", e=exc)})
            return

        # 收集行：每条 (action, source_label, path, filename, size, size_human, mtime_iso, hash, group_id, kind)
        rows: List[List] = []
        seen: Set[Tuple[str, str]] = set()

        def add(action, label, path, filename, size, size_human, mtime_iso, digest,
                group_id, kind):
            key = (action, path)
            if not path or key in seen:
                return
            seen.add(key)
            rows.append([group_id, kind, action, label, path, filename, size,
                         size_human, mtime_iso, digest])

        # 1) 单个文件勾选（必须来自 groups.json 的成员）
        valid: Dict[str, Tuple[dict, dict]] = {}
        for g in data.get("groups", []):
            for m in g.get("members", []):
                valid[m.get("path")] = (g, m)
        selected = payload.get("paths") or []
        if not isinstance(selected, list):
            self._json(400, {"error": t("api.paths_array")})
            return
        for p in selected:
            if p in valid:
                g, m = valid[p]
                add("delete", m.get("source_label", ""), m.get("path", ""),
                    m.get("filename", ""), m.get("size", ""), m.get("size_human", ""),
                    m.get("mtime_iso", ""), m.get("hash", ""), g.get("id"), g.get("kind"))

        # 2) 文件夹勾选 → 从原始扫描记录重新判定并展开为其全部文件 + 删除空目录(rmdir)
        selected_folders = payload.get("folders") or []
        if not isinstance(selected_folders, list):
            self._json(400, {"error": t("api.folders_array")})
            return
        if selected_folders:
            indexes = data.get("indexes") or []
            # 用 min_files=1 作为“超集”校验，并带上文件列表以便展开
            dupfolders, _hashed = compute_duplicate_folders(indexes, min_files=1,
                                                            include_files=True)
            fmap: Dict[str, dict] = {}
            for g in dupfolders:
                for c in g.get("copies", []):
                    fmap[os.path.realpath(c.get("path", ""))] = c
            for fp in selected_folders:
                c = fmap.get(os.path.realpath(str(fp)))
                if not c:
                    continue
                label = c.get("source_label", "")
                for fl in c.get("files", []):
                    add("delete", label, fl.get("path", ""), fl.get("filename", ""),
                        fl.get("size", ""), fl.get("size_human", ""), "",
                        fl.get("hash", ""), "", "duplicate-folder")
                add("rmdir", label, c.get("path", ""), os.path.basename(c.get("path", "")),
                    "", "", "", "", "", "duplicate-folder")

        if not rows:
            self._json(400, {"error": t("api.no_selection")})
            return

        # 按硬盘（source_label）分组，每块盘一个 CSV
        by_label: Dict[str, List[List]] = {}
        for row in rows:
            by_label.setdefault(str(row[3] or "UNKNOWN"), []).append(row)
        header = ["group_id", "kind", "action", "source_label", "path",
                  "filename", "size", "size_human", "mtime_iso", "hash"]
        files = []
        total = 0
        for label, items in sorted(by_label.items()):
            safe = _sanitize_name(label)
            outpath = os.path.join(realdir, f"delete-list-{safe}.csv")
            with _AtomicWriter(outpath) as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                for row in items:
                    writer.writerow(row)
            files.append({"label": label, "path": outpath, "count": len(items)})
            total += len(items)
        self._json(200, {"files": files, "total": total})


def _open_browser(url: str) -> bool:
    """尝试用系统默认浏览器打开 URL（多重兜底），返回是否已尝试启动。"""
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    import shutil as _shutil
    import subprocess
    for cmd in (["xdg-open", url], ["gio", "open", url],
                ["sensible-browser", url], ["x-www-browser", url],
                ["wslview", url]):
        exe = _shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except Exception:
            continue
    return False


def cmd_web(args: argparse.Namespace) -> int:
    global _WORKDIR
    _WORKDIR = _resolve_workdir(args.workdir)
    try:
        os.makedirs(_indexes_dir(), exist_ok=True)
        os.makedirs(_reports_dir(), exist_ok=True)
    except OSError as exc:
        eprint(t("web.workdir_fail", dir=_WORKDIR, e=exc))
        return 2
    httpd = ThreadingHTTPServer((args.host, args.port), WebHandler)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}/"
    eprint(t("web.started", url=url))
    eprint(t("web.workdir", dir=_WORKDIR))
    eprint(t("web.bind", host=args.host, port=args.port))
    if args.open:
        if _open_browser(url):
            eprint(t("web.opened"))
        else:
            eprint(t("web.open_fail"))
    eprint(t("web.stop"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        eprint("\n" + t("web.stopped"))
    finally:
        httpd.server_close()
    return 0


# ───────────────────────────── 配置与 CLI ─────────────────────────────

def load_config(path: Optional[str]) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    candidates: List[str] = []
    if path:
        candidates.append(path)
    else:
        default = os.path.join(os.getcwd(), "dupeclean.ini")
        if os.path.isfile(default):
            candidates.append(default)
    for c in candidates:
        if os.path.isfile(c):
            cfg.read(c, encoding="utf-8")
            break
    return cfg


def _cfg_get(cfg: configparser.ConfigParser, section: str, key: str) -> Optional[str]:
    if cfg.has_option(section, key):
        return cfg.get(section, key)
    return None


def _parse_list(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    items = [x.strip() for x in re.split(r"[,\n]", value) if x.strip()]
    return items


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=t("cli.desc"),
        epilog=t("cli.epilog"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {TOOL_VERSION}")
    parser.add_argument("--lang", choices=["auto", "zh", "en"], default="auto",
                        help=t("cli.lang"))
    parser.add_argument("--config", help=t("cli.config"))
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- scan ----
    p_scan = sub.add_parser("scan", help=t("cli.scan_help"))
    p_scan.add_argument("paths", nargs="+", help=t("help.paths"))
    p_scan.add_argument("-o", "--output", required=True, help=t("help.output_csv"))
    p_scan.add_argument("--label", help=t("help.label"))
    p_scan.add_argument("--min-size", type=parse_size, default=DEFAULT_MIN_SIZE,
                        help=t("help.min_size"))
    p_scan.add_argument("--ignore", action="append", default=[],
                        help=t("help.ignore"))
    p_scan.add_argument("--ignore-ext", action="append", default=[],
                        help=t("help.ignore_ext"))
    p_scan.add_argument("--ext", action="append", default=[],
                        help=t("help.ext"))
    p_scan.add_argument("--include-hidden", action="store_true",
                        help=t("help.include_hidden"))
    p_scan.add_argument("--follow-symlinks", action="store_true",
                        help=t("help.follow_symlinks"))
    p_scan.add_argument("--hash", action="store_true", help=t("help.hash"))
    p_scan.add_argument("--algo", default=DEFAULT_ALGO, help=t("help.algo"))
    p_scan.add_argument("--chunk-size", type=parse_size, default=DEFAULT_CHUNK,
                        help=t("help.chunk"))
    p_scan.add_argument("--quiet", action="store_true", help=t("help.quiet"))
    p_scan.add_argument("--print-path", action="store_true",
                        help=t("help.print_path"))
    p_scan.set_defaults(func=cmd_scan)

    # ---- compare ----
    p_cmp = sub.add_parser("compare", help=t("cli.cmp_help"))
    p_cmp.add_argument("indexes", nargs="+", help=t("help.indexes"))
    p_cmp.add_argument("-o", "--output", required=True, help=t("help.outdir"))
    p_cmp.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                       help=t("help.threshold"))
    p_cmp.add_argument("--cluster", choices=["pair", "group"], default="pair",
                       help=t("help.cluster"))
    p_cmp.add_argument("--sim-algo", choices=["auto", "rapidfuzz", "difflib"],
                       default="auto", help=t("help.sim_algo"))
    p_cmp.add_argument("--max-bucket", type=int, default=DEFAULT_MAX_BUCKET,
                       help=t("help.max_bucket"))
    p_cmp.add_argument("--max-pairs", type=int, default=DEFAULT_MAX_PAIRS,
                       help=t("help.max_pairs"))
    p_cmp.add_argument("--report-limit", type=int, default=DEFAULT_REPORT_LIMIT,
                       help=t("help.report_limit"))
    p_cmp.add_argument("--max-size-diff", type=parse_size, default=DEFAULT_SIZE_DIFF,
                       help=t("help.max_size_diff"))
    p_cmp.add_argument("--no-size-gate", action="store_true",
                       help=t("help.no_size_gate"))
    p_cmp.add_argument("--video-ext", action="append", default=[],
                       help=t("help.video_ext"))
    p_cmp.add_argument("--quiet", action="store_true", help=t("help.quiet"))
    p_cmp.add_argument("--print-path", action="store_true",
                       help=t("help.print_path"))
    p_cmp.set_defaults(func=cmd_compare)

    # ---- relabel ----
    p_rl = sub.add_parser("relabel", help=t("cli.relabel_help"))
    p_rl.add_argument("csvs", nargs="+", help=t("help.csvs"))
    p_rl.add_argument("--label", help=t("help.label"))
    p_rl.add_argument("--dry-run", action="store_true", help=t("help.dry_run"))
    p_rl.add_argument("--backup", dest="backup", action="store_true", default=True,
                      help=t("help.backup"))
    p_rl.add_argument("--no-backup", dest="backup", action="store_false",
                      help=t("help.no_backup"))
    p_rl.add_argument("--quiet", action="store_true")
    p_rl.set_defaults(func=cmd_relabel)

    # ---- web ----
    p_web = sub.add_parser("web", help=t("cli.web_help"))
    p_web.add_argument("--host", default="127.0.0.1", help=t("help.host"))
    p_web.add_argument("--port", type=int, default=8765, help=t("help.port"))
    p_web.add_argument("--workdir", default=None, help=t("help.workdir"))
    p_web.add_argument("--open", dest="open", action="store_true", default=True,
                       help=t("help.open"))
    p_web.add_argument("--no-open", dest="open", action="store_false",
                       help=t("help.no_open"))
    p_web.set_defaults(func=cmd_web)

    return parser


def apply_config(parser: argparse.ArgumentParser, argv: Sequence[str]) -> List[str]:
    """先解析出 --config，再用配置文件里的值填充各子命令默认值。"""
    cfg = load_config(None)
    # 需要提前知道 --config，做一次轻量扫描
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _rest = pre.parse_known_args(argv)
    if known.config:
        cfg = load_config(known.config)

    subparsers_action = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_action = action
            break
    if subparsers_action is None or not cfg.sections():
        return list(argv)

    for name, subparser in subparsers_action.choices.items():
        section = name
        if not cfg.has_section(section):
            continue
        for action in subparser._actions:
            if not action.option_strings or action.dest in ("help",):
                continue
            long_opt = next((o for o in action.option_strings if o.startswith("--")), None)
            key = (long_opt or action.option_strings[0]).lstrip("-").replace("-", "_")
            raw = _cfg_get(cfg, section, key)
            if raw is None:
                continue
            try:
                if action.dest == "paths" or action.dest == "indexes":
                    continue
                if isinstance(action, argparse._AppendAction):
                    action.default = _parse_list(raw) or []
                elif isinstance(action, argparse._StoreTrueAction):
                    action.default = raw.strip().lower() in ("1", "true", "yes", "on")
                elif action.type is parse_size:
                    action.default = parse_size(raw)
                elif action.type is int:
                    action.default = int(raw)
                elif action.type is float:
                    action.default = float(raw)
                elif action.choices is not None:
                    action.default = raw
                elif action.nargs in (None,) and action.dest not in ("output", "label", "config"):
                    action.default = raw
                elif action.dest == "label":
                    action.default = raw
            except (ValueError, TypeError) as exc:
                eprint(t("warn.cfg", section=section, key=key, e=exc))
    return list(argv)


def _prescan_lang(argv: Sequence[str]) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    set_lang(_prescan_lang(list(argv)) or os.environ.get("DUPCLEAN_LANG"))
    parser = build_parser()
    argv = apply_config(parser, argv)
    args = parser.parse_args(argv)
    set_lang(getattr(args, "lang", None) or os.environ.get("DUPCLEAN_LANG"))
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
