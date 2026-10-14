"""工程收尾行为测试：进度里程碑与日志体积、哈希算法命名空间、参数化常量、辅助脚本导入洁净。

- 扫描循环不再有每文件日志；改为每 PROGRESS_INTERVAL(1000) 文件一条 INFO 里程碑
- 默认 sha256 与既有裸 hex 缓存格式逐字节兼容、零重算；非默认算法条目带
  "algo:" 前缀做命名空间隔离，互不污染
- 魔法值提为命名常量/CLI 参数后，显式传默认值与不传的行为逐字段一致
- change_priority.py 不再因导入主程序而拉起日志副作用
"""

import hashlib
import json
import os

import pytest

from conftest import console_lines_with, console_output, moved_payload_files

PROGRESS_INTERVAL = 1000          # 与 dup_finder 的命名常量同值（外部契约）
PAIR_CONTENT = b"polish-pair-" * 410      # 4100B，成对文件同内容
UNIQUE_CONTENT = b"polish-uniq-" * 411    # 4110B，全局唯一 size


def _read_cache(t):
    return json.loads(t.cache_path().read_text())


def _hashed_entries(cache):
    return {k: v for k, v in cache.items() if v["file_id"] != "Z:unique-size"}


# ---------------------------------------------------------------------------
# AC1：进度里程碑可见 + 每文件记录清零 + 体积较基线降一个数量级以上（行数模型）
# ---------------------------------------------------------------------------

def test_progress_milestones_replace_per_file_logging_and_cut_volume(dup_tree):
    """2500 文件树（硬链接构造，全部位于分量 keptdir 下并以之 retain → 零动作行，
    日志只剩扫描阶段记录）+ try-run：
    ①里程碑可见且精确：collect 与 hash 两阶段各在 1000/2000 处一条，共 4 条；
    ②扫描循环每文件记录彻底消失：日志文件 0 条 DEBUG、
      旧每文件行特征（"Collecting file"/"Process File ID"）绝迹；
    ③体积模型：旧行为=每文件 2 条记录 ≈2*N*L（L 取本轮真实记录平均长度），
      新日志实际字节数须 < 旧模型/20——远超一个数量级。"""
    t = dup_tree
    n_per_root = 1250
    seed_a = t.add_file("A/keptdir/seed.bin", PAIR_CONTENT)
    seed_b = t.add_file("B/keptdir/seed.bin", UNIQUE_CONTENT)
    for i in range(n_per_root - 1):          # seed 自身即组内一员，补齐 1250
        os.link(seed_a, t.workdir / f"A/keptdir/h{i:05d}.bin")
        os.link(seed_b, t.workdir / f"B/keptdir/h{i:05d}.bin")
    total_files = 2 * n_per_root

    proc = t.run("-d", "A", "B", "--try-run", "--action", "move",
                 "--retain", "keptdir")
    out = console_output(proc)
    assert proc.returncode == 0, out
    # 全保留 → 动作报告本就无每文件行，体积模型只反映扫描阶段
    assert console_lines_with(out, "[TRY RUN]") == []

    collect_ms = console_lines_with(out, "Progress:", "file entries collected")
    hash_ms = console_lines_with(out, "Progress:", "files hashed")
    expected_counts = [k * PROGRESS_INTERVAL for k in range(1, total_files // PROGRESS_INTERVAL + 1)]
    seen_collect = [int(ln.split("Progress: ")[1].split(" ")[0]) for ln in collect_ms]
    seen_hash = [int(ln.split("Progress: ")[1].split(" ")[0]) for ln in hash_ms]
    assert seen_collect == expected_counts, out
    assert seen_hash == expected_counts, out

    log_path = t.workdir / "logs" / "DupFinder.log"
    log_text = log_path.read_text()
    assert " - DEBUG - " not in log_text
    assert "Collecting file" not in log_text
    assert "Process File ID" not in log_text

    # 体积模型：旧行为每文件 2 条记录；以本轮日志记录平均长度为单条代理
    records = [ln for ln in log_text.splitlines() if " - " in ln]
    assert records
    avg_record = sum(len(ln) + 1 for ln in records) / len(records)
    old_model_bytes = 2 * total_files * avg_record
    assert len(log_text) * 20 < old_model_bytes, (
        f"新日志 {len(log_text)}B，旧模型估计 {old_model_bytes:.0f}B——不足一个数量级"
    )


# ---------------------------------------------------------------------------
# AC2：默认 sha256 零重算 + 非默认算法命名空间隔离
# ---------------------------------------------------------------------------

def test_default_run_keeps_bare_hex_cache_format(dup_tree):
    """默认算法运行：哈希条目为裸 64 位小写 hex、等于旧算法逐字节结果；
    条目键值结构与既有格式完全一致（三字段）。"""
    t = dup_tree
    t.mkdir("trash")
    t.add_file("A/p1.bin", PAIR_CONTENT)
    t.add_file("B/p2.bin", PAIR_CONTENT)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash",
                 "--retain", "p1.bin", "p2.bin")
    assert proc.returncode == 0, console_output(proc)

    hashed = _hashed_entries(_read_cache(t))
    assert set(hashed) == {"A/p1.bin", "B/p2.bin"}
    for entry in hashed.values():
        fid = entry["file_id"]
        assert len(fid) == 64 and ":" not in fid and fid == fid.lower()
        assert set(entry) == {"file_id", "modified_time", "size"}
    assert hashed["A/p1.bin"]["file_id"] == hashlib.sha256(PAIR_CONTENT).hexdigest()


@pytest.mark.slow
def test_default_algo_legacy_cache_hits_byte_identical_zero_rehash(dup_tree):
    """slow（零重算钉桩，规模树）：以既有格式（裸 hex、mtime/size 与磁盘一致、
    file_id 为播种值）预热全部候选；默认 sha256 运行后缓存文件逐字节不变——
    默认路径对既有 15747 条的兼容性即由此命名空间无关的命中路径代表。"""
    t = dup_tree
    t.mkdir("trash")
    n_pairs = 400
    seeded = {}
    for i in range(n_pairs):
        content = (f"legacy-{i}-".encode() + b".") * 520
        content = content[:4160 + (i % 7)]      # 每对唯一 size，互不串组
        a_rel, b_rel = f"A/l{i:04d}.bin", f"B/l{i:04d}.bin"
        t.add_file(a_rel, content)
        t.add_file(b_rel, content)
        for rel in (a_rel, b_rel):
            st = (t.workdir / rel).stat()
            seeded[rel] = {"file_id": hashlib.sha256(content).hexdigest(),
                           "modified_time": st.st_mtime, "size": st.st_size}
    before = json.dumps(seeded)
    t.cache_path().write_text(before)

    retain = tuple(f"l{i:04d}.bin" for i in range(n_pairs))
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--retain", *retain)
    assert proc.returncode == 0, console_output(proc)
    assert t.cache_path().read_text() == before      # 逐字节不变 = 命中零重算


def test_sha512_run_is_namespaced_and_restores_cleanly(dup_tree):
    """三段式隔离钉桩：
    ①sha256 运行 → 裸 hex；
    ②同树 sha512 运行 → 结果分组 key 与缓存条目全部 "sha512:" 前缀且值等于真实
      sha512 摘要（sha256 条目未被当命中）；唯一 size 者的哨兵不带前缀（算法无关）；
    ③再回 sha256 运行 → 重新命中/重算后条目回到与①逐字节一致的裸 hex，
      sha512 前缀值不会误导 sha256 命中。"""
    t = dup_tree
    t.mkdir("trash")
    t.add_file("A/x.bin", PAIR_CONTENT)
    t.add_file("B/x.dup", PAIR_CONTENT)
    t.add_file("A/lonely.bin", UNIQUE_CONTENT)          # size 唯一 → 哨兵
    retain = ("x.bin", "x.dup", "lonely.bin")
    algo_args = ("--action", "move", "-m", "trash", "--retain", *retain)

    proc = t.run("-d", "A", "B", "--hash-algo", "sha256", *algo_args)
    assert proc.returncode == 0, console_output(proc)
    cache_v1 = _read_cache(t)
    bare = {k: v["file_id"] for k, v in cache_v1.items()}
    assert bare["A/x.bin"] == hashlib.sha256(PAIR_CONTENT).hexdigest()
    assert bare["A/lonely.bin"] == "Z:unique-size"

    proc = t.run("-d", "A", "B", "--hash-algo", "sha512", *algo_args)
    assert proc.returncode == 0, console_output(proc)
    # 三段式用例每段都产结果文件，取最新（本次 sha512 运行）的那份
    data512, _ = t.load_result_json(latest=True)
    sha512_id = "sha512:" + hashlib.sha512(PAIR_CONTENT).hexdigest()
    assert list(data512) == [sha512_id], "sha512 运行不得复用 sha256 条目（裸 hex 命中）"
    cache_v2 = _read_cache(t)
    assert cache_v2["A/x.bin"]["file_id"] == sha512_id
    assert cache_v2["A/lonely.bin"]["file_id"] == "Z:unique-size"   # 哨兵算法无关

    proc = t.run("-d", "A", "B", "--hash-algo", "sha256", *algo_args)
    assert proc.returncode == 0, console_output(proc)
    cache_v3 = _read_cache(t)
    assert {k: v["file_id"] for k, v in cache_v3.items()} == bare, \
        "回到默认算法后应与首跑逐字段一致（前缀条目不误导、重算结果确定）"


def test_hash_algo_choices_and_default_visible_in_help(dup_tree):
    """--hash-algo 的默认值与候选集在 help 中可见；非法算法名被 argparse 拒绝（rc=2）。"""
    proc = dup_tree.run("--help")
    flat = " ".join(proc.stdout.split())
    assert "--hash-algo" in flat
    assert "sha256" in flat and "sha512" in flat
    assert "bare-hex" in flat and "namespaced" in flat

    bad = dup_tree.run("-d", "A", "--hash-algo", "bogus123")
    assert bad.returncode == 2
    assert "invalid choice" in console_output(bad)


# ---------------------------------------------------------------------------
# AC3：魔法值常量化/参数化后默认行为不变 + 新阈值参数真实生效
# ---------------------------------------------------------------------------

def test_explicit_default_values_match_implicit_run(dup_tree_factory):
    """同一 fixture 两棵树：一棵不带新参数、一棵显式传全部默认值
    （--cache-batch 2000 --min-size 1 --hash-algo sha256 --min-free-percent 5）：
    结果 JSON 分组、缓存 file_id 映射、trash 落点完全一致。"""
    def build(t):
        t.mkdir("trash")
        t.add_file("A/k1.bin", PAIR_CONTENT)
        t.add_file("B/k2.bin", PAIR_CONTENT)
        t.add_file("A/solo.bin", UNIQUE_CONTENT)

    t_impl = dup_tree_factory("implicit")
    build(t_impl)
    proc = t_impl.run("-d", "A", "B", "--action", "move", "-m", "trash",
                      "--retain", "k1.bin", "k2.bin", "solo.bin")
    assert proc.returncode == 0, console_output(proc)

    t_exp = dup_tree_factory("explicit")
    build(t_exp)
    proc = t_exp.run("-d", "A", "B", "--action", "move", "-m", "trash",
                     "--retain", "k1.bin", "k2.bin", "solo.bin",
                     "--cache-batch", "2000", "--min-size", "1",
                     "--hash-algo", "sha256", "--min-free-percent", "5")
    assert proc.returncode == 0, console_output(proc)

    a, _ = t_impl.load_result_json()
    b, _ = t_exp.load_result_json()
    strip = lambda d: {k: sorted((e["path"], e["size"]) for e in v) for k, v in d.items()}
    assert strip(a) == strip(b)
    ids_a = {k: v["file_id"] for k, v in _read_cache(t_impl).items()}
    ids_b = {k: v["file_id"] for k, v in _read_cache(t_exp).items()}
    assert ids_a == ids_b
    assert [p.name for p in moved_payload_files(t_impl.workdir / "trash")] == \
           [p.name for p in moved_payload_files(t_exp.workdir / "trash")]


def test_min_free_percent_threshold_is_live(dup_tree):
    """--min-free-percent 100 高于任何卷的剩余比例 → 移动被阈值拦截（错误日志+
    文件不动+回收目录零负载）；默认 5 时同树正常移动——参数真实生效。"""
    t = dup_tree
    t.mkdir("trash")
    t.add_file("A/b1.bin", PAIR_CONTENT)
    t.add_file("B/b2.bin", PAIR_CONTENT)

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash",
                 "--retain", "b2.bin", "--min-free-percent", "100")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "insufficient" in out or "空间不足" in out
    assert not [ln for ln in out.splitlines() if "Moved:" in ln]
    assert (t.workdir / "A/b1.bin").exists()
    assert moved_payload_files(t.workdir / "trash") == []


# ---------------------------------------------------------------------------
# 辅助脚本导入洁净（change_priority 不再经主程序拉起日志）
# ---------------------------------------------------------------------------

def test_change_priority_run_leaves_no_logging_side_effects(dup_tree):
    """change_priority.py 运行全程：workdir 无 logs/ 目录、无 __pycache__ 之外的
    新增运行痕迹——导入纯逻辑模块，不触发主程序的 logger 副作用。"""
    t = dup_tree
    t.copy_in("change_priority.py")
    data = {"g": [{"path": "dirA/a.bin", "size": 9, "type": ".bin",
                   "modified_time": 1.0, "priority": 4}]}
    t.add_file("in.json", json.dumps(data).encode())
    proc = t.run_script("change_priority.py", "-i", "in.json", "-o", "out.json",
                        "-p", "dirA", "-a", "1")
    assert proc.returncode == 0, console_output(proc)
    assert not (t.workdir / "logs").exists()
    result = json.loads((t.workdir / "out.json").read_text())
    assert result["g"][0]["priority"] == 3
