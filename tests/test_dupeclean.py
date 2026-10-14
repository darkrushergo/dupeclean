#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dupeclean 测试（纯标准库 unittest，零依赖）

运行：
    python3 -m unittest discover -s tests -v
或：
    python3 tests/test_dupeclean.py -v
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "dupeclean.py")
sys.path.insert(0, REPO)

import dupeclean as dc  # noqa: E402


# ───────────────────────── 单元测试 ─────────────────────────

class TestHelpers(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(dc.parse_size("0"), 0)
        self.assertEqual(dc.parse_size("50M"), 50 * 1024 ** 2)
        self.assertEqual(dc.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(dc.parse_size("1048576"), 1048576)
        self.assertEqual(dc.parse_size("2GiB"), 2 * 1024 ** 3)

    def test_human_size(self):
        self.assertEqual(dc.human_size(0), "0 B")
        self.assertEqual(dc.human_size(1024), "1.00 KiB")
        self.assertEqual(dc.human_size(50 * 1024 ** 2), "50.00 MiB")

    def test_normalize_name(self):
        # NFC + casefold（重音不剥离，大小写统一）
        self.assertEqual(dc.normalize_name("CAFÉ.MKV"), dc.normalize_name("café.mkv"))
        # 组合字符 → 预组合字符
        import unicodedata
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertEqual(dc.normalize_name(decomposed), dc.normalize_name("café"))

    def test_strip_ext(self):
        self.assertEqual(dc.strip_ext("a.b.c.mkv"), "a.b.c")
        self.assertEqual(dc.strip_ext("noext"), "noext")
        self.assertEqual(dc.strip_ext(".hidden"), ".hidden")

    def test_ext_of(self):
        self.assertEqual(dc.ext_of("A.MKV"), ".mkv")
        self.assertEqual(dc.ext_of("noext"), "")

    def test_parse_season_episode(self):
        self.assertEqual(dc.parse_season_episode("show.s01e02.1080p"), (1, 2))
        self.assertEqual(dc.parse_season_episode("show.1x02"), (1, 2))
        self.assertEqual(dc.parse_season_episode("剧.第3集"), (None, 3))
        self.assertEqual(dc.parse_season_episode("剧.第2季"), (2, None))
        self.assertEqual(dc.parse_season_episode("show.ep12"), (None, 12))
        self.assertEqual(dc.parse_season_episode("movie.2020.1080p"), (None, None))

    def test_adjust_score_episode_penalty(self):
        a = (1, 1)
        b = (1, 2)
        self.assertEqual(dc.adjust_score(0.95, a, b), 0.0)        # 不同集 → 排除
        self.assertEqual(dc.adjust_score(0.95, (1, 1), (1, 1)), 0.95)
        self.assertEqual(dc.adjust_score(0.95, (1, 2), (2, 2)), 0.0)  # 不同季
        # 一方有集、一方没有 → 轻微惩罚
        self.assertAlmostEqual(dc.adjust_score(1.0, (1, 1), (None, None)), 0.95)

    def test_similarity(self):
        self.assertEqual(dc.similarity("abc", "abc", "difflib"), 1.0)
        self.assertGreater(dc.similarity("abcdef", "abcdeg", "difflib"), 0.8)
        self.assertEqual(dc.similarity("", "abc", "difflib"), 0.0)

    def test_make_grams_tokens(self):
        grams = dc.make_grams("film.one.2020")
        self.assertIn("fil", grams)
        tokens = dc.make_tokens("Film.One.2020.mkv")
        self.assertIn("film", tokens)
        self.assertIn("one", tokens)
        self.assertIn("2020", tokens)
        self.assertIn("mkv", tokens)
        # 少于 3 个字符的 token 不索引
        self.assertNotIn("cd", dc.make_tokens("a.b.cd"))

    def test_infer_label_fallback(self):
        # 临时目录不是挂载点，应回退为 basename
        with tempfile.TemporaryDirectory() as d:
            label = dc.infer_label(d)
            self.assertTrue(label)

    def test_is_system_mount(self):
        self.assertTrue(dc._is_system_mount("/"))
        self.assertTrue(dc._is_system_mount("/boot/efi"))
        self.assertTrue(dc._is_system_mount("/snap/firefox/9036"))
        self.assertTrue(dc._is_system_mount("/var/lib/docker/overlay2"))
        self.assertFalse(dc._is_system_mount("/mnt/data"))
        self.assertFalse(dc._is_system_mount("/run/media/user/DISK_A"))
        self.assertFalse(dc._is_system_mount("/media/user/DISK_B"))


# ───────────────────────── 集成测试 ─────────────────────────

def make_tree(base: str) -> None:
    a = os.path.join(base, "diskA")
    b = os.path.join(base, "diskB")
    os.makedirs(os.path.join(a, "Movies"))
    os.makedirs(os.path.join(a, "Dup"))
    os.makedirs(os.path.join(a, "Show"))
    os.makedirs(os.path.join(a, ".hidden"))
    os.makedirs(os.path.join(b, "Movies"))
    os.makedirs(os.path.join(b, "Dup"))

    def w(path, n, ch):
        with open(path, "wb") as fh:
            fh.write(ch * n)

    w(os.path.join(a, "Movies", "Film.One.2020.720p.mkv"), 100, b"A")
    w(os.path.join(b, "Movies", "Film.One.2020.IMAX.2160p.mkv"), 300, b"B")
    w(os.path.join(a, "Dup", "SameFile.bin"), 200, b"C")
    w(os.path.join(b, "Dup", "SameFile.bin"), 200, b"C")   # 内容相同 → 哈希也应相同
    w(os.path.join(a, "SameNameDiffSize.dat"), 100, b"D")
    w(os.path.join(b, "SameNameDiffSize.dat"), 150, b"E")
    w(os.path.join(a, "Show", "Show.S01E01.mkv"), 120, b"F")
    w(os.path.join(a, "Show", "Show.S01E02.mkv"), 120, b"G")
    w(os.path.join(a, "Café.Movie.mkv"), 130, b"H")
    w(os.path.join(b, "CAFE.MOVIE.mkv"), 130, b"H")        # 内容相同
    # 应被排除
    open(os.path.join(a, "empty.txt"), "wb").close()
    w(os.path.join(a, "temp.tmp"), 10, b"x")
    w(os.path.join(a, ".hidden", "secret.mkv"), 10, b"x")
    link = os.path.join(a, "link_to_b.bin")
    try:
        os.symlink(os.path.join(b, "Dup", "SameFile.bin"), link)
    except (OSError, NotImplementedError):
        pass


def run(args, cwd):
    env = dict(os.environ)
    env["DUPCLEAN_LANG"] = "zh"   # 稳定测试输出语言
    return subprocess.run([sys.executable, SCRIPT] + args, cwd=cwd,
                          capture_output=True, text=True, env=env)


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dc_test_")
        make_tree(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _snapshot(self):
        snap = {}
        for root, _dirs, files in os.walk(self.base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    with open(p, "rb") as fh:
                        snap[p] = (os.path.getsize(p), fh.read())
                except OSError:
                    pass
        return snap

    def test_scan_filters_and_readonly(self):
        before = self._snapshot()
        idx = os.path.join(self.base, "A.index.csv")
        r = run(["scan", os.path.join(self.base, "diskA"), "-o", idx,
                 "--min-size", "0", "--label", "DISK_A"], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(idx))
        with open(idx, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# schema_version: 1", text)
        self.assertIn("# source_label: DISK_A", text)
        self.assertIn("# hashed: false", text)
        body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
        # 应包含 6 个普通文件；排除空文件/临时扩展名/隐藏目录/软链
        self.assertIn("SameFile.bin", body)
        self.assertNotIn("empty.txt", body)
        self.assertNotIn("temp.tmp", body)
        self.assertNotIn("secret.mkv", body)
        self.assertNotIn("link_to_b.bin", body)
        # 只读：原始文件内容/大小未变（新增索引文件除外）
        after = self._snapshot()
        for p, v in before.items():
            self.assertEqual(after.get(p), v, f"原始文件被改动：{p}")

    def test_compare_finds_groups(self):
        a_idx = os.path.join(self.base, "A.index.csv")
        b_idx = os.path.join(self.base, "B.index.csv")
        run(["scan", os.path.join(self.base, "diskA"), "-o", a_idx,
             "--min-size", "0", "--label", "DISK_A"], self.base)
        run(["scan", os.path.join(self.base, "diskB"), "-o", b_idx,
             "--min-size", "0", "--label", "DISK_B"], self.base)
        out = os.path.join(self.base, "report")
        r = run(["compare", a_idx, b_idx, "-o", out], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)

        with open(os.path.join(out, "report.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("## A. 疑似精确重复组", md)
        self.assertIn("SameFile.bin", md)
        self.assertIn("## A2.", md)
        self.assertIn("SameNameDiffSize.dat", md)
        self.assertIn("## B. 相似候选", md)
        self.assertIn("Film.One.2020.720p.mkv", md)
        self.assertIn("Film.One.2020.IMAX.2160p.mkv", md)
        # 同剧不同集不应出现在 B 段
        b_section = md.split("## B.")[1]
        self.assertNotIn("S01E01", b_section)
        self.assertNotIn("S01E02", b_section)

        # 分类 CSV 存在且格式正确
        with open(os.path.join(out, "duplicates-exact.csv"), encoding="utf-8") as fh:
            exact = fh.read()
        self.assertIn("group_id,kind,member_index", exact)
        self.assertIn("exact", exact)
        self.assertIn("same-name-different", exact)
        with open(os.path.join(out, "similar-candidates.csv"), encoding="utf-8") as fh:
            sim = fh.read()
        self.assertIn("group_id,kind,group_size,similarity", sim)
        self.assertIn("pair", sim)

    def test_hash_mode(self):
        a_idx = os.path.join(self.base, "Ah.index.csv")
        b_idx = os.path.join(self.base, "Bh.index.csv")
        run(["scan", os.path.join(self.base, "diskA"), "-o", a_idx,
             "--min-size", "0", "--label", "DISK_A", "--hash"], self.base)
        run(["scan", os.path.join(self.base, "diskB"), "-o", b_idx,
             "--min-size", "0", "--label", "DISK_B", "--hash"], self.base)
        with open(a_idx, encoding="utf-8") as fh:
            self.assertIn("# hashed: true", fh.read())

        out = os.path.join(self.base, "report_hash")
        r = run(["compare", a_idx, b_idx, "-o", out], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(out, "report.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("## A. 精确重复组（哈希完全相同）", md)
        # SameFile.bin 内容相同 → 应作为哈希精确重复出现
        a_section = md.split("## A2.")[0]
        self.assertIn("SameFile.bin", a_section)
        self.assertIn("CAFE.MOVIE.mkv", a_section)

    def test_cluster_group_and_config(self):
        a_idx = os.path.join(self.base, "A.index.csv")
        b_idx = os.path.join(self.base, "B.index.csv")
        run(["scan", os.path.join(self.base, "diskA"), "-o", a_idx,
             "--min-size", "0"], self.base)
        run(["scan", os.path.join(self.base, "diskB"), "-o", b_idx,
             "--min-size", "0"], self.base)
        cfg = os.path.join(self.base, "dupeclean.ini")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("[compare]\nthreshold = 0.8\ncluster = group\n")
        out = os.path.join(self.base, "report_cfg")
        r = run(["--config", cfg, "compare", a_idx, b_idx, "-o", out], self.base)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(out, "report.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("相似分组（连通聚类）", md)
        with open(os.path.join(out, "similar-candidates.csv"), encoding="utf-8") as fh:
            self.assertIn("group", fh.read())

    def test_schema_version_mismatch_rejected(self):
        idx = os.path.join(self.base, "bad.index.csv")
        with open(idx, "w", encoding="utf-8") as fh:
            fh.write("# dupeclean-index\n# schema_version: 99\n")
            fh.write("schema_version,source_label,source_root,rel_path,filename,ext,"
                     "size,mtime_epoch,mtime_iso,inode,device,hash_algo,hash\n")
            fh.write("99,X,/x,a.mkv,a.mkv,.mkv,1,0,0,1,1,,\n")
        out = os.path.join(self.base, "r")
        r = run(["compare", idx, "-o", out], self.base)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema_version", r.stderr)


class TestWebUI(unittest.TestCase):
    """启动真实 HTTP 服务，走一遍 Web UI 的 API。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.base = tempfile.mkdtemp(prefix="dc_web_")
        make_tree(cls.base)
        cls.workdir = os.path.join(cls.base, "workspace")
        dc._WORKDIR = cls.workdir
        os.makedirs(dc._indexes_dir(), exist_ok=True)
        os.makedirs(dc._reports_dir(), exist_ok=True)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), dc.WebHandler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        dc._WEB_ACTIVE = None
        dc._WEB_JOBS.clear()
        dc._ALLOWED_DIRS.clear()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as r:
            return r.status, r.read()

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())

    def wait_job(self, jid, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _s, body = self.get(f"/api/job?id={jid}")
            job = json.loads(body)
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        self.fail("任务超时未完成")

    def test_index_and_mounts(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Dupeclean", body)
        status, body = self.get("/api/mounts")
        self.assertEqual(status, 200)
        self.assertIn("mounts", json.loads(body))
        # 默认与 all=1 均应可用
        status, body = self.get("/api/mounts?all=1")
        self.assertEqual(status, 200)
        self.assertIn("mounts", json.loads(body))

    def test_full_scan_compare_flow(self):
        # 用结果目录默认路径（不传 output），模拟 Web 的正常使用
        _s, job = self.post("/api/scan", {
            "paths": [os.path.join(self.base, "diskA")], "name": "DISK_A",
            "label": "DISK_A", "min_size": "0"})
        done = self.wait_job(job["id"])
        self.assertEqual(done["status"], "done", done.get("log"))
        a_idx = done["result"]["index"]
        self.assertTrue(a_idx.startswith(os.path.join(self.workdir, "indexes")))
        self.assertTrue(os.path.isfile(a_idx))

        _s, job = self.post("/api/scan", {
            "paths": [os.path.join(self.base, "diskB")], "name": "DISK_B",
            "label": "DISK_B", "min_size": "0"})
        done = self.wait_job(job["id"])
        self.assertEqual(done["status"], "done")
        b_idx = done["result"]["index"]

        # /api/indexes 能把刚生成的索引列出来供点选
        status, body = self.get("/api/indexes")
        names = [it["name"] for it in json.loads(body)["indexes"]]
        self.assertIn("DISK_A.index.csv", names)
        self.assertIn("DISK_B.index.csv", names)

        _s, job = self.post("/api/compare", {
            "indexes": [a_idx, b_idx], "name": "r1", "threshold": "0.75"})
        done = self.wait_job(job["id"])
        self.assertEqual(done["status"], "done", done.get("log"))
        report = done["result"]["report.md"]
        groups_json = done["result"]["groups.json"]
        self.assertTrue(os.path.isfile(report))
        self.assertTrue(os.path.isfile(groups_json))
        outdir = done["result"]["output_dir"]

        # /api/reports 能列出该报告
        status, body = self.get("/api/reports")
        self.assertIn(outdir, [r["path"] for r in json.loads(body)["reports"]])

        # /api/groups 返回结构化分组
        status, body = self.get("/api/groups?dir=" + urllib.parse.quote(outdir))
        data = json.loads(body)
        kinds = {g["kind"] for g in data["groups"]}
        self.assertIn("exact", kinds)
        self.assertIn("similar", kinds)
        self.assertTrue(any(len(g["members"]) >= 2 for g in data["groups"]))

        # 报告预览与下载
        status, body = self.get("/api/report?path=" + urllib.parse.quote(report))
        self.assertEqual(status, 200)
        self.assertIn("疑似精确重复组".encode("utf-8"), body)
        csvp = done["result"]["similar-candidates.csv"]
        status, body = self.get("/api/download?path=" + urllib.parse.quote(csvp))
        self.assertEqual(status, 200)
        self.assertIn(b"similarity", body)

        # 生成删除名单：从不同盘各选一个文件，应按盘拆成多个 CSV
        by_label = {}
        for g in data["groups"]:
            for m in g["members"]:
                by_label.setdefault(m["source_label"], m["path"])
        self.assertIn("DISK_A", by_label)
        self.assertIn("DISK_B", by_label)
        _s, res = self.post("/api/deletelist", {
            "dir": outdir,
            "paths": [by_label["DISK_A"], by_label["DISK_B"], "/nonexistent/ignored"]})
        self.assertEqual(res["total"], 2)
        self.assertEqual({f["label"] for f in res["files"]}, {"DISK_A", "DISK_B"})
        for f in res["files"]:
            self.assertTrue(os.path.isfile(f["path"]))
            self.assertIn("delete-list-", os.path.basename(f["path"]))
        with open(res["files"][0]["path"], encoding="utf-8") as fh:
            dl = fh.read()
        self.assertIn("action", dl)

        # 重复文件夹（夹具里 Dup 各只有 1 个文件，故 min_files=1）
        status, body = self.get("/api/dupfolders?dir=" + urllib.parse.quote(outdir)
                                + "&min_files=1")
        fdata = json.loads(body)
        fpaths = [c["path"] for g in fdata["folders"] for c in g["copies"]]
        dup_dirs = [p for p in fpaths if p.endswith("/Dup")]
        self.assertGreaterEqual(len(dup_dirs), 2, fpaths)
        _s, fres = self.post("/api/deletelist", {
            "dir": outdir, "paths": [], "folders": [dup_dirs[0]]})
        self.assertGreaterEqual(fres["total"], 2)   # 至少 1 个文件 + 1 个 rmdir
        with open(fres["files"][0]["path"], encoding="utf-8") as fh:
            self.assertIn("rmdir", fh.read())

        # 越权读取被拒
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/download?path=" + urllib.parse.quote("/etc/passwd"))
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()

    def test_busy_returns_409(self):
        dc._WEB_ACTIVE = "busy"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post("/api/scan", {"paths": [self.base],
                                       "output": os.path.join(self.base, "z.csv")})
            self.assertEqual(cm.exception.code, 409)
            cm.exception.close()
        finally:
            dc._WEB_ACTIVE = None

    def test_bad_payload_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/scan", {"paths": [], "output": "x.csv"})
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


class TestRelabel(unittest.TestCase):
    def test_derive_label(self):
        self.assertEqual(dc.derive_label_from_filename("DISK_A.index.csv"), "DISK_A")
        self.assertEqual(dc.derive_label_from_filename("/x/DISK_B.csv"), "DISK_B")
        self.assertEqual(dc.derive_label_from_filename("my.disk.index.csv"), "my")

    def test_relabel_rewrites_label(self):
        import argparse
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "MyDisk.index.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("# dupeclean-index\n# schema_version: 1\n# source_label: WRONG\n")
                fh.write("schema_version,source_label,source_root,rel_path,filename,"
                         "ext,size,mtime_epoch,mtime_iso,inode,device,hash_algo,hash\n")
                fh.write("1,WRONG,/x,a.mkv,a.mkv,.mkv,10,0,0,1,1,,\n")
            args = argparse.Namespace(csvs=[p], label=None, dry_run=False,
                                      backup=True, quiet=True)
            self.assertEqual(dc.cmd_relabel(args), 0)
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("# source_label: MyDisk", text)
            self.assertIn("1,MyDisk,/x,a.mkv", text)
            self.assertNotIn("WRONG", text)
            self.assertTrue(os.path.isfile(p + ".bak"))
            # 再读一次仍能被正常解析
            recs, _metas = dc.load_indexes([p])
            self.assertEqual(recs[0].source_label, "MyDisk")

    def test_relabel_dry_run_keeps_file(self):
        import argparse
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "DiskX.index.csv")
            original = ("# source_label: OLD\n"
                        "schema_version,source_label,hash\n1,OLD,\n")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(original)
            args = argparse.Namespace(csvs=[p], label=None, dry_run=True,
                                      backup=True, quiet=True)
            self.assertEqual(dc.cmd_relabel(args), 0)
            with open(p, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)


class TestDeleteScripts(unittest.TestCase):
    """验证 Linux 删除脚本：默认演练、--yes 才删、目录受保护、--map 生效。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dc_delscript_")
        self.mnt = os.path.join(self.base, "mnt")
        os.makedirs(os.path.join(self.mnt, "A", "adir"))
        os.makedirs(os.path.join(self.mnt, "B"))
        for p in ("A/a1.bin", "A/keep.bin", "B/b1.bin"):
            with open(os.path.join(self.mnt, p), "wb") as fh:
                fh.write(b"x")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _write_csv(self, name, rows):
        p = os.path.join(self.base, name)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("group_id,kind,action,source_label,path,filename,size,"
                     "size_human,mtime_iso,hash\n")
            for action, label, path in rows:
                fh.write(f"1,exact,{action},{label},{path},x,1,1 B,,\n")
        return p

    def test_dry_run_then_delete(self):
        script = os.path.join(REPO, "scripts", "delete-files.sh")
        if not os.path.isfile(script):
            self.skipTest("delete-files.sh 不存在")
        self._write_csv("delete-list-DISK_A.csv", [
            ("delete", "DISK_A", "OLDROOT/A/a1.bin"),
            ("keep", "DISK_A", "OLDROOT/A/keep.bin"),
            ("delete", "DISK_A", "OLDROOT/A/adir"),
            ("delete", "DISK_A", "OLDROOT/A/missing.bin"),
        ])
        args = [script, "--map", f"OLDROOT={self.mnt}", self.base]

        before = os.path.isfile(os.path.join(self.mnt, "A", "a1.bin"))
        self.assertTrue(before)
        r = subprocess.run(["bash"] + args, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("演练模式", r.stdout)
        # 演练不删任何东西
        self.assertTrue(os.path.isfile(os.path.join(self.mnt, "A", "a1.bin")))
        self.assertIn("是目录，不删除", r.stdout)

        r = subprocess.run(["bash", script, "--yes", "--map", f"OLDROOT={self.mnt}",
                            os.path.join(self.base, "delete-list-DISK_A.csv")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.mnt, "A", "a1.bin")))
        # action=keep 的文件不应被删；目录也不应被删
        self.assertTrue(os.path.isfile(os.path.join(self.mnt, "A", "keep.bin")))
        self.assertTrue(os.path.isdir(os.path.join(self.mnt, "A", "adir")))

    def test_delete_map_file(self):
        script = os.path.join(REPO, "scripts", "delete-files.sh")
        if not os.path.isfile(script):
            self.skipTest("delete-files.sh 不存在")
        self._write_csv("delete-list-DISK_A.csv",
                        [("delete", "DISK_A", "OLDROOT/B/b1.bin")])
        with open(os.path.join(self.base, "delete-map.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"# map\nOLDROOT={self.mnt}\n")
        r = subprocess.run(["bash", script, "--yes", self.base],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.mnt, "B", "b1.bin")))


class TestSizeGate(unittest.TestCase):
    """非视频文件大小差 > 阈值则不算相似候选；视频豁免。"""

    def _write_index(self, d, rows):
        idx = os.path.join(d, "gate.index.csv")
        with open(idx, "w", encoding="utf-8", newline="") as fh:
            fh.write("# dupeclean-index\n# schema_version: 1\n# hashed: false\n")
            w = csv.writer(fh)
            w.writerow(["schema_version", "source_label", "source_root", "rel_path",
                        "filename", "ext", "size", "mtime_epoch", "mtime_iso",
                        "inode", "device", "hash_algo", "hash"])
            for i, (name, size) in enumerate(rows):
                w.writerow([1, "DISK", d, name, name,
                            os.path.splitext(name)[1].lower(), size, "0",
                            "2020-01-01T00:00:00+08:00", i, 1, "", ""])
        return idx

    def test_non_video_size_gate_and_video_exempt(self):
        MB = 1024 * 1024
        rows = [
            ("archive.backup.tar", 100 * MB),
            ("archive.backup2.tar", 200 * MB),        # 非视频，差 100MB → 应被剔除
            ("movie.name.720p.mkv", 100 * MB),
            ("movie.name.720p.IMAX.mkv", 2000 * MB),  # 视频，差很大 → 应保留
        ]
        with tempfile.TemporaryDirectory() as d:
            idx = self._write_index(d, rows)
            out = os.path.join(d, "r")
            r = run(["compare", idx, "-o", out, "--threshold", "0.7"], d)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(out, "groups.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            pairs = {"|".join(sorted(m["filename"] for m in g["members"]))
                     for g in data["groups"] if g["kind"] == "similar"}
            self.assertTrue(any("movie.name.720p.mkv" in p for p in pairs), pairs)
            self.assertFalse(any("archive.backup.tar" in p and "archive.backup2.tar" in p
                                 for p in pairs), pairs)
            self.assertGreaterEqual(data.get("gated_pairs", 0), 1)

            # 关闭大小过滤后，tar 对应出现
            out2 = os.path.join(d, "r2")
            r2 = run(["compare", idx, "-o", out2, "--threshold", "0.7",
                      "--no-size-gate"], d)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            with open(os.path.join(out2, "groups.json"), encoding="utf-8") as fh:
                data2 = json.load(fh)
            pairs2 = {"|".join(sorted(m["filename"] for m in g["members"]))
                      for g in data2["groups"] if g["kind"] == "similar"}
            self.assertTrue(any("archive.backup.tar" in p and "archive.backup2.tar" in p
                                for p in pairs2), pairs2)


class TestDuplicateFolders(unittest.TestCase):
    """重复文件夹判定：基于原始扫描记录，路径无关，按内容签名。"""

    def _idx(self, d, rows):
        p = os.path.join(d, "f.index.csv")
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("# dupeclean-index\n# schema_version: 1\n")
            w = csv.writer(fh)
            w.writerow(dc.INDEX_COLUMNS)
            for i, (lab, root, rel, name, size, h) in enumerate(rows):
                w.writerow([1, lab, root, rel, name, os.path.splitext(name)[1],
                            size, "0", "", i, 1, "sha256", h])
        return p

    def test_identical_folders_detected_path_independent(self):
        rows = [
            ("A", "/mnt/A", "Show/S1/a.mkv", "a.mkv", 1000, "h1"),
            ("A", "/mnt/A", "Show/S1/b.mkv", "b.mkv", 2000, "h2"),
            ("B", "/mnt/B", "Other/Name/a.mkv", "a.mkv", 1000, "h1"),
            ("B", "/mnt/B", "Other/Name/b.mkv", "b.mkv", 2000, "h2"),
            # 名字相同但大小/哈希不同 → 不算完全重复
            ("A", "/mnt/A", "Show/S2/a.mkv", "a.mkv", 1000, "h1"),
            ("A", "/mnt/A", "Show/S2/b.mkv", "b.mkv", 9999, "h9"),
        ]
        with tempfile.TemporaryDirectory() as d:
            idx = self._idx(d, rows)
            folders, hashed = dc.compute_duplicate_folders([idx])
            self.assertTrue(hashed)
            self.assertEqual(len(folders), 1)
            g = folders[0]
            self.assertEqual(g["file_count"], 2)
            paths = sorted(c["path"] for c in g["copies"])
            self.assertEqual(paths, ["/mnt/A/Show/S1", "/mnt/B/Other/Name"])

    def test_min_files_filters_single_file_folder(self):
        rows = [
            ("A", "/mnt/A", "X/one.bin", "one.bin", 10, "h"),
            ("B", "/mnt/B", "X/one.bin", "one.bin", 10, "h"),
        ]
        with tempfile.TemporaryDirectory() as d:
            idx = self._idx(d, rows)
            g1, _ = dc.compute_duplicate_folders([idx], min_files=1)
            g2, _ = dc.compute_duplicate_folders([idx], min_files=2)
            self.assertEqual(len(g1), 1)
            self.assertEqual(len(g2), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
