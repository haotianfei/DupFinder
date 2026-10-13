"""辅助脚本 change_priority.py 的三项防御测试（CLI 进程边界 seam）。

①缺字段输入 → 指明条目位置的可读错误、非零退出、无 traceback、不产出文件；
②目标匹配与 --retain 同语义（整分量连续窗口：dirA 命中 dirA/... 不命中 dirA-nm/...）；
③符号方向：new = max(0, priority - adjustment)，正值提升保留优先级（数值减小）、
负值降低、0 为下限，且帮助文本写明上述事实。

脚本副本运行于 DupTree 私有 tmp 树（copy_in 复制 change_priority.py 本体）。
"""

import json

from conftest import console_output


def _run(t, *args):
    """在私有 tmp 树内以子进程运行 change_priority.py 副本。"""
    t.copy_in("change_priority.py")
    return t.run_script("change_priority.py", *args)


def _entries(path_priority_pairs, extra=None):
    entries = [
        {"path": path, "size": 10, "type": ".bin", "modified_time": 1.0, "priority": priority}
        for path, priority in path_priority_pairs
    ]
    if extra:
        entries.extend(extra)
    return entries


def _load(t, name="out.json"):
    return json.loads((t.workdir / name).read_text())


# ---------------------------------------------------------------------------
# ①缺字段：可读错误、非零退出、无 traceback
# ---------------------------------------------------------------------------

def test_missing_priority_field_readable_error_no_traceback(dup_tree):
    """条目缺 'priority'：stderr 指明组 key、条目序号与字段名并以 rc=1 退出，
    不再中途抛裸 KeyError traceback，也不写出结果文件。"""
    t = dup_tree
    data = {"g1": _entries([("dirA/keep.bin", 5)],
                           extra=[{"path": "dirA/oops.bin", "size": 10}])}  # 第二条缺 priority
    t.add_file("in.json", json.dumps(data).encode())

    proc = _run(t, "-i", "in.json", "-o", "out.json", "-p", "dirA", "-a", "1")
    out = console_output(proc)
    assert proc.returncode == 1, out
    assert "Traceback" not in out and "KeyError" not in out
    assert "结构不合法" in out
    assert "priority" in out and "entry #1" in out and "g1" in out
    assert not (t.workdir / "out.json").exists()


def test_top_level_not_object_readable_error(dup_tree):
    """顶层不是对象（误传数组）：同样给出指明问题的可读错误并非零退出。"""
    t = dup_tree
    t.add_file("in.json", json.dumps([1, 2, 3]).encode())

    proc = _run(t, "-i", "in.json", "-o", "out.json", "-p", "x", "-a", "1")
    out = console_output(proc)
    assert proc.returncode == 1, out
    assert "Traceback" not in out
    assert "top level must be an object" in out
    assert not (t.workdir / "out.json").exists()


# ---------------------------------------------------------------------------
# ②分量感知匹配：dirA 命中 dirA/...，绝不命中兄弟 dirA-nm/...
# ---------------------------------------------------------------------------

def test_component_aware_match_does_not_hit_sibling_directory(dup_tree):
    """关键字 dirA：只调整 dirA 完整分量下的条目；兄弟目录 dirA-nm、
    前缀分量 dirA-at-root 均不受影响——匹配语义与 --retain 一致。"""
    t = dup_tree
    data = {
        "g": _entries([
            ("base/dirA/x.txt", 5),       # 命中：dirA 是完整分量
            ("base/dirA-nm/y.txt", 5),    # 不命中：dirA-nm ≠ dirA
            ("dirA-at-root/z.txt", 5),    # 不命中：整分量是 dirA-at-root
            ("dirA/w.txt", 5),            # 命中：dirA 分量
        ])
    }
    t.add_file("in.json", json.dumps(data).encode())

    proc = _run(t, "-i", "in.json", "-o", "out.json", "-p", "dirA", "-a", "2")
    assert proc.returncode == 0, console_output(proc)

    by_path = {e["path"]: e["priority"] for e in _load(t)["g"]}
    assert by_path["base/dirA/x.txt"] == 3        # 5 - 2
    assert by_path["dirA/w.txt"] == 3             # 5 - 2
    assert by_path["base/dirA-nm/y.txt"] == 5     # 兄弟目录原样
    assert by_path["dirA-at-root/z.txt"] == 5     # 非整分量原样


# ---------------------------------------------------------------------------
# ③符号方向：数值语义 + 0 下限 + 帮助文本一致性
# ---------------------------------------------------------------------------

def test_positive_adjustment_raises_retention_and_clamps_at_zero(dup_tree):
    """正值 adjustment → priority 减小（保留优先级提升）并截断到 >=0；
    输出按 priority 升序排列。"""
    t = dup_tree
    data = {"g": _entries([("dirA/high.bin", 4), ("dirA/low.bin", 2)])}
    t.add_file("in.json", json.dumps(data).encode())

    proc = _run(t, "-i", "in.json", "-o", "out.json", "-p", "dirA", "-a", "3")
    assert proc.returncode == 0, console_output(proc)

    group = _load(t)["g"]
    assert [e["priority"] for e in group] == [0, 1]          # 升序；2-3 截 0，4-3=1
    values = {e["path"]: e["priority"] for e in group}
    assert values["dirA/low.bin"] == 0
    assert values["dirA/high.bin"] == 1


def test_negative_adjustment_lowers_retention(dup_tree):
    """负值 adjustment → priority 增大（保留优先级降低）。"""
    t = dup_tree
    data = {"g": _entries([("dirA/a.bin", 5)])}
    t.add_file("in.json", json.dumps(data).encode())

    proc = _run(t, "-i", "in.json", "-o", "out.json", "-p", "dirA", "-a", "-2")
    assert proc.returncode == 0, console_output(proc)
    assert _load(t)["g"][0]["priority"] == 7                 # 5 - (-2)


def test_help_text_matches_actual_sign_direction(dup_tree):
    """帮助文本对符号方向与分量匹配语义的陈述与实际实现一致（防漂移）。"""
    t = dup_tree
    proc = _run(t, "--help")
    out = console_output(proc)
    assert proc.returncode == 0, out
    flat = " ".join(out.split())
    assert "正值提升保留优先级" in flat                        # 方向事实
    assert "priority - adjustment" in flat
    assert "按路径分量匹配" in flat                           # 匹配语义
    assert "dirA-nm" in flat                                  # 兄弟目录反例写进帮助
