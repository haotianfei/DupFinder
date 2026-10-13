"""保留关键字的路径分量精确匹配测试。

规则：关键字与路径按分量切分（POSIX/Windows 分隔符归一、忽略空分量与尾部分隔符）
后，命中当且仅当关键字分量序列作为**连续整分量**出现在路径分量序列中。
单分量关键字必须等于某个完整分量——`dirA` 命中 `dirA/...` 下的文件，绝不命中
相邻兄弟目录 `dirA-nm/...`（旧的裸子串规则两者都命中，把兄弟目录一并卷入处理，
正是旧语义的问题所在）。`--retain`、`--retain-file`、`--priority-order path` 共用同一套
分量匹配语义。

全部经 CLI 进程边界断言外部行为（退出码、动作日志、文件系统终态、结果 JSON）。
"""

import os

import pytest

from conftest import console_lines_with, console_output, moved_payload_files

TRAP_CONTENT = b"trap-content" * 100  # 1200B：三成员同内容同 size，落入同一重复组


def _build_sibling_trap(t):
    """一组"相邻同名前缀目录"陷阱：dirA 组件 + 兄弟 dirA-nm 组件，全部同内容。

    A/dirA/keep.txt            —— dirA 分量子目录
    A/dirA/nested/inner.txt    —— dirA 分量子目录更深处
    A/dirA-nm/decoy.txt        —— 兄弟目录：旧子串规则会把 "dirA" 关键字也命中到它
    """
    t.add_file("A/dirA/keep.txt", TRAP_CONTENT)
    t.add_file("A/dirA/nested/inner.txt", TRAP_CONTENT)
    t.add_file("A/dirA-nm/decoy.txt", TRAP_CONTENT)


def _shape(t):
    """终态形状：相对路径 → 内容 sha256（跨树可比较，不含 mtime/时间戳类字段）。"""
    return {rel: state[3] for rel, state in t.snapshot().items()}


def _deleted_paths(console_text):
    return console_lines_with(console_text, "Deleted:")


# ---------------------------------------------------------------------------
# AC1：dirA 命中 dirA 分量，绝不命中兄弟目录 dirA-nm
# ---------------------------------------------------------------------------

def test_retain_dir_component_delete_processes_only_sibling_decoy(dup_tree):
    """--retain dirA --action delete：dirA 分量下的两个文件全保留（priority 0），
    兄弟目录 dirA-nm 的诱饵不被关键字命中、恰好被处理删除一个。"""
    t = dup_tree
    _build_sibling_trap(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "--action", "delete", "--retain", "dirA")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1, f"应恰好处理 1 个文件:\n{out}"
    assert "A/dirA-nm/decoy.txt" in deleted[0]

    t.assert_file_unchanged("A/dirA/keep.txt", before)
    t.assert_file_unchanged("A/dirA/nested/inner.txt", before)
    assert not (t.workdir / "A/dirA-nm/decoy.txt").exists()

    # 结果 JSON：三成员同组；dirA 下两个 priority=0（保留），诱饵非 0（被处理）
    data, _ = t.load_result_json()
    (group,) = data.values()
    prio = {e["path"]: e["priority"] for e in group}
    assert sorted(prio) == ["A/dirA-nm/decoy.txt", "A/dirA/keep.txt", "A/dirA/nested/inner.txt"]
    assert prio["A/dirA/keep.txt"] == 0
    assert prio["A/dirA/nested/inner.txt"] == 0
    assert prio["A/dirA-nm/decoy.txt"] > 0


def test_retain_dir_component_move_takes_only_sibling_decoy(dup_tree):
    """--retain dirA --action move -m trash：真实移动动作同样只波及 dirA-nm 诱饵，
    dirA 分量下文件零触碰，回收目录里恰好只有 decoy.txt。"""
    t = dup_tree
    t.mkdir("trash")
    _build_sibling_trap(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "--action", "move", "-m", "trash", "--retain", "dirA")
    out = console_output(proc)
    assert proc.returncode == 0, out

    moved_lines = console_lines_with(out, "Moved:")
    assert len(moved_lines) == 1 and "A/dirA-nm/decoy.txt" in moved_lines[0]
    t.assert_file_unchanged("A/dirA/keep.txt", before)
    t.assert_file_unchanged("A/dirA/nested/inner.txt", before)

    moved_files = moved_payload_files(t.workdir / "trash")
    assert [p.name for p in moved_files] == ["decoy.txt"]


# ---------------------------------------------------------------------------
# AC2：带/不带尾部分隔符两种写法行为一致
# ---------------------------------------------------------------------------

def test_retain_with_and_without_trailing_slash_behave_identically(dup_tree_factory):
    """同一棵树分别用关键字 "dirA" 与 "dirA/" 跑真实删除：两次终态形状逐路径相等，
    且都是"保留 dirA 两成员、删除 dirA-nm 诱饵"——不再有子串技巧带来的差异。"""
    shapes = {}
    for name, keyword in (("plain", "dirA"), ("slash", "dirA/")):
        t = dup_tree_factory(name)
        _build_sibling_trap(t)
        proc = t.run("-d", "A", "--action", "delete", "--retain", keyword)
        out = console_output(proc)
        assert proc.returncode == 0, out
        deleted = _deleted_paths(out)
        assert len(deleted) == 1 and "A/dirA-nm/decoy.txt" in deleted[0]
        shapes[name] = _shape(t)

    assert shapes["plain"] == shapes["slash"]
    assert set(shapes["plain"]) == {"A/dirA/keep.txt", "A/dirA/nested/inner.txt"}


@pytest.mark.parametrize("keyword", ["dirA/nested", "dirA/nested/", "dirA\\nested"])
def test_retain_multi_component_keyword_matches_contiguous_run(dup_tree, keyword):
    """多分量关键字按"连续分量序列"匹配：dirA/nested 只命中 nested 路径下的成员；
    尾部 '/' 与反斜杠写法与正斜杠等价（分隔符风格归一）。未命中的 keep/decoy 均被
    处理，唯 inner.txt 幸存。"""
    t = dup_tree
    _build_sibling_trap(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "--action", "delete", "--retain", keyword)
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 2, f"除 inner.txt 外两个成员都应被处理:\n{out}"
    assert any("decoy.txt" in ln for ln in deleted)
    assert any("keep.txt" in ln for ln in deleted)
    t.assert_file_unchanged("A/dirA/nested/inner.txt", before)
    assert not (t.workdir / "A/dirA/keep.txt").exists()
    assert not (t.workdir / "A/dirA-nm/decoy.txt").exists()


# ---------------------------------------------------------------------------
# AC3：旧用法兼容——完整文件名片段按分量语义
# ---------------------------------------------------------------------------

def test_retain_full_filename_components_keep_everything(dup_tree):
    """旧用法：以完整文件名作关键字（keep.txt/inner.txt/decoy.txt 都是真实分量），
    全部命中 → 三成员都保留，move 动作零执行、回收目录为空。"""
    t = dup_tree
    t.mkdir("trash")
    _build_sibling_trap(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "--action", "move", "-m", "trash",
                 "--retain", "keep.txt", "inner.txt", "decoy.txt")
    out = console_output(proc)
    assert proc.returncode == 0, out

    t.assert_no_file_changed(before, under=("A",))
    assert [p for p in (t.workdir / "trash").rglob("*") if p.is_file()] == []


def test_retain_important_directory_component_readme_style(dup_tree):
    """README 式场景 --retain important：名为 important 的目录分量命中，
    其下文件保留；副本在未命中的兄弟目录中被删除。"""
    t = dup_tree
    content = b"snap" * 300
    t.add_file("A/important/photo.dat", content)
    t.add_file("A/misc/photo-copy.dat", content)

    proc = t.run("-d", "A", "--action", "delete", "--retain", "important")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1 and "A/misc/photo-copy.dat" in deleted[0]
    assert (t.workdir / "A/important/photo.dat").exists()


def test_retain_fragment_substring_trick_no_longer_matches(dup_tree):
    """反向钉桩：关键字 "keep" 只是 "keep.txt" 分名的前缀片段（旧裸子串会命中并
    保留它）。分量语义下 "keep" 不等于任何完整分量 → 不保留：按 mtime 升序浅规则，
    较新的 A/keep.txt 被处理、较旧的副本保留——与旧语义恰好相反。"""
    t = dup_tree
    content = b"frag" * 250
    older = t.add_file("B/older-copy.bin", content)
    newer = t.add_file("A/keep.txt", content)
    base = older.stat().st_mtime
    os.utime(newer, (base + 100, base + 100))  # 钉死排序方向，避免同秒 mtime 竞态

    proc = t.run("-d", "A", "B", "--action", "delete", "--retain", "keep")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1 and "A/keep.txt" in deleted[0], (
        f"片段关键字不应再按子串保留 keep.txt:\n{out}"
    )
    assert (t.workdir / "B/older-copy.bin").exists()


# ---------------------------------------------------------------------------
# 共用入口：--retain-file 与 --priority-order path 共享同一套匹配语义
# ---------------------------------------------------------------------------

def test_retain_file_keywords_use_same_component_matching(dup_tree):
    """--retain-file 每行关键字与 --retain 走同一分量匹配：文件内容 "dirA" 同样
    保留 dirA 分量下两成员、只处理 dirA-nm 诱饵。"""
    t = dup_tree
    _build_sibling_trap(t)
    before = t.snapshot()
    t.add_file("retain_keys.txt", b"dirA\n")

    proc = t.run("-d", "A", "--action", "delete", "--retain-file", "retain_keys.txt")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1 and "A/dirA-nm/decoy.txt" in deleted[0]
    t.assert_file_unchanged("A/dirA/keep.txt", before)
    t.assert_file_unchanged("A/dirA/nested/inner.txt", before)


def test_priority_order_path_depth_sorting_component_normalized(dup_tree):
    """--priority-order path：深度按归一分量数度量（目录越深越先保留）。
    同内容一对文件一深一浅、无 retain → 深者保留、浅者被处理，结果确定。"""
    t = dup_tree
    content = b"depth-pair" * 150
    t.add_file("A/shallow.bin", content)
    t.add_file("A/deep/sub/copy.bin", content)

    proc = t.run("-d", "A", "--action", "delete", "--priority-order", "path")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1 and "A/shallow.bin" in deleted[0]
    assert (t.workdir / "A/deep/sub/copy.bin").exists()


def test_priority_order_path_combined_with_retain_shares_matching(dup_tree):
    """排序字段换成 path 时，retain 关键字仍按同一分量匹配生效：与默认排序下
    相同的终态——保留 dirA 两成员、只处理 dirA-nm 诱饵。"""
    t = dup_tree
    _build_sibling_trap(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "--action", "delete",
                 "--priority-order", "path", "--retain", "dirA")
    out = console_output(proc)
    assert proc.returncode == 0, out

    deleted = _deleted_paths(out)
    assert len(deleted) == 1 and "A/dirA-nm/decoy.txt" in deleted[0]
    t.assert_file_unchanged("A/dirA/keep.txt", before)
    t.assert_file_unchanged("A/dirA/nested/inner.txt", before)
