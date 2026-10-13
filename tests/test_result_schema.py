"""重复结果文件载入前置校验 + "同哈希不同大小"降级警示。

载入校验：--duplicates-result-file 的 json 在任何消费（裁剪/选择/动作）之前过
validate_result_schema 守卫——顶层 dict[str, list]、条目 dict 且含 str 'path'
与 int 'priority'（bool 拒绝）。不合法 → 逐条 ERROR（组 key、条目定位、字段名，
最多 20 条 + 总数）+ 非零退出 + 零动作。合法路径零行为变化。

同哈希不同大小：assign_priorities 中的罕见碰撞分支自降级警示，整组全部保留。
经缓存播种（两条不同 size 文件共享同一缓存 file_id）在扫描路径构造触发。

全部经 CLI 进程边界断言外部行为。
"""

import hashlib
import json

from conftest import console_output

FAKE_ID_1 = "a" * 64          # 64 位小写 hex：形状等同真实 sha256，仅用于缓存播种
FAKE_ID_2 = "b" * 64


def _write(t, rel, data):
    return t.add_file(rel, data)


def _seed_cache(t, entries_by_path):
    """按真实磁盘 mtime/size 播种缓存，file_id 用给定值（命中→不重算）。"""
    cache = {}
    for rel, file_id in entries_by_path.items():
        st = (t.workdir / rel).stat()
        cache[rel] = {"file_id": file_id, "modified_time": st.st_mtime, "size": st.st_size}
    t.cache_path().write_text(json.dumps(cache))
    return cache


def _make_cross_pair(t, content, a_rel="A/p.txt", b_rel="B/p-copy.txt"):
    _write(t, a_rel, content)
    _write(t, b_rel, content)
    return a_rel, b_rel


def _error_lines(out):
    return [ln for ln in out.splitlines() if "ERROR" in ln]


# ---------------------------------------------------------------------------
# ①缺 priority 字段：明确报错、逐条定位、零动作
# ---------------------------------------------------------------------------

def test_missing_priority_field_load_errors_with_location_and_zero_actions(dup_tree):
    """结果文件里一条成员缺 'priority'：载入即败——rc=1，ERROR 指明组 key、
    条目路径与缺失字段；不创建回收目录、源树 snapshot 分毫未动。"""
    t = dup_tree
    a_rel, b_rel = _make_cross_pair(t, b"schema-pair" * 100)
    result = {
        hashlib.sha256(b"schema-pair" * 100).hexdigest(): [
            {"path": a_rel, "size": 1100, "type": ".txt", "modified_time": 1.0, "priority": 1},
            {"path": b_rel, "size": 1100, "type": ".txt", "modified_time": 1.0},   # 缺 priority
        ]
    }
    t.add_file("review_result.json", json.dumps(result).encode())
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash",
                 "--duplicates-result-file", "review_result.json")
    out = console_output(proc)
    assert proc.returncode == 1, out

    errors = _error_lines(out)
    joined = "\n".join(errors)
    assert "Invalid duplicates result file review_result.json: 1 problem(s) found" in joined
    assert "priority" in joined
    assert b_rel in joined                      # 定位到具体条目路径
    assert next(iter(result)) in joined         # 定位到组 key
    # 零动作：回收目录不存在、无 Moved/Deleted 日志、源树逐字段未变
    assert not (t.workdir / "trash").exists()
    assert "Moved:" not in out and "Deleted:" not in out
    t.assert_no_file_changed(before)


# ---------------------------------------------------------------------------
# ②非法结构：条目非 dict / priority 为字符串 / bool 不算 int
# ---------------------------------------------------------------------------

def test_illegal_entry_shapes_reported_not_crash(dup_tree):
    """三种非法结构一次性逐条报告：元素非 dict、priority 是字符串、priority 是
    bool（int 子类但被明确拒绝）；rc=1 且总数汇总正确，零动作。"""
    t = dup_tree
    _make_cross_pair(t, b"shape" * 300)
    result = {
        "groupA": ["just-a-string"],                                  # 非 dict 条目
        "groupB": [{"path": "A/str.bin", "priority": "5"}],           # priority 为字符串
        "groupC": [{"path": "A/bool.bin", "priority": True}],         # bool 拒绝
        "groupOK": [{"path": "A/ok1.bin", "priority": 0},
                    {"path": "A/ok2.bin", "priority": 1}],             # 合法组
    }
    t.add_file("review_result.json", json.dumps(result).encode())

    proc = t.run("-d", "A", "B", "--try-run",
                 "--duplicates-result-file", "review_result.json")
    out = console_output(proc)
    assert proc.returncode == 1, out

    joined = "\n".join(_error_lines(out))
    assert "3 problem(s) found" in joined
    assert "group 'groupA' entry #0: entry must be an object" in joined
    assert "group 'groupB' entry #0 (path='A/str.bin')" in joined and "priority" in joined
    assert "group 'groupC' entry #0 (path='A/bool.bin')" in joined
    assert "Loaded file_dict" not in out          # 校验失败则不进入消费


def test_problem_list_truncation_via_cli(dup_tree):
    """25 条非法条目：ERROR 行恰列出前 20 条 + '... and 5 more problem(s)'，
    总行 "25 problem(s)"。"""
    t = dup_tree
    t.mkdir("A")
    result = {"big": [{"path": f"A/bad{i}.bin"} for i in range(25)]}   # 全缺 priority
    t.add_file("review_result.json", json.dumps(result).encode())

    proc = t.run("-d", "A", "--try-run", "--duplicates-result-file", "review_result.json")
    out = console_output(proc)
    assert proc.returncode == 1, out

    errors = _error_lines(out)
    joined = "\n".join(errors)
    assert "25 problem(s) found" in joined
    assert "... and 5 more problem(s) not listed" in joined
    listed = [ln for ln in errors if "entry #" in ln]
    assert len(listed) == 20, len(listed)


# ---------------------------------------------------------------------------
# ③合法载入回归：正常完成、动作与此前一致
# ---------------------------------------------------------------------------

def test_valid_result_file_load_behavior_unchanged(dup_tree):
    """结构合法（含 bool 之外的全部字段要求）：载入照常——try-run 依文件中
    priority 计划处理较高数值一侧，校验路径零行为变化。"""
    t = dup_tree
    a_rel, b_rel = _make_cross_pair(t, b"valid-load" * 200)
    digest = hashlib.sha256(b"valid-load" * 200).hexdigest()
    result = {
        digest: [
            {"path": a_rel, "size": 2000, "type": ".txt", "modified_time": 5.0, "priority": 0},
            {"path": b_rel, "size": 2000, "type": ".txt", "modified_time": 6.0, "priority": 1},
        ]
    }
    t.add_file("review_result.json", json.dumps(result).encode())
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--try-run",
                 "--duplicates-result-file", "review_result.json")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "Loaded file_dict from review_result.json" in out
    assert not _error_lines(out)
    would = [ln for ln in out.splitlines() if "[TRY RUN]" in ln]
    assert len(would) == 1 and b_rel in would[0]     # priority 大者被处理（同旧行为）
    assert "Invalid duplicates result file" not in out
    t.assert_no_file_changed(before)


# ---------------------------------------------------------------------------
# ④同哈希不同大小：降级 warning、整组保留、无 ERROR 级该行
# ---------------------------------------------------------------------------

def test_same_digest_different_sizes_warns_and_keeps_all(dup_tree):
    """缓存播种令两条不同 size 文件共享同一 file_id（模拟摘要碰撞进入扫描的
    分组）：assign 阶段输出 warning"哈希一致但大小不同、全部保留待人工甄别"，
    不再是 error 级；全部文件保留、零处理动作、snapshot 逐字段未变。

    为通过 size 预过滤，每个 size 组需 >=2 文件：1200 组与 300 组各含一个
    "碰撞成员 + 一个 partner"，两个 partner 共享另一 id（同样不同 size，也触发）。
    """
    t = dup_tree
    _write(t, "A/big.bin", b"B" * 1200)            # 1200 → FAKE_ID_1
    _write(t, "A/big_partner.bin", b"b" * 1200)    # 1200 → FAKE_ID_2（size 伴生）
    _write(t, "B/small.bin", b"S" * 300)           # 300  → FAKE_ID_1（与 big 同 id 不同 size）
    _write(t, "B/small_partner.bin", b"s" * 300)   # 300  → FAKE_ID_2
    _seed_cache(t, {
        "A/big.bin": FAKE_ID_1,
        "B/small.bin": FAKE_ID_1,
        "A/big_partner.bin": FAKE_ID_2,
        "B/small_partner.bin": FAKE_ID_2,
    })
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--try-run")
    out = console_output(proc)
    assert proc.returncode == 0, out

    warn_lines = [ln for ln in out.splitlines()
                  if "Same digest with differing sizes" in ln]
    assert len(warn_lines) == 2, out                    # 两个碰撞组各一条
    assert all("WARNING" in ln for ln in warn_lines)
    assert "kept for manual review" in "\n".join(warn_lines)
    for ln in out.splitlines():                         # 该行绝不以 error 级出现
        if "Same digest" in ln or "inconsistent sizes" in ln:
            assert "ERROR" not in ln, ln
    # 整组全部保留：无任何处理动作、源树分毫未动
    assert "[TRY RUN]" not in out
    t.assert_no_file_changed(before)
