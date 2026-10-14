"""缓存批量写策略与演练模式零正式产物的测试。

覆盖：批量阈值默认 2000 下的落盘次数、原子替换（缓存文件任何时刻都是完整 JSON）、
SIGINT/SIGTERM 中断时在锁内受控落盘一次并以非零码干净退出、中断后续跑结果与一次
跑到底完全一致（缓存正确续用）、--try-run 对正式缓存/结果文件零污染（冷启动与
预热起跑两态）。

全部经 CLI 进程边界断言外部行为。
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from conftest import console_output


# try-run 允许出现在 workdir 的新增条目：日志目录、缓存锁文件（FileLock 运行期
# 基础设施）、子进程导入脚本副本产生的字节码缓存——三者都不是数据类正式产物
ALLOWED_NEW_ENTRIES = {"logs", "file_cache.json.lock", "__pycache__"}


def _add_pair_files(t, n_pairs, size=4096):
    """造 n_pairs 对跨目录重复文件（每对内容互不相同），共 2*n_pairs 个文件。

    同名相对路径在孪生树中内容逐字节一致，便于做"两棵树跑法不同、结果应相同"的比对。
    """
    for i in range(n_pairs):
        prefix = (f"pair-{i:06d}-".encode() + b".")
        content = (prefix * (size // len(prefix) + 1))[:size]
        t.add_file(f"A/f{i:05d}.bin", content)
        t.add_file(f"B/f{i:05d}.bin", content)
    return 2 * n_pairs


# ---------------------------------------------------------------------------
# 落盘次数观测：1ms 轮询缓存文件 st_mtime_ns，统计"出现新值"的次数
# ---------------------------------------------------------------------------

class _CacheWriteSampler(threading.Thread):
    """轮询缓存文件的 mtime_ns，记录出现过的不同值（=观测到的落盘次数下界）。

    每次原子替换都会刷新 mtime_ns；采样只能漏计写（两次写落在同一采样间隔），
    不会重复计数。因此"少写"方向做上界断言、"多写"方向做带大余量的下界断言，
    两头都稳定。
    """

    def __init__(self, cache_path, interval=0.001):
        super().__init__(daemon=True)
        self.cache_path = str(cache_path)
        self.interval = interval
        self.changes = []
        self._halt = threading.Event()

    def run(self):
        last = None
        while not self._halt.is_set():
            ns = self._peek()
            if ns != last:
                if ns is not None:
                    self.changes.append(ns)
                last = ns
            time.sleep(self.interval)

    def _peek(self):
        try:
            return os.stat(self.cache_path).st_mtime_ns
        except OSError:
            return None

    def stop(self):
        self._halt.set()
        self.join(timeout=5)
        # 最后一次替换可能紧贴进程退出发生，收尾时补采一次
        ns = self._peek()
        if ns is not None and ns not in self.changes:
            self.changes.append(ns)


def _run_with_write_sampling(t, args):
    sampler = _CacheWriteSampler(t.cache_path())
    sampler.start()
    try:
        proc = t.run(*args)
    finally:
        sampler.stop()
    return proc, sampler.changes


def test_default_cache_batch_writes_cache_only_a_few_times(dup_tree):
    """2500 个文件、默认阈值 2000：观测到的缓存落盘次数 ≤3（理论 2 次），缓存完整无 .tmp 残留。"""
    total = _add_pair_files(dup_tree, 1250)
    proc, changes = _run_with_write_sampling(dup_tree, ("-d", "A", "B"))
    console = console_output(proc)
    assert proc.returncode == 0, console
    assert len(changes) <= 3, f"默认批量阈值下落盘次数异常: {len(changes)}"
    cache = json.loads(dup_tree.cache_path().read_text())
    assert len(cache) == total
    assert list(dup_tree.workdir.glob("*.tmp")) == []
    assert "Saved file_dict to duplicates_" in console


def test_small_cache_batch_writes_cache_far_more_often(dup_tree):
    """对照组：同一量级树 + --cache-batch 10 时落盘次数 ≥10（1000 文件理论 100 次，余量充分）。"""
    total = _add_pair_files(dup_tree, 500)
    proc, changes = _run_with_write_sampling(dup_tree, ("-d", "A", "B", "--cache-batch", "10"))
    assert proc.returncode == 0, console_output(proc)
    assert len(changes) >= 10, f"小批量阈值下落盘次数应远多于默认值，观测: {len(changes)}"
    cache = json.loads(dup_tree.cache_path().read_text())
    assert len(cache) == total
    assert list(dup_tree.workdir.glob("*.tmp")) == []


def test_help_documents_cache_batch_flag_and_default(dup_tree):
    """argparse 帮助文本含 --cache-batch 与默认值 2000 的中性描述。"""
    proc = dup_tree.run("--help")
    assert proc.returncode == 0
    flat = " ".join(proc.stdout.split())
    idx = flat.rfind("--cache-batch")
    assert idx != -1, "help 缺少 --cache-batch"
    entry = flat[idx:idx + 400]
    assert "cache" in entry
    assert "batch" in entry.lower()
    assert "default: 2000" in entry


# ---------------------------------------------------------------------------
# 中断：SIGINT/SIGTERM 受控落盘、非零退出、无 traceback
# ---------------------------------------------------------------------------

# 中断路径的计数快照行（与正常 Scan summary 同字段结构，前缀多出 dirs scanned）
INTERRUPTED_SUMMARY_RE = re.compile(
    r"Scan interrupted after (\d+) dirs scanned: "
    r"skipped small/empty=(\d+) \(empty=(\d+)\), symlinks=(\d+), errors=(\d+)"
)

def _interrupted_run(t, sig, extra_args=()):
    """真实扫描模式启动子进程，等阶段二的缓存首批批量写出现后再发 `sig`。

    两阶段扫描下阶段一（纯 stat 收集）不发生任何缓存落盘，缓存文件首次出现即
    "已进入哈希阶段、首批已落盘、后续大批量仍在进行"的硬证据；此刻发信号保证
    中断落在阶段二进行中，既不会早到无条目可落盘，也不会迟至进程收尾。
    返回 (退出码, 收集到的 stderr 文本)。
    """
    proc = subprocess.Popen(
        [sys.executable, str(t.workdir / "dup_finder.py"), "-d", "A", "B", *extra_args],
        cwd=str(t.workdir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    lines = []
    started = False
    signalled = False
    deadline = time.monotonic() + 60
    assert proc.stderr is not None  # stderr=PIPE
    while True:
        line = proc.stderr.readline()
        if not line:
            break
        lines.append(line)
        if "Processing directory" in line:
            started = True
        if started and not signalled:
            # 等首批缓存落盘出现（= 哈希阶段过半程证据），轮询而非固定 sleep，机器快慢自适应
            while (not t.cache_path().exists() and proc.poll() is None
                   and time.monotonic() < deadline):
                time.sleep(0.002)
            time.sleep(0.01)
            if proc.poll() is None:
                proc.send_signal(sig)
            signalled = True
    rc = proc.wait(timeout=120)
    return rc, "".join(lines)


def _assert_interruption_contract(t, sig, total):
    rc, console = _interrupted_run(t, sig)
    assert rc != 0, f"被信号中断后应以非零码退出，实际 rc={rc}"
    assert "Traceback" not in console, f"中断不应抛 traceback:\n{console}"
    assert console.count("Scan interrupted by signal") == 1, "应恰好一条中断 INFO"

    # 中断路径同样给出跳过计数快照（与正常路径的 Scan summary 同字段结构）
    m = INTERRUPTED_SUMMARY_RE.search(console)
    assert m, f"中断时应有计数快照行:\n{console}"
    assert int(m.group(1)) >= 1, "被中断时至少已进入过一个目录"

    # 已完成部分不丢：缓存文件存在且能整体解析——原子替换保证它是完整快照，无半写状态
    assert t.cache_path().exists(), "中断后未落盘任何缓存，已处理条目丢失"
    cache = json.loads(t.cache_path().read_text())
    assert 0 < len(cache) < total, f"中断时缓存应为部分但非空的完整快照，实际 {len(cache)}/{total}"
    for entry in cache.values():
        assert set(entry) == {"file_id", "modified_time", "size"}

    # 中断发生在扫描阶段：不产出结果文件，也不执行任何文件动作
    assert list(t.workdir.glob("duplicates_*.json")) == []

    # .tmp 残留只可能是写中途被杀留下的孤儿文件；正式缓存的完整性已由上面的解析证明
    for orphan in t.workdir.glob("*.tmp"):
        assert orphan.name.startswith(t.cache_path().name + ".")
    return rc, console


def test_sigint_flushes_partial_cache_once_and_exits_nonzero(dup_tree):
    """SIGINT 落在哈希阶段中段（首批批量写已落盘、后续批次仍在进行）：锁内受控
    补盘一次、非零退出码、无 traceback、缓存为已处理条目的完整 JSON 快照、
    不产出结果文件、不触碰任何源文件。"""
    t = dup_tree
    total = _add_pair_files(t, 2000)  # 4000 文件
    before = t.snapshot()
    _assert_interruption_contract(t, signal.SIGINT, total)
    t.assert_no_file_changed(before)


def test_sigterm_interruption_follows_same_contract(dup_tree):
    """SIGTERM 与 SIGINT 走同一中断落盘与干净退出契约。"""
    t = dup_tree
    total = _add_pair_files(t, 2000)
    before = t.snapshot()
    _assert_interruption_contract(t, signal.SIGTERM, total)
    t.assert_no_file_changed(before)


def test_rerun_after_interrupt_matches_clean_run(dup_tree_factory):
    """中断后再跑一次完整扫描：最终缓存与结果和一次跑到底的孪生树完全一致；
    中断前已落盘的条目在后继运行中原值复用、未被重算覆盖。"""
    t_i = dup_tree_factory("interrupted")
    t_c = dup_tree_factory("clean")
    n_i = _add_pair_files(t_i, 2000)
    n_c = _add_pair_files(t_c, 2000)
    assert n_i == n_c

    rc, console = _interrupted_run(t_i, signal.SIGINT)
    assert rc != 0 and "Scan interrupted" in console
    partial = json.loads(t_i.cache_path().read_text())
    assert 0 < len(partial) < n_i

    proc = t_i.run("-d", "A", "B")   # 在中断残留缓存上续跑，完整扫完
    assert proc.returncode == 0, console_output(proc)
    proc2 = t_c.run("-d", "A", "B")  # 孪生树一次跑到底
    assert proc2.returncode == 0, console_output(proc2)

    full_i = json.loads(t_i.cache_path().read_text())
    full_c = json.loads(t_c.cache_path().read_text())
    assert len(full_i) == n_i
    assert {k: v["file_id"] for k, v in full_i.items()} == \
           {k: v["file_id"] for k, v in full_c.items()}
    for rel, entry in partial.items():
        assert full_i[rel]["file_id"] == entry["file_id"], \
            f"中断前条目 {rel} 在续跑中被重算覆盖"

    def project(data):
        return {k: sorted((e["path"], e["size"]) for e in v) for k, v in data.items()}

    d_i, _ = t_i.load_result_json()
    d_c, _ = t_c.load_result_json()
    assert project(d_i) == project(d_c) and project(d_i)


# ---------------------------------------------------------------------------
# 演练模式：正式产物零污染（冷启动与预热起跑两态）
# ---------------------------------------------------------------------------

def test_try_run_cold_start_writes_no_formal_artifacts(dup_tree):
    """冷启动 try-run：不写缓存、不写结果 JSON、无 .tmp 残留；新增目录项仅限
    日志目录与锁文件；演练报告与汇总照常输出，源树零变化。"""
    t = dup_tree
    t.build_full_scenarios()
    before = t.snapshot()
    names_before = sorted(os.listdir(t.workdir))

    proc = t.run_scan()
    console = console_output(proc)
    assert proc.returncode == 0, console
    assert "[TRY RUN]" in console
    assert "no cache or result file written" in console
    assert "Scan summary" in console

    t.assert_no_file_changed(before)
    assert not t.cache_path().exists()
    assert list(t.workdir.glob("duplicates_*.json")) == []
    assert list(t.workdir.glob("*.tmp")) == []
    new_names = set(os.listdir(t.workdir)) - set(names_before)
    assert new_names <= ALLOWED_NEW_ENTRIES, f"try-run 多出意外条目: {new_names}"


def test_try_run_leaves_prewarmed_cache_byte_identical(dup_tree):
    """预热起跑 try-run：缓存可正常读复用，但文件逐字节不变；无结果 JSON、无 .tmp；
    演练动作与汇总报告正常。"""
    t = dup_tree
    t.build_minimal()
    t.prewarm_cache()
    cache_bytes = t.cache_path().read_bytes()
    names_before = sorted(os.listdir(t.workdir))
    before = t.snapshot()

    proc = t.run_scan()
    console = console_output(proc)
    assert proc.returncode == 0, console

    assert t.cache_path().read_bytes() == cache_bytes, "try-run 改写了正式缓存"
    assert list(t.workdir.glob("duplicates_*.json")) == []
    assert list(t.workdir.glob("*.tmp")) == []
    assert set(os.listdir(t.workdir)) - set(names_before) <= ALLOWED_NEW_ENTRIES
    t.assert_no_file_changed(before)
    assert "[TRY RUN]" in console
    assert "Scan summary" in console


def test_orphan_tmp_file_does_not_affect_a_real_run(dup_tree):
    """缓存旁路的同名模式孤儿 .tmp（上次进程被硬杀留下的残留）不影响本次运行：
    真实扫描照常完成、正式缓存仍是完整合法 JSON，且新写入只经原子替换完成。"""
    t = dup_tree
    t.build_minimal()
    orphan = t.workdir / (t.cache_path().name + ".orphan.tmp")
    orphan.write_text('{"stale": {"file_id": "not-a-hash", "modified_time": 0, "size": 0}}')

    proc = t.run("-d", "A", "B")
    assert proc.returncode == 0, console_output(proc)
    cache = json.loads(t.cache_path().read_text())
    assert set(cache) == {"A/x_cross.txt", "B/x_cross.dup"}
    assert all(len(v["file_id"]) == 64 for v in cache.values())
    # 结果文件照常产出且结构完整
    data, _ = t.load_result_json()
    assert len(data) == 1 and sum(len(v) for v in data.values()) == 2
