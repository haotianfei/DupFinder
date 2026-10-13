"""扫描安全基线测试：0 字节/软链接排除与跳过计数、--min-size 阈值、坏文件韧性、
单次 stat 快照下的行为等价、缓存条目格式兼容与零重算。

全部经 CLI 进程边界断言外部行为（退出码、控制台输出、文件系统终态、产物 JSON）。
"""

import hashlib
import json
import os
import re
import stat

import pytest


def _sha(data):
    return hashlib.sha256(data).hexdigest()


SUMMARY_RE = re.compile(
    r"Scan summary: skipped small/empty=(\d+) \(empty=(\d+)\), symlinks=(\d+), errors=(\d+)"
)


def _summary(console_output):
    m = SUMMARY_RE.search(console_output)
    assert m, f"未找到扫描汇总行: {console_output!r}"
    return tuple(int(g) for g in m.groups())  # (small/empty, empty, symlinks, errors)


def _console(proc):
    return proc.stdout + proc.stderr


_ACTION_MARKERS = ("Would delete", "Would move", "Would rename", "Deleted:", "Moved:", "Renamed:")


def _assert_never_in_action_lines(console_output, *substrings):
    """任何一条处理动作日志（演练或实删/实移）都不得提及这些路径特征。"""
    for line in console_output.splitlines():
        if any(marker in line for marker in _ACTION_MARKERS):
            for s in substrings:
                assert s not in line, f"处理动作波及被排除文件 {s!r}: {line}"


def _assert_protected_untouched(t, before):
    """0 字节、软链接及其目标、不可读文件在三种 action 下都必须终态零触碰。"""
    for rel in ("A/empty.bin", "B/empty.bin", "B/sym_link.txt", "B/sym_target.txt", "A/secret.bin"):
        t.assert_file_unchanged(rel, before)
    assert (t.workdir / "B/sym_link.txt").is_symlink(), "软链接被替换或删除"


def _add_small_pair(t):
    """两个 10 字节的跨目录重复文件（专门用于阈值测试）。"""
    content = b"0123456789"
    t.add_file("A/small1.bin", content)
    t.add_file("B/small2.bin", content)
    return content


# ---------------------------------------------------------------------------
# 0 字节 / 软链接：delete、move、--try-run 三种 action 下终态零触碰
# ---------------------------------------------------------------------------

def test_empty_and_symlink_untouched_under_try_run(full_tree):
    """--try-run：全场景树完整跑通（含不可读文件仅警告），空文件/软链接零触碰，
    汇总计数正确，结果 JSON 只含真实重复组，且动作日志从不提及被排除文件。"""
    t = full_tree
    before = t.snapshot()
    proc = t.run_scan()
    console = _console(proc)

    assert proc.returncode == 0, console
    assert _summary(console) == (2, 2, 1, 1)
    _assert_protected_untouched(t, before)
    t.assert_no_file_changed(before)  # 演练模式下整棵树都不应变化
    _assert_never_in_action_lines(console, "empty.bin", "sym_link.txt", "secret.bin")

    data, _ = t.load_result_json()
    assert set(data) == {_sha(t.cross_content), _sha(t.indir_content)}


def test_empty_and_symlink_untouched_under_delete(full_tree):
    """--action delete：重复组各实删一个文件，但 0 字节/软链接/不可读文件终态零触碰。"""
    t = full_tree
    before = t.snapshot()
    proc = t.run("-d", "A", "B", "--action", "delete")
    console = _console(proc)

    assert proc.returncode == 0, console
    assert _summary(console) == (2, 2, 1, 1)
    _assert_protected_untouched(t, before)
    _assert_never_in_action_lines(console, "empty.bin", "sym_link.txt", "secret.bin")

    # 改动范围严格限定在两对重复文件内，每组恰好剩一个
    changed = t.changed_paths(before)
    assert changed <= {"A/x_cross.txt", "B/x_cross.dup", "A/y_indir1.txt", "A/sub/y_indir2.txt"}
    for pair in ({"A/x_cross.txt", "B/x_cross.dup"}, {"A/y_indir1.txt", "A/sub/y_indir2.txt"}):
        remaining = [rel for rel in pair if (t.workdir / rel).exists()]
        assert len(remaining) == 1, f"删除组应恰好保留 1 个文件，实际: {remaining}"

    data, _ = t.load_result_json()
    assert set(data) == {_sha(t.cross_content), _sha(t.indir_content)}


def test_empty_and_symlink_untouched_under_move(full_tree):
    """--action move -m trash：重复组各实移一个文件到回收目录，
    但 0 字节/软链接/不可读文件终态零触碰，回收目录里也只有 2 个被移文件。"""
    t = full_tree
    t.mkdir("trash")
    before = t.snapshot()
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    console = _console(proc)

    assert proc.returncode == 0, console
    assert _summary(console) == (2, 2, 1, 1)
    _assert_protected_untouched(t, before)
    _assert_never_in_action_lines(console, "empty.bin", "sym_link.txt", "secret.bin")

    changed = t.changed_paths(before)
    assert changed <= {"A/x_cross.txt", "B/x_cross.dup", "A/y_indir1.txt", "A/sub/y_indir2.txt"}
    for pair in ({"A/x_cross.txt", "B/x_cross.dup"}, {"A/y_indir1.txt", "A/sub/y_indir2.txt"}):
        remaining = [rel for rel in pair if (t.workdir / rel).exists()]
        assert len(remaining) == 1, f"移动组应恰好保留 1 个文件，实际: {remaining}"

    moved_files = [p for p in (t.workdir / "trash").rglob("*") if p.is_file()]
    assert len(moved_files) == 2
    assert all(p.name not in ("empty.bin", "sym_link.txt", "secret.bin") for p in moved_files)


# ---------------------------------------------------------------------------
# --min-size 阈值
# ---------------------------------------------------------------------------

def test_min_size_excludes_small_duplicates_entirely(dup_tree_factory):
    """同一对 10 字节重复文件：默认阈值参与判定（成组），--min-size 50 时完全不参与（结果为空）。"""
    t1 = dup_tree_factory("default-threshold")
    content = _add_small_pair(t1)
    proc = t1.run_scan()
    assert proc.returncode == 0, _console(proc)
    assert _summary(_console(proc)) == (0, 0, 0, 0)
    data, _ = t1.load_result_json()
    assert set(data) == {_sha(content)}
    assert {e["path"] for e in data[_sha(content)]} == {"A/small1.bin", "B/small2.bin"}

    t2 = dup_tree_factory("raised-threshold")
    _add_small_pair(t2)
    proc = t2.run("-d", "A", "B", "--try-run", "--min-size", "50")
    assert proc.returncode == 0, _console(proc)
    assert _summary(_console(proc)) == (2, 0, 0, 0)
    data, _ = t2.load_result_json()
    assert data == {}


def test_min_size_help_documents_byte_semantics(dup_tree):
    """argparse 帮助文本写明 --min-size 以字节为单位、跳过语义与默认值。"""
    proc = dup_tree.run("--help")
    assert proc.returncode == 0
    # 折叠换行后检查，规避 argparse 自动 wrap 对文本的切断；
    # usage 行也含 --min-size，取最后一次匹配即选项说明区
    flat = " ".join(proc.stdout.split())
    idx = flat.rfind("--min-size")
    assert idx != -1, "help 缺少 --min-size"
    entry = flat[idx:idx + 400]
    assert "Minimum file size" in entry
    assert "bytes" in entry
    assert "skipped" in entry
    assert "default: 1" in entry


# ---------------------------------------------------------------------------
# 不可读文件：仅警告、整轮完成、其余处理完整
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root 下 chmod 000 不阻止读取")
def test_unreadable_file_warns_once_and_scan_completes(full_tree):
    """chmod 000 文件只产生一条警告；退出码正常；其余文件照常完成扫描与分组；
    坏文件不写入缓存。"""
    t = full_tree
    proc = t.run_scan()
    console = _console(proc)

    assert proc.returncode == 0, console
    warn_lines = [ln for ln in console.splitlines() if "A/secret.bin" in ln]
    assert len(warn_lines) == 1, f"不可读文件应只产生一条控制台记录，实际 {len(warn_lines)} 条: {warn_lines}"
    assert "WARNING" in warn_lines[0]
    assert _summary(console) == (2, 2, 1, 1)

    # 其余文件处理完整：两对真实重复都被完整识别
    data, _ = t.load_result_json()
    assert set(data) == {_sha(t.cross_content), _sha(t.indir_content)}

    # 坏文件不进缓存
    cache = json.loads(t.cache_path().read_text())
    assert "A/secret.bin" not in cache


# ---------------------------------------------------------------------------
# 单次 stat 快照：行为等价 + 缓存格式兼容 + 零重算
# ---------------------------------------------------------------------------

def _expected_candidate_groups(t, min_size=1):
    """在测试进程内独立复算：按当前既定语义（排除软链接、小于阈值、不可读文件，
    其余按内容 sha256 成组、组内 >=2 保留）应得到的 duplicates 结构。"""
    groups = {}
    for base in ("A", "B"):
        for p in sorted((t.workdir / base).rglob("*")):
            if p.is_symlink():
                continue
            try:
                st = p.stat()
                if not stat.S_ISREG(st.st_mode) or st.st_size < min_size:
                    continue
                with open(p, "rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
            except OSError:
                continue
            groups.setdefault(digest, []).append(
                (str(p.relative_to(t.workdir)), st.st_size, st.st_mtime)
            )
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def _project(data):
    return {k: sorted((e["path"], e["size"], e["modified_time"]) for e in v) for k, v in data.items()}


def test_single_stat_output_equivalence(full_tree):
    """单次 stat 重构后输出与既定语义完全等价：full_tree 的 duplicates JSON
    （路径/大小/mtime 逐字段）等于独立复算的期望值，mtime 直接取自磁盘 stat。"""
    t = full_tree
    before = t.snapshot()
    proc = t.run_scan()
    assert proc.returncode == 0, _console(proc)
    t.assert_no_file_changed(before)

    data, _ = t.load_result_json()
    assert _project(data) == _expected_candidate_groups(t)


def test_excluded_files_get_no_cache_entries_and_format_stays_compatible(full_tree):
    """扫描后缓存条目格式与既有文件完全一致（key=路径，value 三字段），
    0 字节/软链接/不可读文件零条目，其余条目值与磁盘实态逐字段吻合。"""
    t = full_tree
    proc = t.run_scan()
    assert proc.returncode == 0, _console(proc)

    cache = json.loads(t.cache_path().read_text())
    assert set(cache) == {
        "A/x_cross.txt", "B/x_cross.dup",
        "A/y_indir1.txt", "A/sub/y_indir2.txt",
        "A/unique_a.bin", "B/unique_b.bin",
        "B/sym_target.txt",
    }
    assert set(cache["A/x_cross.txt"]) == {"file_id", "modified_time", "size"}
    for rel, entry in cache.items():
        p = t.workdir / rel
        st = p.stat()
        assert entry["file_id"] == _sha(p.read_bytes())
        assert entry["size"] == st.st_size
        assert entry["modified_time"] == st.st_mtime


def test_warm_cache_reused_without_rehash(dup_tree_factory):
    """预热缓存（size/mtime 与磁盘一致、file_id 为哨兵值）起跑：扫描结果直接使用
    缓存中的哨兵 id 成组，运行后哨兵原样保留——证明命中条目零重算，
    且单次 stat 快照的比对语义正确。"""
    t = dup_tree_factory("poisoned-warm")
    t.build_minimal()
    t.prewarm_cache()
    cache = json.loads(t.cache_path().read_text())
    sentinel = "f" * 64
    for entry in cache.values():
        entry["file_id"] = sentinel
    t.cache_path().write_text(json.dumps(cache))

    proc = t.run_scan()
    assert proc.returncode == 0, _console(proc)

    data, _ = t.load_result_json()
    assert list(data) == [sentinel], "预热条目未被直接复用（哨兵 id 没有出现在结果 key）"
    assert {e["path"] for e in data[sentinel]} == {"A/x_cross.txt", "B/x_cross.dup"}

    after = json.loads(t.cache_path().read_text())
    assert all(v["file_id"] == sentinel for v in after.values()), "缓存条目被重算覆盖"
