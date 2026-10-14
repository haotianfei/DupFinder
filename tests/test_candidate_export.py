"""执行前审查：--export-candidates 待处理清单导出与同步目录标记警示。

清单：一行一个待处理文件的绝对源路径，与 retain_files 实际选中处理的队列同序同集合
（cross-dirs 过滤与 retain/优先级选择之后）；delete/move/try-run 均可导出。
导出文件是用户显式要求的产物——try-run 下写它不违反"演练零正式产物"红线
（缓存/结果 JSON/移动清单在 try-run 下照旧零写入）。
警示：扫描根直接子项含 .stfolder / .sync 前缀的同步目录标记时，一条 WARNING、
不阻塞；更深位置的标记与无标记根不警示。

全部经 CLI 进程边界断言外部行为。

本套件的重复对全部是异名（xover.txt/xover-copy.txt、i1/i2、j1/j2），断言语义依赖
"异名候选被处理"，故逐条显式传 --no-gate-renamed 保持原断言（gate 本体行为由
tests/test_renamed_gate.py 覆盖）。
"""

import json
import os
import re

import pytest

from conftest import console_output, is_move_manifest, moved_payload_files

X_CONTENT = b"cross" * 500          # 2500B 跨根对
A_CONTENT = b"inta-" * 500          # 2000B A 根内部对
B_CONTENT = b"intb-" * 600          # 2400B B 根内部对

CROSS_PAIR = ("A/xover.txt", "B/xover-copy.txt")
A_INTERNAL = ("A/i1.txt", "A/i2.txt")
B_INTERNAL = ("B/deep/j1.txt", "B/deep/j2.txt")


def _build_three_groups(t):
    """双根三重复组：一对跨根重复 + A 内部对 + B 内部对。"""
    a1, a2 = CROSS_PAIR
    t.add_file(a1, X_CONTENT)
    t.add_file(a2, X_CONTENT)
    i1, i2 = A_INTERNAL
    t.add_file(i1, A_CONTENT)
    t.add_file(i2, A_CONTENT)
    j1, j2 = B_INTERNAL
    t.add_file(j1, B_CONTENT)
    t.add_file(j2, B_CONTENT)


def _build_cross_pair_only(t):
    a1, a2 = CROSS_PAIR
    t.add_file(a1, X_CONTENT)
    t.add_file(a2, X_CONTENT)


def _read_export(t, rel):
    lines = (t.workdir / rel).read_text().splitlines()
    assert all(l.strip() == l and l for l in lines), lines   # 每行一条干净路径，无空行
    return lines


def _extract(out, pattern):
    return [m.group(1) for m in re.finditer(pattern, out)]


def _moved_sources(out):
    return _extract(out, r"Moved: (\S+) to ")


def _deleted_sources(out):
    return _extract(out, r"Deleted: (\S+)")


def _would_rename_sources(out):
    return _extract(out, r"Would rename: (\S+) to ")


def _manifest_srcs(t):
    manifests = [p for p in (t.workdir / "trash").iterdir() if is_move_manifest(p)]
    assert len(manifests) == 1
    return [json.loads(ln)["src"]
            for ln in manifests[0].read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# ①move：导出清单与实际执行队列逐行一致（并三方对照 manifest/trash 落点）
# ---------------------------------------------------------------------------

def test_exported_candidates_match_actual_move_queue_line_by_line(dup_tree):
    """--export-candidates review/list.txt（父目录原本不存在）：导出 3 行，
    顺序与内容和 Moved 执行日志完全一致；与移动 manifest 的 src 序列、trash
    负载三方吻合；每组恰处理一个成员、保留侧原地不动。"""
    t = dup_tree
    t.mkdir("trash")
    _build_three_groups(t)
    assert not (t.workdir / "review").exists()          # 导出父目录不存在 → 自动创建

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash",
                 "--export-candidates", "review/list.txt", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    lines = _read_export(t, "review/list.txt")
    assert (t.workdir / "review" / "list.txt").exists()
    assert len(lines) == 3
    assert all(os.path.isabs(p) for p in lines)

    # 逐行一致：导出顺序 == 实际移动顺序（同序同集合）
    assert lines == [os.path.abspath(t.workdir / s) for s in _moved_sources(out)]
    # 与移动清单、trash 落点三方一致
    assert lines == _manifest_srcs(t)
    assert sorted(p.name for p in moved_payload_files(t.workdir / "trash")) == \
           sorted(os.path.basename(p) for p in lines)

    # 每个重复组恰好处理一个成员：被处理者与保留者互补
    for pair in (CROSS_PAIR, A_INTERNAL, B_INTERNAL):
        processed = [r for r in pair if os.path.abspath(t.workdir / r) in lines]
        assert len(processed) == 1, pair
        keeper = [r for r in pair if r not in processed][0]
        assert (t.workdir / keeper).exists()
        assert not (t.workdir / processed[0]).exists()

    assert "Candidate list exported: review/list.txt (3 file(s))" in out


# ---------------------------------------------------------------------------
# ②try-run + 导出：清单照写，正式产物与目标树红线延续
# ---------------------------------------------------------------------------

def test_try_run_with_export_writes_list_and_touches_nothing(dup_tree):
    """try-run 叠加导出：review 清单正常写出且与演练动作行逐行一致；
    目标树 snapshot 逐字段零变化；缓存/结果 JSON/移动清单/.tmp 零产物
    （导出文件属用户显式要求的审查产物，与红线不冲突）。"""
    t = dup_tree
    _build_three_groups(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--try-run",
                 "--export-candidates", "review/candidates.txt", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    lines = _read_export(t, "review/candidates.txt")
    assert len(lines) == 3
    assert lines == [os.path.abspath(t.workdir / s) for s in _would_rename_sources(out)]
    assert "Candidate list exported: review/candidates.txt (3 file(s))" in out

    # 演练零触碰 + 零正式产物
    assert t.snapshot() == before
    assert not t.cache_path().exists()
    assert list(t.workdir.glob("duplicates_*.json")) == []
    assert list(t.workdir.rglob("moves_*.jsonl")) == []
    assert list(t.workdir.glob("*.tmp")) == []
    assert "Moved:" not in out and "Deleted:" not in out


# ---------------------------------------------------------------------------
# ③delete：导出与实删序列一致
# ---------------------------------------------------------------------------

def test_exported_candidates_match_delete_queue(dup_tree):
    """delete 动作同样可导清单：导出逐行等于 Deleted: 执行序列；每组恰删一个
    成员、保留侧内容原样。"""
    t = dup_tree
    _build_three_groups(t)
    keep_bytes = {rel: (t.workdir / rel).read_bytes()
                  for pair in (CROSS_PAIR, A_INTERNAL, B_INTERNAL) for rel in pair}

    proc = t.run("-d", "A", "B", "--action", "delete",
                 "--export-candidates", "del_list.txt", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    lines = _read_export(t, "del_list.txt")
    assert lines == [os.path.abspath(t.workdir / s) for s in _deleted_sources(out)]
    assert len(lines) == 3

    for pair in (CROSS_PAIR, A_INTERNAL, B_INTERNAL):
        gone = [r for r in pair if os.path.abspath(t.workdir / r) in lines]
        assert len(gone) == 1, pair
        keeper = [r for r in pair if r not in gone][0]
        assert not (t.workdir / gone[0]).exists()
        assert (t.workdir / keeper).read_bytes() == keep_bytes[keeper]


# ---------------------------------------------------------------------------
# ④与 --cross-dirs-only 组合：清单只含跨根组成员（先过滤后导出）
# ---------------------------------------------------------------------------

def test_export_after_cross_dirs_filter_contains_only_cross_root_queue(dup_tree):
    """开启 cross-dirs 后导出：整组跳过的两个同根内部组不在清单；跨根组恰一个
    成员在清单中（组过滤发生在导出之前）。"""
    t = dup_tree
    _build_three_groups(t)

    proc = t.run("-d", "A", "B", "--try-run", "--cross-dirs-only",
                 "--export-candidates", "cross.txt", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    lines = _read_export(t, "cross.txt")
    assert len(lines) == 1
    assert os.path.basename(lines[0]) in ("xover.txt", "xover-copy.txt")
    forbidden = {os.path.abspath(t.workdir / r)
                 for pair in (A_INTERNAL, B_INTERNAL) for r in pair}
    assert not set(lines) & forbidden
    assert "Cross-dirs-only: skipped 2 same-root duplicate group(s)" in out
    assert "Candidate list exported: cross.txt (1 file(s))" in out


# ---------------------------------------------------------------------------
# ⑤回归钉桩：不带导出参数时产物集合与现状一致、无导出 INFO
# ---------------------------------------------------------------------------

def test_behavior_without_export_flag_is_unchanged(dup_tree):
    """同 fixture 不带 --export-candidates：产物集合恰好是既有集合
    （脚本副本/双根/trash/缓存+锁/日志/字节码/单一结果 JSON），
    无候选清单文件、无导出 INFO——逐字节不变的回归面。"""
    t = dup_tree
    t.mkdir("trash")
    _build_three_groups(t)

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert len(_moved_sources(out)) == 3
    assert "Candidate list exported" not in out

    top = {p.name for p in t.workdir.iterdir()}
    fixed = {"A", "B", "trash", "dup_finder.py", "logger.py", "pathlogic.py",
           "__pycache__", "logs",
             "file_cache.json", "file_cache.json.lock"}
    assert top >= fixed, fixed - top
    extras = top - fixed
    assert len(extras) == 1
    assert next(iter(extras)).startswith("duplicates_")
    assert not any("candidate" in n.lower() for n in top)


# ---------------------------------------------------------------------------
# ⑥同步目录标记警示
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [".stfolder", ".stfolder-alias", ".sync", ".sync.state"])
def test_sync_marker_direct_child_warns_and_actions_continue(dup_tree, marker):
    """扫描根 A 直接子项含 .stfolder / .sync 前缀标记（本体与变体）→
    恰一条中性 WARNING（指明动作会向对端传播），且出现在动作之前；
    只警示不阻塞：移动照常完成、退出码正常。根 B 无标记不警示。"""
    t = dup_tree
    t.mkdir("trash")
    t.mkdir(f"A/{marker}")
    _build_cross_pair_only(t)

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    warn_lines = [ln for ln in out.splitlines() if "synchronized folder" in ln]
    assert len(warn_lines) == 1, out
    assert f"Scan root A appears to be an active synchronized folder (marker {marker})" in warn_lines[0]
    assert "move/delete actions will propagate to peer devices" in warn_lines[0]
    # 警示先于动作；动作照常完成
    assert out.index("synchronized folder") < out.index("Moved:")
    assert len(moved_payload_files(t.workdir / "trash")) == 1
    assert "Scan root B" not in out


def test_clean_scan_roots_produce_no_sync_warning(dup_tree):
    """两根均无标记：控制台/日志不出现任何同步警示，流程照常。"""
    t = dup_tree
    t.mkdir("trash")
    _build_cross_pair_only(t)

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "synchronized folder" not in out
    assert "marker" not in out
    assert len(moved_payload_files(t.workdir / "trash")) == 1


def test_marker_deeper_than_direct_child_does_not_warn(dup_tree):
    """标记位于子目录深处（非扫描根直接子项）：不警示，动作照常——
    检测范围严格限定在各 -d 根的入口一层。"""
    t = dup_tree
    t.mkdir("trash")
    t.mkdir("A/sub/.stfolder")
    _build_cross_pair_only(t)

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "synchronized folder" not in out
    assert len(moved_payload_files(t.workdir / "trash")) == 1


# ---------------------------------------------------------------------------
# ⑦导出路径父目录缺失 → 自动创建（选择 makedirs 语义并测）
# ---------------------------------------------------------------------------

def test_export_creates_missing_parent_directories(dup_tree):
    """深层缺失父目录 a/b/c：导出成功、逐级创建、内容完整，运行不因路径缺失失败。"""
    t = dup_tree
    _build_cross_pair_only(t)
    assert not (t.workdir / "a").exists()

    proc = t.run("-d", "A", "B", "--try-run", "--export-candidates", "a/b/c/list.txt",
                 "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    exported = t.workdir / "a" / "b" / "c" / "list.txt"
    assert exported.exists()
    lines = exported.read_text().splitlines()
    assert len(lines) == 1 and os.path.isabs(lines[0])
    assert "Candidate list exported: a/b/c/list.txt (1 file(s))" in out
