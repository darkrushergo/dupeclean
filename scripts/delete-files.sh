#!/usr/bin/env bash
# delete-files.sh —— 根据 dupeclean 生成的删除名单 CSV 删除文件
#
# ⚠️ 危险操作：本脚本会真正删除文件。默认只“演练”（列出将删除的文件），
#    必须显式加 --yes 才会真正删除。请先演练并核对无误后再执行。
#
# 用法：
#   ./delete-files.sh [--yes] [--map OLD=NEW]... <删除名单.csv 或 目录>...
#
#   --yes / -y     真正执行删除（不加则只显示将删除的文件）
#   --map A=B      把路径前缀 A 替换为 B（可多次，用于换挂载点/换盘符）
#   -h / --help    显示帮助
#
# 说明：
#   * 处理 action=delete（删文件）与 action=rmdir（仅删空目录，不递归）。
#   * 删除不可逆！请先演练、核对名单、保留至少一份并备份。详见 DISCLAIMER.md。
#   * 若传入目录，会自动读取其中所有 delete-list-*.csv。
#   * 会自动读取同目录下的 delete-map.txt（每行 OLD=NEW，# 开头为注释）。
#   * 删除名单由 dupeclean 生成；本脚本与 dupeclean 主程序相互独立。
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：需要 python3。" >&2
  exit 1
fi

python3 - "$@" <<'PYEOF'
import csv
import glob
import os
import sys

argv = sys.argv[1:]
do_delete = False
maps = []
targets = []
i = 0
while i < len(argv):
    a = argv[i]
    if a in ("--yes", "-y", "--delete"):
        do_delete = True
        i += 1
    elif a == "--map":
        if i + 1 >= len(argv):
            print("错误：--map 后面需要一个 OLD=NEW 参数", file=sys.stderr)
            sys.exit(2)
        maps.append(argv[i + 1])
        i += 2
    elif a.startswith("--map="):
        maps.append(a.split("=", 1)[1])
        i += 1
    elif a in ("-h", "--help"):
        print("用法: delete-files.sh [--yes] [--map OLD=NEW]... "
              "<删除名单.csv 或 目录>...")
        print("  默认演练（不删除）；加 --yes 才真正删除。")
        print("  --map OLD=NEW 可多次，用于把扫描路径前缀换成本机挂载点。")
        sys.exit(0)
    else:
        targets.append(a)
        i += 1

if not targets:
    print("用法: delete-files.sh [--yes] [--map OLD=NEW]... <删除名单.csv 或 目录>...")
    sys.exit(2)


def load_map_file(directory):
    p = os.path.join(directory, "delete-map.txt")
    if not os.path.isfile(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                maps.append(line)
    except OSError as exc:
        print(f"[警告] 读取 {p} 失败: {exc}", file=sys.stderr)


csvs = []
for t in targets:
    if os.path.isdir(t):
        load_map_file(t)
        found = sorted(glob.glob(os.path.join(t, "delete-list-*.csv")))
        if not found:
            found = sorted(glob.glob(os.path.join(t, "delete-list.csv")))
        if not found:
            print(f"[警告] {t} 里没有 delete-list-*.csv")
        csvs += found
    else:
        load_map_file(os.path.dirname(os.path.abspath(t)) or ".")
        csvs.append(t)

parsed_maps = []
for m in maps:
    if "=" in m:
        old, new = m.split("=", 1)
        parsed_maps.append((old.strip(), new.strip()))
parsed_maps.sort(key=lambda x: -len(x[0]))


def remap(path):
    for old, new in parsed_maps:
        if not old:
            continue
        if path == old or path.startswith(old.rstrip("/") + "/"):
            return new.rstrip("/") + path[len(old):]
    return path


print("=" * 74)
print("  DANGER / 危险：本脚本会永久删除文件，无法恢复。")
print("  外置盘/USB/NAS 通常没有回收站；删除不可逆。")
print("  模式：" + ("真实删除 (--yes)  >>> 将永久删除 <<<" if do_delete else "演练 (dry-run，不删除)"))
print("  重复判定为启发式，可能误判。请先演练、核对名单、保留至少一份并先备份。")
print("  完整免责声明见 DISCLAIMER.md。风险自负。")
print("=" * 74)
print()

total = will = done = missing = skipped = failed = 0
for c in csvs:
    if not os.path.isfile(c):
        print(f"[跳过] 找不到 CSV: {c}")
        continue
    try:
        with open(c, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                action = (row.get("action") or "").strip().lower()
                if action not in ("delete", "rmdir", "delete-dir", "rmdir-dir"):
                    continue
                raw = (row.get("path") or "").strip()
                if not raw or raw in ("/", "\\"):
                    continue
                total += 1
                p = remap(raw)
                if not os.path.exists(p):
                    missing += 1
                    continue
                if action in ("rmdir", "rmdir-dir", "delete-dir"):
                    # 只删除“空目录”（不递归）
                    if not os.path.isdir(p):
                        skipped += 1
                        continue
                    try:
                        empty = not os.listdir(p)
                    except OSError:
                        empty = False
                    if not empty:
                        skipped += 1
                        print(f"[跳过] 目录非空，保留: {p}")
                        continue
                    will += 1
                    if do_delete:
                        try:
                            os.rmdir(p)
                            done += 1
                            print(f"[删除空目录] {p}")
                        except OSError as exc:
                            failed += 1
                            print(f"[失败] {p}: {exc}")
                    else:
                        print(f"[将删除空目录] {p}")
                    continue
                if os.path.isdir(p):
                    skipped += 1
                    print(f"[跳过] 是目录，不删除: {p}")
                    continue
                will += 1
                if do_delete:
                    try:
                        os.remove(p)
                        done += 1
                        print(f"[删除] {p}")
                    except OSError as exc:
                        failed += 1
                        print(f"[失败] {p}: {exc}")
                else:
                    print(f"[将删除] {p}")
    except OSError as exc:
        print(f"[错误] 读取 {c} 失败: {exc}", file=sys.stderr)

print()
if do_delete:
    print(f"完成：删除 {done} 个，失败 {failed} 个，缺失 {missing} 个，跳过(目录) {skipped} 个。")
else:
    print(f"演练模式（未删除任何文件）：待删除 {will} 个，缺失 {missing} 个，跳过(目录) {skipped} 个。")
    print("确认无误后，加上 --yes 再运行一次即可真正删除。")
PYEOF
