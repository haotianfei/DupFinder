"""--cross-dirs-only 跨目录专属去重语义测试。

开启后：重复组仅当成员分布在 >=2 个 -d 扫描根时才被处理；整组同根的
"目录内部重复"被整组跳过（不进结果 JSON、不进候选处理）。
默认关闭时行为与现状完全一致。跨根归属按绝对路径的整分量前缀判定，
兄弟目录名（A 与 A-nm）不串根。

全部经 CLI 进程边界断言外部行为（退出码、真实 move 终态、结果 JSON、日志）。
"""

import hashlib

from conftest import console_output, moved_payload_files

CROSS_CONTENT = b"cross-payload-" * 100     # 1400B：跨 A/B 根的重复对
INT_A_CONTENT = b"internal-a-" * 100        # A 根内部重复对
INT_B_CONTENT = b"internal-b-" * 100        # B 根内部重复对

SIBLING_X = b"sibling-cross-" * 100         # A 与 A-nm 两根的跨根对
SIBLING_Y = b"sibling-a-nm-internal-" * 100  # 仅 A-nm 根内部重复对


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _build_two_root_fixture(t):
    """A、B 双根：各含一个内部重复对 + 一对跨根重复。

    A/dirA/xover.txt ┐
    B/b-over.txt     ┘ 跨根组（CROSS_CONTENT）
    A/dirA/a1.txt, a2.txt     A 根内部组
    B/internal/b1.txt, b2.txt B 根内部组
    """
    t.add_file("A/dirA/xover.txt", CROSS_CONTENT)
    t.add_file("B/b-over.txt", CROSS_CONTENT)
    t.add_file("A/dirA/a1.txt", INT_A_CONTENT)
    t.add_file("A/dirA/a2.txt", INT_A_CONTENT)
    t.add_file("B/internal/b1.txt", INT_B_CONTENT)
    t.add_file("B/internal/b2.txt", INT_B_CONTENT)


def _build_sibling_roots_fixture(t):
    """两扫描根取兄弟目录名 A 与 A-nm（归属判定诱饵）。

    A/x1.txt + A-nm/y1.txt   跨根组（SIBLING_X）
    A-nm/y2.txt + A-nm/y3.txt 仅 A-nm 根内部组（SIBLING_Y）
    """
    t.add_file("A/x1.txt", SIBLING_X)
    t.add_file("A-nm/y1.txt", SIBLING_X)
    t.add_file("A-nm/y2.txt", SIBLING_Y)
    t.add_file("A-nm/y3.txt", SIBLING_Y)


def _group_paths(data):
    """结果 JSON → {组key: 排序后的成员相对路径}。"""
    return {k: sorted(e["path"] for e in v) for k, v in data.items()}


def _moved_lines(console_text):
    return [ln for ln in console_text.splitlines() if "Moved:" in ln]


# ---------------------------------------------------------------------------
# AC1：开启后仅跨根组被处理，两目录内部重复零处理（真实 move 断言）
# ---------------------------------------------------------------------------

def test_cross_flag_processes_only_cross_root_group(dup_tree):
    """-d A B --cross-dirs-only --action move -m trash：三个重复组里只有跨根组
    产生动作（恰移动一个成员进 trash），A 内部对与 B 内部对四文件全部原地不动；
    收尾 INFO 报告整组跳过数为 2。"""
    t = dup_tree
    t.mkdir("trash")
    _build_two_root_fixture(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--cross-dirs-only", "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out

    # 动作恰一条，且来自跨根组的成员
    moved = _moved_lines(out)
    assert len(moved) == 1, f"应恰移动 1 个文件:\n{out}"
    moved_members = {rel for rel in ("A/dirA/xover.txt", "B/b-over.txt") if rel in moved[0]}
    assert len(moved_members) == 1, f"应恰好移动跨根组中的一个成员，实际: {moved_members}"

    # 两个内部重复组（共 4 文件）零动作，终态与运行前逐字段一致
    for rel in ("A/dirA/a1.txt", "A/dirA/a2.txt",
                "B/internal/b1.txt", "B/internal/b2.txt"):
        t.assert_file_unchanged(rel, before)

    # trash 里只有那一个被移动的跨根成员
    moved_files = moved_payload_files(t.workdir / "trash")
    assert len(moved_files) == 1
    assert moved_files[0].name in ("xover.txt", "b-over.txt")

    # 结果 JSON 只剩跨根组；同根两组整组不进结果
    data, _ = t.load_result_json()
    assert _group_paths(data) == {
        _sha(CROSS_CONTENT): ["A/dirA/xover.txt", "B/b-over.txt"]
    }

    # 收尾汇总 INFO：整组跳过了 2 个同根组
    assert "Cross-dirs-only: skipped 2 same-root duplicate group(s)" in out


# ---------------------------------------------------------------------------
# AC2：与 retain 分量匹配组合——A 侧文件全程只读不动（mtime+内容双断言）
# ---------------------------------------------------------------------------

def test_cross_flag_with_retain_component_keeps_a_side_untouched(dup_tree):
    """目标场景一条命令：-d A B --cross-dirs-only --retain dirA --action move
    -m trash → 只把 B 中与 A 重复的文件移走；A 侧（含 dirA 分量下全部文件）
    mtime 与内容双断言零变化；B 自身内部重复同样零触碰。"""
    t = dup_tree
    t.mkdir("trash")
    _build_two_root_fixture(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--cross-dirs-only", "--action", "move",
                 "-m", "trash", "--retain", "dirA")
    out = console_output(proc)
    assert proc.returncode == 0, out

    # 唯一被移走的是 B 侧跨根副本（A 侧成员被 dirA 分量关键字保留）
    moved = _moved_lines(out)
    assert len(moved) == 1 and "B/b-over.txt" in moved[0], out
    moved_files = moved_payload_files(t.workdir / "trash")
    assert [p.name for p in moved_files] == ["b-over.txt"]

    # A 侧全量文件：mtime + 内容 + 权限逐字段与运行前一致（只读不动）
    for rel in ("A/dirA/xover.txt", "A/dirA/a1.txt", "A/dirA/a2.txt",
                "B/internal/b1.txt", "B/internal/b2.txt"):
        t.assert_file_unchanged(rel, before)
    assert not (t.workdir / "B/b-over.txt").exists()


# ---------------------------------------------------------------------------
# AC3：关闭态与现状逐字段等价（回归钉桩）
# ---------------------------------------------------------------------------

def test_flag_off_result_identical_to_current_grouping(dup_tree_factory):
    """不开启开关：结果 JSON 与"全量分组、无跨根裁剪"的现状语义逐字段一致——
    三个组全部在场（key→成员路径→大小→类型），且不出现任何 Cross-dirs-only 日志。
    对照组：同一 fixture 开启后只剩跨根组。"""
    def oracle_groups():
        return {
            _sha(CROSS_CONTENT): [("A/dirA/xover.txt", len(CROSS_CONTENT), ".txt"),
                                  ("B/b-over.txt", len(CROSS_CONTENT), ".txt")],
            _sha(INT_A_CONTENT): [("A/dirA/a1.txt", len(INT_A_CONTENT), ".txt"),
                                  ("A/dirA/a2.txt", len(INT_A_CONTENT), ".txt")],
            _sha(INT_B_CONTENT): [("B/internal/b1.txt", len(INT_B_CONTENT), ".txt"),
                                  ("B/internal/b2.txt", len(INT_B_CONTENT), ".txt")],
        }

    def project(data):
        return {k: sorted((e["path"], e["size"], e["type"]) for e in v)
                for k, v in data.items()}

    # 关闭态：全部三组在场，字段逐一等价
    t_off = dup_tree_factory("flag-off")
    _build_two_root_fixture(t_off)
    proc = t_off.run("-d", "A", "B", "--action", "move", "-m", "trash",
                     "--retain", "xover.txt", "b-over.txt",
                     "a1.txt", "a2.txt", "b1.txt", "b2.txt")
    out = console_output(proc)
    assert proc.returncode == 0, out
    data, _ = t_off.load_result_json()
    assert project(data) == oracle_groups()
    assert "Cross-dirs-only" not in out          # 关闭态绝不引入新日志

    # 开启态（同构 fixture）：只剩跨根组——开关前后差异恰好是两组同根内部重复
    t_on = dup_tree_factory("flag-on")
    _build_two_root_fixture(t_on)
    proc = t_on.run("-d", "A", "B", "--cross-dirs-only", "--action", "move", "-m", "trash",
                    "--retain", "xover.txt", "b-over.txt")
    assert proc.returncode == 0, console_output(proc)
    data_on, _ = t_on.load_result_json()
    expected_on = {k: sorted((p, s, t) for p, s, t in v)
                   for k, v in oracle_groups().items() if k == _sha(CROSS_CONTENT)}
    assert project(data_on) == expected_on


# ---------------------------------------------------------------------------
# AC4：help 含参数说明与目标场景示例
# ---------------------------------------------------------------------------

def test_help_documents_cross_dirs_flag_with_example(dup_tree):
    """--help 输出含 --cross-dirs-only 选项及其目标场景示例行（wrap 折叠后按
    去空白匹配，规避 argparse 断行对含连字符 token 的切断）。"""
    proc = dup_tree.run("--help")
    assert proc.returncode == 0
    collapsed = "".join(proc.stdout.split())
    assert "--cross-dirs-only" in collapsed
    assert "Example:dup_finder.py-dAB--cross-dirs-only--actionmove-mtrash" in collapsed
    assert "(default:off)" in collapsed


# ---------------------------------------------------------------------------
# AC5 诱饵：兄弟目录名双根归属不串根
# ---------------------------------------------------------------------------

def test_sibling_roots_attribution_does_not_leak_between_A_and_A_nm(dup_tree):
    """两根取 A 与 A-nm：跨根对 {A/x1, A-nm/y1} 必须判为跨根而处理；
    仅 A-nm 内部组 {y2, y3} 必须整组跳过。若归属用裸子串把 'A-nm/...' 误归到
    'A' 根，两组判定会恰好互换——本用例即钉死这一退化。"""
    t = dup_tree
    t.mkdir("trash")
    _build_sibling_roots_fixture(t)
    before = t.snapshot(under=("A", "A-nm"))

    proc = t.run("-d", "A", "A-nm", "--cross-dirs-only", "--action", "move",
                 "-m", "trash", "--retain", "x1.txt")
    out = console_output(proc)
    assert proc.returncode == 0, out

    # 跨根组处理：A-nm/y1.txt 被移走（A 侧 x1.txt 由分量关键字保留）
    moved = _moved_lines(out)
    assert len(moved) == 1 and "A-nm/y1.txt" in moved[0], out
    moved_files = moved_payload_files(t.workdir / "trash")
    assert [p.name for p in moved_files] == ["y1.txt"]

    # A-nm 内部组（y2/y3）整组零触碰；A/x1 保留
    for rel in ("A/x1.txt", "A-nm/y2.txt", "A-nm/y3.txt"):
        t.assert_file_unchanged(rel, before)

    data, _ = t.load_result_json()
    assert _group_paths(data) == {_sha(SIBLING_X): ["A-nm/y1.txt", "A/x1.txt"]}
    assert "Cross-dirs-only: skipped 1 same-root duplicate group(s)" in out


# ---------------------------------------------------------------------------
# 补充语义边界
# ---------------------------------------------------------------------------

def test_no_summary_line_when_nothing_is_skipped(dup_tree):
    """开启但没有任何同根组可跳（只有一个跨根组）：不打印 Cross-dirs-only 汇总，
    日志保持干净。"""
    t = dup_tree
    t.add_file("A/only1.bin", CROSS_CONTENT)
    t.add_file("B/only2.bin", CROSS_CONTENT)

    proc = t.run("-d", "A", "B", "--cross-dirs-only", "--try-run")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "Cross-dirs-only" not in out
    assert "Try run mode: 1 duplicate group(s) found" in out


def test_single_root_with_cross_flag_skips_all_groups(dup_tree):
    """单根 + 开关：组内成员必然同属一个根 → 全部整组跳过，零动作，
    汇总 INFO 报告跳过数；结果 JSON 为空对象。"""
    t = dup_tree
    t.mkdir("trash")
    t.add_file("A/int1.bin", INT_A_CONTENT)
    t.add_file("A/int2.bin", INT_A_CONTENT)

    proc = t.run("-d", "A", "--cross-dirs-only", "--try-run")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "Cross-dirs-only: skipped 1 same-root duplicate group(s)" in out
    assert "Try run mode: 0 duplicate group(s) found" in out
    assert "[TRY RUN]" not in out              # 没有任何待处理动作
