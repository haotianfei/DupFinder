"""move JSONL 清单与 --undo 回滚闭环测试。

清单：move 动作每成功一条即向 <回收目录>/moves_<时间戳>.jsonl 追加一行
{"src": 绝对路径, "dst": 绝对路径}（即时 flush）；delete / try-run / 改名模式
（无 --move-to-dir）一律不产清单；零成功移动时懒创建不落地。
--undo：接清单文件或含 moves_*.jsonl 的目录；与扫描（-d）互斥；dst→src 反向
恢复，原位已被占用则跳过不覆盖（保护回滚期间新数据），回收目录内变空的目录
骨架清理，收尾给出 restored/skipped_conflict/missing/total 计数核对。

全部经 CLI 进程边界断言外部行为；不假设重复组内哪一侧被移走——一切以清单实际
记录的 src/dst 为准。
"""

import json
import os
import re
import shutil

from conftest import console_output, is_move_manifest, moved_payload_files

CONTENT_P1 = b"undo-pair-one-" * 200     # 2800B
CONTENT_P2 = b"undo-pair-two-" * 200     # 2800B：与 P1 同 size 不同内容（跨 size 组边界诱饵）
CONTENT_P3 = b"undo-pair-three-" * 200   # 3200B

# 三对跨目录重复，src 候选分散在不同深度（给空骨架清理留出深层级）
PAIRS = (
    ("A/deep/sub/p1.txt", "B/p1-copy.txt", CONTENT_P1),
    ("A/deep/p2.txt", "B/deep/p2-copy.txt", CONTENT_P2),
    ("A/p3.txt", "B/other/p3-copy.txt", CONTENT_P3),
)

SUMMARY_RE = re.compile(
    r"Undo summary: restored=(\d+) skipped_conflict=(\d+) missing=(\d+) total=(\d+)"
)


def _build_pairs(t):
    for rel_a, rel_b, content in PAIRS:
        t.add_file(rel_a, content)
        t.add_file(rel_b, content)


def _read_manifest_entries(t):
    manifests = _manifest_files(t)
    assert len(manifests) == 1, [p.name for p in manifests]
    assert manifests[0].parent == t.workdir / "trash"          # 落在回收目录根部
    lines = [ln for ln in manifests[0].read_text().splitlines() if ln.strip()]
    return manifests[0], [json.loads(ln) for ln in lines]


def _manifest_files(t):
    return sorted(p for p in (t.workdir / "trash").iterdir() if is_move_manifest(p))


def _undo_summary(console_text):
    m = SUMMARY_RE.search(console_text)
    assert m, f"未找到计数核对行:\n{console_text}"
    counts = {"restored": int(m.group(1)), "skipped_conflict": int(m.group(2)),
              "missing": int(m.group(3)), "total": int(m.group(4))}
    assert (counts["restored"] + counts["skipped_conflict"] + counts["missing"]
            == counts["total"]), f"计数不自洽: {counts}"
    return counts


def _rel(t, abs_path):
    return os.path.relpath(abs_path, t.workdir)


# ---------------------------------------------------------------------------
# AC1：清单行数 == 实际移动数、逐行可解析、src/dst 均为真实发生过的路径
# ---------------------------------------------------------------------------

def test_move_writes_one_manifest_line_per_actual_move(dup_tree):
    """三对重复各移走一个成员 → 清单恰 3 行；每行仅含 src/dst 两个绝对路径字段：
    src 确为移动前的原位（现已不在原处），dst 在回收目录下真实存在且同名。
    收尾 INFO 报告清单路径与条目数。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    all_original_rels = [rel for rel_a, rel_b, _ in PAIRS for rel in (rel_a, rel_b)]

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    moved_log = [ln for ln in out.splitlines() if "Moved:" in ln]
    assert len(moved_log) == 3, f"真实移动应为 3 条:\n{out}"

    manifest, entries = _read_manifest_entries(t)
    assert len(entries) == 3
    moved_now = {str(p.relative_to(t.workdir)) for p in moved_payload_files(t.workdir / "trash")}

    for entry in entries:
        assert set(entry) == {"src", "dst"}
        assert os.path.isabs(entry["src"]) and os.path.isabs(entry["dst"])
        src_rel, dst_rel = _rel(t, entry["src"]), _rel(t, entry["dst"])
        assert src_rel in all_original_rels, src_rel            # 移动前的真实原位
        assert not (t.workdir / src_rel).exists()               # 已被移走
        assert dst_rel in moved_now                             # dst 在回收目录下
        assert (t.workdir / dst_rel).exists()                   # 且真实存在
        assert os.path.basename(dst_rel) == os.path.basename(src_rel)

    # 每个移动组恰好一个成员被移走：清单 dst 集合与 trash 负载一一对应
    assert {e["dst"] for e in entries} == {str(p) for p in moved_payload_files(t.workdir / "trash")}
    assert f"Move manifest: trash/{manifest.name} (3 entries)" in out


def test_manifest_created_lazily_zero_moves_no_manifest(dup_tree):
    """懒创建：真实运行但零移动（全部 retain）→ 不产生清单文件、不报告路径。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    retain = tuple(os.path.basename(rel) for rel_a, rel_b, _ in PAIRS for rel in (rel_a, rel_b))
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash", "--retain", *retain)
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _manifest_files(t) == []
    assert list(t.workdir.rglob("moves_*.jsonl")) == []
    assert "Move manifest" not in out


# ---------------------------------------------------------------------------
# AC2：--undo 闭环——目录树还原至移动前逐字段一致；空骨架清理；undo 不碰缓存
# ---------------------------------------------------------------------------

def test_undo_restores_tree_identical_and_prunes_skeleton(dup_tree):
    """move 三成员 → --undo trash（不带 -d）：snapshot（size/mtime_ns/权限位/内容
    sha）与移动前逐字段一致；回收目录深处骨架清空只剩清单文件；undo 不改正式
    缓存字节、不新增结果文件；计数行 restored=3 且自洽。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    p1 = t.workdir / "A/deep/sub/p1.txt"
    os.chmod(p1, 0o750)
    os.utime(p1, (1234.5, 1234.5))
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)
    assert t.snapshot() != before                              # 确有文件被移走
    cache_bytes = t.cache_path().read_bytes()
    result_files = sorted(t.workdir.glob("duplicates_*.json"))

    proc = t.run("--undo", "trash")                            # undo 模式不要求 -d
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _undo_summary(out) == {"restored": 3, "skipped_conflict": 0,
                                  "missing": 0, "total": 3}

    # 目录树终态与移动前逐字段一致（size/mtime_ns/权限/内容/存在性）
    assert t.snapshot() == before

    # 空骨架清理：回收目录内只剩清单，所有 <file_id>/深度目录层级被移除
    leftovers = sorted((t.workdir / "trash").rglob("*"))
    assert all(p.is_file() and is_move_manifest(p) for p in leftovers), \
        [str(p) for p in leftovers]
    assert moved_payload_files(t.workdir / "trash") == []

    # undo 只读清单做恢复：正式缓存逐字节不变、结果文件集合不变
    assert t.cache_path().read_bytes() == cache_bytes
    assert sorted(t.workdir.glob("duplicates_*.json")) == result_files


# ---------------------------------------------------------------------------
# AC3：src 原位被占用 → 跳过不覆盖；其余条目照常恢复；计数自洽
# ---------------------------------------------------------------------------

def test_undo_skips_occupied_src_without_overwriting(dup_tree):
    """move 后模拟"回滚期间新数据"：在某条清单记录的 src 原位写入全新文件
    （内容/权限/mtime 均新）。--undo：该条计入 skipped_conflict——占用文件逐字节
    +mtime+权限原样不被覆盖、其 dst 保留在回收目录；其余两条 restored；计数与
    分项自洽。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)
    _, entries = _read_manifest_entries(t)

    victim, others = entries[0], entries[1:]
    occupied_rel = _rel(t, victim["src"])
    intruder = b"brand-new-data-written-during-rollback-window" * 7
    occupied = t.add_file(occupied_rel, intruder)              # 原位重建为全新数据
    os.chmod(occupied, 0o600)
    os.utime(occupied, (4321.0, 4321.0))
    occupied_state = t.snapshot()[occupied_rel]

    proc = t.run("--undo", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _undo_summary(out) == {"restored": 2, "skipped_conflict": 1,
                                  "missing": 0, "total": 3}

    # 占用文件不被覆盖：内容逐字节、mtime_ns、权限位与 undo 前完全一致
    assert t.snapshot()[occupied_rel] == occupied_state
    assert (t.workdir / occupied_rel).read_bytes() == intruder
    # 被跳过条目的 dst 仍留在回收目录（未被动过）
    assert os.path.exists(victim["dst"])
    assert os.path.basename(victim["dst"]) in [p.name for p in moved_payload_files(t.workdir / "trash")]
    # 其余两条：src 已归位，dst 已搬空
    for entry in others:
        assert os.path.exists(entry["src"])
        assert not os.path.exists(entry["dst"])


def test_undo_counts_missing_when_destination_already_gone(dup_tree):
    """回滚前手动处理掉一个 dst（模拟回收件被清理）：该条计入 missing 并告警，
    其余照常 restored；total 与分项核对一致。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)

    victims = moved_payload_files(t.workdir / "trash")
    assert len(victims) == 3
    removed = victims[0]
    os.remove(removed)

    proc = t.run("--undo", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _undo_summary(out) == {"restored": 2, "skipped_conflict": 0,
                                  "missing": 1, "total": 3}
    assert f"recorded destination no longer exists: {removed}" in out


def test_undo_skips_malformed_manifest_lines_without_crash(dup_tree):
    """清单被手工编辑混入非对象行（字符串/数字）与缺字段行：坏行告警跳过，
    正常行照常全部恢复，进程不崩、计数不受干扰。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)

    manifests = sorted((t.workdir / "trash").glob("moves_*.jsonl"))
    assert len(manifests) == 1
    with open(manifests[0], "a") as f:
        f.write('"just a string"\n42\n{"src": "x"}\n')

    proc = t.run("--undo", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert out.count("Malformed manifest entry") == 3
    assert _undo_summary(out) == {"restored": 3, "skipped_conflict": 0,
                                  "missing": 0, "total": 3}
    assert moved_payload_files(t.workdir / "trash") == []


def test_undo_recreates_missing_parent_directories(dup_tree):
    """回滚前 src 的父目录整棵消失：undo 重建缺失目录后完成恢复（不越界、
    不误删他物），权限与时间戳保留。使用独立目录 D 的一对内重复，删除
    被移走成员所在的整个父目录以制造缺失。"""
    t = dup_tree
    t.mkdir("trash")
    pair = b"orphan-parent-payload" * 50
    a = t.add_file("D/side/orig-a.bin", pair)
    b = t.add_file("D/side/orig-b.bin", pair)
    os.chmod(a, 0o640)
    os.chmod(b, 0o662)   # 两者各带非常规权限：无论哪个被移走都能验证 mode 保留

    proc = t.run("-d", "D", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)
    moved = moved_payload_files(t.workdir / "trash")
    assert len(moved) == 1
    _, entries = _read_manifest_entries(t)
    src_abs = entries[0]["src"]
    src_parent = os.path.dirname(src_abs)                      # D/side
    removed_name = os.path.basename(src_abs)
    # 移动是 rename：回收件仍带着原始 mode/mtime，以此为准对照
    st_before = os.stat(entries[0]["dst"])

    shutil.rmtree(src_parent)                                  # 连兄弟文件一起消失
    assert not os.path.isdir(src_parent)

    proc = t.run("--undo", "trash")
    counts = _undo_summary(console_output(proc))
    assert counts == {"restored": 1, "skipped_conflict": 0, "missing": 0, "total": 1}

    assert os.path.isdir(src_parent)                           # 缺失父目录已重建
    restored = os.path.join(src_parent, removed_name)
    assert os.path.exists(restored)
    st_after = os.stat(restored)
    assert st_after.st_mode & 0o777 == st_before.st_mode & 0o777   # 权限位保留
    assert st_after.st_mtime_ns == st_before.st_mtime_ns           # 时间戳保留
    # 恢复只按清单还原该条：兄弟文件不会被多造
    assert sorted(os.listdir(src_parent)) == [removed_name]


# ---------------------------------------------------------------------------
# AC4：delete / try-run 不产清单；--undo 非法目标明确报错；与 -d 互斥
# ---------------------------------------------------------------------------

def test_delete_and_try_run_produce_no_manifest(dup_tree_factory):
    """delete 真实运行与 try-run 演练：工作树内均无 moves_*.jsonl、无清单 INFO；
    try-run 同时保持零正式产物红线。"""
    t_del = dup_tree_factory("delete-no-manifest")
    t_del.mkdir("trash")
    _build_pairs(t_del)
    proc = t_del.run("-d", "A", "B", "--action", "delete", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert list(t_del.workdir.rglob("moves_*.jsonl")) == []
    assert "Move manifest" not in out

    t_try = dup_tree_factory("tryrun-no-manifest")
    t_try.mkdir("trash")
    _build_pairs(t_try)
    proc = t_try.run("-d", "A", "B", "--action", "move", "-m", "trash", "--try-run")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert list(t_try.workdir.rglob("moves_*.jsonl")) == []
    assert "Move manifest" not in out
    assert "Moved:" not in out
    assert list(t_try.workdir.glob("duplicates_*.json")) == []   # 零产物红线保持


def test_undo_with_rename_only_move_writes_no_manifest(dup_tree):
    """改名模式（move 不带 --move-to-dir）不产清单——清单只归属回收目录根部。"""
    t = dup_tree
    _build_pairs(t)
    proc = t.run("-d", "A", "B", "--action", "move")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert list(t.workdir.rglob("moves_*.jsonl")) == []
    assert "Move manifest" not in out


def test_undo_target_without_manifest_exits_nonzero(dup_tree):
    """--undo 指向空目录或不存在路径：非零退出码 + 明确错误信息；零文件系统动作。"""
    t = dup_tree
    _build_pairs(t)
    t.mkdir("empty_dir")
    tree_before = t.snapshot()
    # 数据目录与清单产物才是关注对象；__pycache__/logs 与脚本副本属基础设施
    expected_names = {"A", "B", "empty_dir", "dup_finder.py", "logger.py", "pathlogic.py",
                      "__pycache__", "logs"}

    proc = t.run("--undo", "empty_dir")
    out = console_output(proc)
    assert proc.returncode != 0
    assert "Undo target has no moves_*.jsonl manifest to replay: empty_dir" in out

    proc = t.run("--undo", "no_such_path")
    assert proc.returncode != 0
    assert ("Undo target has no moves_*.jsonl manifest to replay: no_such_path"
            in console_output(proc))

    # 数据树逐字段未变，且没有生成任何清单/结果文件
    assert t.snapshot() == tree_before
    assert not (set(os.listdir(t.workdir)) - expected_names)
    assert list(t.workdir.rglob("moves_*.jsonl")) == []
    assert list(t.workdir.glob("duplicates_*.json")) == []


def test_undo_and_directories_are_mutually_exclusive(dup_tree):
    """--undo 与 -d 同时给出：argparse 校验报错、退出码 2、信息指向互斥。"""
    t = dup_tree
    t.mkdir("trash")
    proc = t.run("--undo", "trash", "-d", "A")
    out = console_output(proc)
    assert proc.returncode == 2
    assert "usage:" in out
    assert "--undo rollback mode cannot be combined with -d/--directories" in out


def test_undo_accepts_single_manifest_file_path(dup_tree):
    """--undo 也接受直接的清单文件路径（相对 cwd），效果等同目录模式。"""
    t = dup_tree
    t.mkdir("trash")
    _build_pairs(t)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    assert proc.returncode == 0, console_output(proc)
    manifest, entries = _read_manifest_entries(t)

    proc = t.run("--undo", str(manifest.relative_to(t.workdir)))
    counts = _undo_summary(console_output(proc))
    assert counts["restored"] == 3 and counts["total"] == 3
    for entry in entries:
        assert os.path.exists(entry["src"])
        assert not os.path.exists(entry["dst"])
    assert moved_payload_files(t.workdir / "trash") == []
