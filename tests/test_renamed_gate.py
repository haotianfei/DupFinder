"""异名重复门禁（renamed-dupes gate）与 --approve 审批闭环测试（spec v2 §6 十二项验收）。

规则回顾（v2，opt-out 反转）：处理队列最终确定之后，候选 C 与组内保留项 K 按
`os.path.basename(C) == os.path.basename(K)` 判定——同名照常处理，异名移出队列、
写入 renamed_pending_<ts>_<us>.txt 待审清单。清单行默认预标记 `x`（拟执行）；否决
= 改标记为空格/`-` 或直接删行；`!` 病态路径行永不执行（--approve-all 亦不）。
文件级安全底线是 body-sha256 未经审阅拒绝门：正文（所有非 `#` 行按原始字节以 \n
连接）重算与头部记录一致 = 从未人工编辑 → --approve 拒绝 rc1 零动作；显式
--approve-all 跳过该门执行全部 x 行。逐条四项校验、moves 清单与 --undo 兼容不变。

全部经 CLI 进程边界断言外部行为（退出码、控制台里程碑、文件系统终态、产物文件）。
"""

import hashlib
import json
import os
import re

from conftest import console_output, is_move_manifest, moved_payload_files

SAME_CONTENT = b"gate-same-name-" * 80          # 1200B 同名对
REN1_CONTENT = b"gate-renamed-1-" * 100         # 1300B 异名对 1
REN2_CONTENT = b"gate-renamed-2-" * 100         # 1300B 与 REN1 同 size 不同内容（跨组诱饵）
REN3_CONTENT = b"gate-renamed-3-" * 120         # 1440B 异名对 2
REN4_CONTENT = b"gate-renamed-4-" * 140         # 1680B 异名对 3

GATE_SUMMARY = "Renamed-dupes gate: held {held} renamed candidate(s) for review; {same} same-name candidate(s) stay in the processing queue"  # noqa: E501

APPROVE_SUMMARY_RE = re.compile(
    r"Approve summary: executed=(\d+) skipped=(\d+) mismatch=(\d+) total=(\d+)"
)
UNDO_SUMMARY_RE = re.compile(
    r"Undo summary: restored=(\d+) skipped_conflict=(\d+) missing=(\d+) total=(\d+)"
)


def _add_pair(t, rel_a, rel_b, content):
    """A 侧刻意改旧 mtime → 默认优先级规则下保留侧稳定为 A（候选=B 侧）。"""
    a = t.add_file(rel_a, content)
    b = t.add_file(rel_b, content)
    base = b.stat().st_mtime
    os.utime(a, (base - 10, base - 10))
    os.utime(b, (base, base))
    return a, b


def _build_same_and_renamed(t):
    """同名对（A/B 各一个 same.txt）+ 异名对（orig.cfg vs renamed-copy.cfg）。"""
    _add_pair(t, "A/same.txt", "B/same.txt", SAME_CONTENT)
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)


def _pending_files(t):
    return sorted(t.workdir.glob("renamed_pending_*.txt"))


def _latest_pending(t):
    files = _pending_files(t)
    assert files, f"应存在 renamed_pending_*.txt: {sorted(p.name for p in t.workdir.iterdir())}"
    return files[-1]


def _body_lines(path):
    """非注释、非空的数据行。"""
    return [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]


def _protect_rows(path, rows="x", replace="-"):
    """批量否决标记（模拟 sed -i 's/^x/-'/'）：rows 集合内行首改 replace。"""
    text = path.read_text()
    pattern = rf"(?m)^[{re.escape(rows)}]"
    path.write_text(re.sub(pattern, replace, text))


def _body_sha(text):
    """独立重算正文摘要（测试侧参照实现，spec §3 规则）：
    所有不以 '#' 开头的行（含空行）按原始字节以 \n 连接后 sha256。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join(ln for ln in lines if not ln.startswith("#"))
    return hashlib.sha256(body.encode()).hexdigest()


def _recorded_body_sha(text):
    m = [ln for ln in text.splitlines() if ln.startswith("# body-sha256:")]
    assert len(m) == 1, m
    return m[0].split(":", 1)[1].strip()


def _approve_summary(console_text):
    m = APPROVE_SUMMARY_RE.search(console_text)
    assert m, f"未找到审批计数行:\n{console_text}"
    counts = {"executed": int(m.group(1)), "skipped": int(m.group(2)),
              "mismatch": int(m.group(3)), "total": int(m.group(4))}
    assert counts["executed"] + counts["skipped"] + counts["mismatch"] == counts["total"], \
        f"计数不自洽: {counts}"
    return counts


def _undo_summary(console_text):
    m = UNDO_SUMMARY_RE.search(console_text)
    assert m, f"未找到回滚计数行:\n{console_text}"
    return {"restored": int(m.group(1)), "skipped_conflict": int(m.group(2)),
            "missing": int(m.group(3)), "total": int(m.group(4))}


def _manifest_entries(t):
    """回收目录根部全部 moves_*.jsonl 的 {src, dst} 条目。"""
    entries = []
    for p in sorted((t.workdir / "trash").iterdir()):
        if is_move_manifest(p):
            entries += [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    return entries


def _gate_run(t, *extra):
    """默认 gate 的正式扫描 + move 到 trash。"""
    t.mkdir("trash")
    return t.run("-d", "A", "B", "--action", "move", "-m", "trash", *extra)


# ---------------------------------------------------------------------------
# 验收①：gate 默认开——异名候选保留在原地，同名候选照常 move/delete
# ---------------------------------------------------------------------------

def test_gate_default_move_holds_renamed_and_processes_samename(dup_tree):
    """同名对被照常移走一个成员；异名对两侧都留在原地、进待审清单。
    控制台摘要给出 gated/同名处理计数与 pending 文件路径。"""
    t = dup_tree
    _build_same_and_renamed(t)
    proc = _gate_run(t)
    out = console_output(proc)
    assert proc.returncode == 0, out

    # 同名组：恰一个成员被移走（较旧的 A/same.txt 保留），异名组两侧原地不动
    moved = moved_payload_files(t.workdir / "trash")
    assert [p.name for p in moved] == ["same.txt"]
    assert (t.workdir / "A/same.txt").exists()
    assert not (t.workdir / "B/same.txt").exists()
    for rel in ("A/orig.cfg", "B/renamed-copy.cfg"):
        assert (t.workdir / rel).exists(), f"异名候选不得被动过: {rel}"

    assert GATE_SUMMARY.format(held=1, same=1) in out
    pending = _latest_pending(t)
    assert f"pending review file: {pending.name}" in out
    assert len(_body_lines(pending)) == 1


def test_gate_default_delete_holds_renamed_and_processes_samename(dup_tree):
    """delete 模式与 move 同等生效：同名组删一个，异名组零触碰。"""
    t = dup_tree
    _build_same_and_renamed(t)
    proc = t.run("-d", "A", "B", "--action", "delete")
    out = console_output(proc)
    assert proc.returncode == 0, out

    assert not (t.workdir / "B/same.txt").exists()
    assert (t.workdir / "A/same.txt").exists()
    for rel in ("A/orig.cfg", "B/renamed-copy.cfg"):
        assert (t.workdir / rel).exists(), f"异名候选不得被删: {rel}"
    assert GATE_SUMMARY.format(held=1, same=1) in out
    assert len(_pending_files(t)) == 1


# ---------------------------------------------------------------------------
# 验收②：pending 文件——存在性、五列格式、默认标记 x、组注释头、排序稳定、
#         body-sha256 头部与正文重算一致、无异名不产文件
# ---------------------------------------------------------------------------

def test_pending_file_format_default_x_mark_and_body_sha(dup_tree):
    """两个异名组 + 一个同名组 try-run 后逐项核对清单：
    头部注释（v2 标记图例逐字）、`# kept:` 组头、**行首默认 'x'**、5 列 TAB 分隔
    （候选绝对/保留绝对/sha256/size）、行序按 (组哈希, kept, 候选) 稳定、
    `# body-sha256:` 记录值 == 测试侧独立重算的正文摘要；同树重跑逐字节一致。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    _add_pair(t, "A/keep.dat", "B/other-use.dat", REN3_CONTENT)
    _add_pair(t, "A/same.txt", "B/same.txt", SAME_CONTENT)

    proc = t.run("-d", "A", "B", "--try-run")
    out = console_output(proc)
    assert proc.returncode == 0, out

    pending = _latest_pending(t)
    text = pending.read_text()
    lines = text.splitlines()
    assert lines[0] == "# Renamed-duplicate review file / 异名重复审批清单"
    assert lines[1] == "# Mark column (single char + TAB): 'x' = approved for processing (default)"
    assert lines[2] == ("#   ' ' or '-' = protected, keep in place; delete line = never executed; "
                        "'!' = pathological, never executed")
    assert lines[3] == ("#   anything else / missing mark = malformed, "
                        "rejected at parse with warning")
    assert lines[4] == "# Columns: mark <TAB> candidate_path <TAB> kept_path <TAB> sha256 <TAB> size"
    assert lines[5] == ("# File with unedited body => --approve refuses (rc1) "
                        "unless --approve-all.")
    assert lines[6].startswith("# body-sha256: ")
    assert "#   sed -i 's/^x/-/' <file>                    # protect everything" in text
    assert "#   vim -c ':g/keepassxc/s/^x/ /' <file>       # pattern-based batch protect" in text

    # body-sha256：头部记录 == 独立参照实现重算（所有非 # 行按原始字节 \n 连接）
    assert _recorded_body_sha(text) == _body_sha(text)

    # 组注释头：每组恰一个 `# kept:`
    kept_headers = [ln for ln in lines if ln.startswith("# kept: ")]
    assert len(kept_headers) == 2
    assert f"# kept: {os.path.abspath(t.workdir / 'A/orig.cfg')}" in kept_headers
    assert f"# kept: {os.path.abspath(t.workdir / 'A/keep.dat')}" in kept_headers

    rows = [ln.split("\t") for ln in _body_lines(pending)]
    assert len(rows) == 2, "同名候选不得进入待审清单"
    for fields in rows:
        assert len(fields) == 5, fields
        mark, cand, kept, file_id, size_text = fields
        assert mark == "x", repr(mark)        # opt-out：默认即拟执行
        assert os.path.isabs(cand) and os.path.isabs(kept)
        assert (t.workdir / cand).exists() and (t.workdir / kept).exists()
        content = (t.workdir / cand).read_bytes()
        assert file_id == hashlib.sha256(content).hexdigest()
        assert int(size_text) == len(content)
        assert os.path.basename(cand) != os.path.basename(kept)

    # 排序稳定：(组哈希 → kept → 候选) 升序，且重跑逐字节一致
    keys = [(f[3], f[2], f[1]) for f in rows]
    assert keys == sorted(keys), keys
    assert t.run("-d", "A", "B", "--try-run").returncode == 0
    second = _latest_pending(t)
    assert second != pending
    assert second.read_text() == text          # 含 body-sha256 行也一致


def test_pending_file_absent_when_no_renamed_candidates(dup_tree):
    """只有同名重复：零 gated，不产生任何 renamed_pending_*.txt，无 gate 摘要行。"""
    t = dup_tree
    t.mkdir("trash")
    _add_pair(t, "A/same.txt", "B/same.txt", SAME_CONTENT)
    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _pending_files(t) == []
    assert "Renamed-dupes gate" not in out
    assert len(moved_payload_files(t.workdir / "trash")) == 1


# ---------------------------------------------------------------------------
# 验收③：--no-gate-renamed 恢复旧行为
# ---------------------------------------------------------------------------

def test_no_gate_renamed_restores_legacy_processing(dup_tree):
    """同名+异名混合树：显式关 gate 后异名候选照常被移走；无 pending、无 gate 摘要。"""
    t = dup_tree
    _build_same_and_renamed(t)
    proc = _gate_run(t, "--no-gate-renamed")
    out = console_output(proc)
    assert proc.returncode == 0, out

    assert sorted(p.name for p in moved_payload_files(t.workdir / "trash")) == \
           ["renamed-copy.cfg", "same.txt"]
    assert not (t.workdir / "B/renamed-copy.cfg").exists()
    assert _pending_files(t) == []
    assert "Renamed-dupes gate" not in out


# ---------------------------------------------------------------------------
# 验收④：try-run + gate——只打印、pending 仍写出、无 moves 清单
# ---------------------------------------------------------------------------

def test_try_run_with_gate_prints_only_and_writes_pending(dup_tree):
    """演练模式下 gate 照常拦截异名项（不打印其动作行）、同名项只打印不执行；
    pending 属审查产物照常写出（默认 x 标记同样生效）；源树逐字段零变化，
    无 moves_*.jsonl 与结果 JSON。"""
    t = dup_tree
    t.mkdir("trash")
    _build_same_and_renamed(t)
    before = t.snapshot()

    proc = t.run("-d", "A", "B", "--try-run", "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out

    would = [ln for ln in out.splitlines() if "[TRY RUN]" in ln]
    assert len(would) == 1 and "B/same.txt" in would[0], (
        f"异名候选不得出现演练动作行:\n{out}")
    assert "Moved:" not in out and "Deleted:" not in out

    # 待审清单照常写出（审查产物豁免），正式产物红线保持
    pending = _latest_pending(t)
    assert len(_body_lines(pending)) == 1
    assert list(t.workdir.rglob("moves_*.jsonl")) == []
    assert list(t.workdir.glob("duplicates_*.json")) == []
    assert not t.cache_path().exists()
    t.assert_no_file_changed(before)


# ---------------------------------------------------------------------------
# 验收⑤：body-sha256 未经审阅拒绝门——未编辑 rc1；仅改注释仍 rc1；改标记通过
# ---------------------------------------------------------------------------

def test_review_gate_refuses_unedited_list(dup_tree):
    """生成的清单（默认全 x）未经编辑：--approve rc1 拒绝、零动作、
    提示须审阅或 --approve-all；源树逐字段未动。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)
    before = t.snapshot()

    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 1, out
    assert "Approve refused" in out
    assert "body-sha256" in out and "--approve-all" in out
    assert "Approve summary" not in out
    t.assert_no_file_changed(before)                      # 拒绝即零动作
    assert list(t.workdir.rglob("moves_*.jsonl")) == []   # 不产清单


def test_review_gate_comment_only_edit_still_refused(dup_tree):
    """只在清单里增/改注释行（正文未变）不构成审阅：--approve 仍 rc1。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)

    text = pending.read_text()
    pending.write_text(text.replace("#\n# Batch examples:", "# my review note\n#\n# Batch examples:"))
    # 正文（非 # 行）确实一字未改——参照实现重算与记录仍一致
    assert _recorded_body_sha(pending.read_text()) == _body_sha(pending.read_text())

    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 1, out
    assert "Approve refused" in out
    assert (t.workdir / "B/renamed-copy.cfg").exists()


def test_review_gate_unlocked_by_mark_edit(dup_tree):
    """改任一标记（x→'-' 保护）= 正文变更 → 门通过：rc0 零执行（唯一行已保护）。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)

    _protect_rows(pending)                                # 's/^x/-'：全行保护
    assert _recorded_body_sha(pending.read_text()) != _body_sha(pending.read_text())

    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 0,
                                     "mismatch": 0, "total": 0}
    assert (t.workdir / "B/renamed-copy.cfg").exists()


# ---------------------------------------------------------------------------
# 验收⑥/⑦：--approve 保护/删行语义、--approve-all 三态
# ---------------------------------------------------------------------------

def test_approve_all_executes_every_x_row_and_writes_manifest(dup_tree):
    """未编辑清单 + --approve-all：跳过审阅门，全部 x 行执行、标准 moves 清单
    逐条落地（gate 运行零真实移动时懒创建不落地，审批后才有清单）。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    _add_pair(t, "A/keep.dat", "B/other-use.dat", REN3_CONTENT)

    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)
    # gate 运行零真实移动：trash 内没有清单（懒创建）
    assert [p for p in (t.workdir / "trash").iterdir() if is_move_manifest(p)] == []

    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 2, "skipped": 0,
                                     "mismatch": 0, "total": 2}

    for rel in ("B/renamed-copy.cfg", "B/other-use.dat"):
        assert not (t.workdir / rel).exists()
    for rel in ("A/orig.cfg", "A/keep.dat"):
        assert (t.workdir / rel).exists()     # 保留侧永不动作
    payloads = moved_payload_files(t.workdir / "trash")
    assert sorted(p.name for p in payloads) == ["other-use.dat", "renamed-copy.cfg"]
    srcs = {e["src"] for e in _manifest_entries(t)}
    assert srcs == {str(t.workdir / "B/renamed-copy.cfg"), str(t.workdir / "B/other-use.dat")}
    assert "Move manifest" in out


def test_approve_edited_marks_and_deleted_rows_only_x_rows_run(dup_tree):
    """三组异名：一行改 '-' 保护、一行直接删除、一行保持默认 x → --approve（门已
    被正文编辑解锁）只执行余下的 x 行；保护行与删行都不动。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    _add_pair(t, "A/keep.dat", "B/other-use.dat", REN3_CONTENT)
    _add_pair(t, "A/plain.dat", "B/stray-use.dat", REN4_CONTENT)
    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)

    lines = pending.read_text().splitlines()
    edited = []
    for ln in lines:
        if ln.startswith("x") and "renamed-copy.cfg" in ln:
            edited.append("-" + ln[1:])       # 保护方式一：改标记 '-'
        elif ln.startswith("x") and "other-use.dat" in ln:
            continue                          # 保护方式二：直接删行
        else:
            edited.append(ln)                 # stray-use.dat 行保持默认 x → 待执行
    pending.write_text("\n".join(edited) + "\n")

    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 1, "skipped": 0,
                                     "mismatch": 0, "total": 1}
    # 唯一被执行的是保持 x 的行；'-' 行与删行都原地不动
    assert [p.name for p in moved_payload_files(t.workdir / "trash")] == ["stray-use.dat"]
    assert not (t.workdir / "B/stray-use.dat").exists()
    for rel in ("B/renamed-copy.cfg", "B/other-use.dat"):
        assert (t.workdir / rel).exists()


def test_approve_missing_body_sha_record_refused_and_inert_files_via_all(dup_tree):
    """手工伪造/损坏清单（头部缺 body-sha256）：--approve rc1 拒绝；
    同文件加 --approve-all 则照常按行解析——空文件/纯注释零动作 rc0。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    t.mkdir("trash")
    t.add_file("empty_review.txt", b"")
    t.add_file("comments_only.txt", b"# kept: /nowhere/keep.cfg\n# comment line\n")
    before = t.snapshot()

    for name in ("empty_review.txt", "comments_only.txt"):
        proc = t.run("--approve", name, "--action", "move", "-m", "trash")
        out = console_output(proc)
        assert proc.returncode == 1, f"{name}: {out}"      # 缺记录 → 拒绝
        assert "Approve refused" in out and "body-sha256" in out
        proc = t.run("--approve", name, "--approve-all",
                     "--action", "move", "-m", "trash")
        out = console_output(proc)
        assert proc.returncode == 0, f"{name}: {out}"
        assert _approve_summary(out) == {"executed": 0, "skipped": 0,
                                         "mismatch": 0, "total": 0}
    assert t.snapshot() == before
    assert list(t.workdir.rglob("moves_*.jsonl")) == []


def test_approve_all_alone_without_approve_exits_two(dup_tree):
    """--approve-all 是 --approve 的附属逃逸开关：单独使用 argparse 报错 rc2。"""
    t = dup_tree
    proc = t.run("--approve-all", "--action", "move", "-m", "trash", "-d", "A")
    out = console_output(proc)
    assert proc.returncode == 2, out
    assert "usage:" in out
    assert "--approve-all must be combined with --approve PATH" in out


# ---------------------------------------------------------------------------
# 验收⑧：四项逐条校验——候选消失/哈希不符/保留项消失各自归类；mtime 变化仅告警
# ---------------------------------------------------------------------------

def _one_renamed_pair_ready_to_approve(t):
    """gate 跑一轮后返回 (pending 路径, 候选绝对路径, 保留绝对路径)。"""
    _add_pair(t, "A/keep.dat", "B/cand.dat", REN1_CONTENT)
    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)
    return pending, str(t.workdir / "B/cand.dat"), str(t.workdir / "A/keep.dat")


def test_approve_skips_candidate_vanished(dup_tree):
    """校验1（存在）：候选在审批前消失 → skipped_gone，不执行、计数归 skipped。"""
    t = dup_tree
    pending, cand_abs, _kept_abs = _one_renamed_pair_ready_to_approve(t)
    os.remove(cand_abs)

    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 1,
                                     "mismatch": 0, "total": 1}
    assert "skipped_gone" in out
    assert moved_payload_files(t.workdir / "trash") == []


def test_approve_skips_hash_mismatch_and_warns_modified(dup_tree):
    """校验2+3：候选被改成同 size 不同内容 → warn_modified（mtime 与扫描时点不符、
    不拦截）叠加 skipped_mismatch（哈希不符、拦截），文件原样保留、零动作。"""
    t = dup_tree
    pending, cand_abs, _kept_abs = _one_renamed_pair_ready_to_approve(t)
    original = open(cand_abs, "rb").read()
    tampered = b"X" * len(original)
    assert tampered != original and len(tampered) == len(original)
    with open(cand_abs, "wb") as f:
        f.write(tampered)

    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 0,
                                     "mismatch": 1, "total": 1}
    assert "skipped_mismatch" in out and "warn_modified" in out
    assert open(cand_abs, "rb").read() == tampered      # 内容未被处置
    assert moved_payload_files(t.workdir / "trash") == []


def test_approve_mtime_only_change_warns_and_still_executes(dup_tree):
    """校验2 正向半：只 touch（mtime 变、size/内容不变）→ warn_modified 不拦截，
    照常执行（size 同+哈希同则语义不变）。"""
    t = dup_tree
    pending, cand_abs, _kept_abs = _one_renamed_pair_ready_to_approve(t)
    os.utime(cand_abs, (946_684_800.5, 946_684_800.5))   # 与扫描登记值不同

    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "warn_modified" in out
    assert _approve_summary(out) == {"executed": 1, "skipped": 0,
                                     "mismatch": 0, "total": 1}
    assert not os.path.exists(cand_abs)


def test_approve_skips_kept_gone(dup_tree):
    """校验4：保留项消失 → 组语义失效，skipped_kept_gone，候选不得被"升级为原件"处理。"""
    t = dup_tree
    pending, cand_abs, kept_abs = _one_renamed_pair_ready_to_approve(t)
    os.remove(kept_abs)

    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 1,
                                     "mismatch": 0, "total": 1}
    assert "skipped_kept_gone" in out
    assert os.path.exists(cand_abs)


# ---------------------------------------------------------------------------
# 验收⑨：--approve 与 -d/--undo/--duplicates-result-file 互斥 rc2
# ---------------------------------------------------------------------------

def test_approve_mutually_exclusive_with_scan_and_rollback_flags(dup_tree):
    """argparse 层互斥（早退通道不混用）：三种组合各自 rc=2 + usage + 指向冲突参数。"""
    t = dup_tree
    t.add_file("review.txt", b"# nothing\n")
    t.mkdir("trash")
    t.add_file("result.json", b"{}")

    cases = (
        (("--approve", "review.txt", "-d", "A"),
         "--approve mode cannot be combined with -d/--directories"),
        (("--approve", "review.txt", "--undo", "trash"),
         "--approve mode cannot be combined with --undo"),
        (("--approve", "review.txt", "--duplicates-result-file", "result.json"),
         "--approve mode cannot be combined with --duplicates-result-file"),
    )
    for args, expected in cases:
        proc = t.run(*args)
        out = console_output(proc)
        assert proc.returncode == 2, f"{args} → {out}"
        assert "usage:" in out
        assert expected in out, out

    # 单独的 --approve 不带 -d 是合法早退通道（此文件缺 body 记录 → rc1 拒绝，非 rc2）
    proc = t.run("--approve", "review.txt", "--action", "move", "-m", "trash")
    assert proc.returncode == 1, console_output(proc)


# ---------------------------------------------------------------------------
# 验收⑩：manifest → --undo 端到端恢复
# ---------------------------------------------------------------------------

def test_approve_moves_are_undone_end_to_end(dup_tree):
    """主流程同名移动 + 审批异名移动（--approve-all）各写标准清单 →
    --undo trash 一次全部还原：源树终态与初始 snapshot 逐字段一致
    （含 mtime_ns/权限/内容）。"""
    t = dup_tree
    _build_same_and_renamed(t)
    before = t.snapshot()

    proc = _gate_run(t)
    assert proc.returncode == 0, console_output(proc)
    pending = _latest_pending(t)
    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out)["executed"] == 1
    assert len(_manifest_entries(t)) == 2       # 主流程 1 条 + 审批 1 条

    proc = t.run("--undo", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _undo_summary(out) == {"restored": 2, "skipped_conflict": 0,
                                  "missing": 0, "total": 2}
    assert t.snapshot() == before               # 四个文件全部原样归位


# ---------------------------------------------------------------------------
# 验收⑪：TAB/换行病态路径——'!' 行永不执行且不破坏行结构/正文哈希
# ---------------------------------------------------------------------------

def test_pathological_tab_path_row_marked_never_and_inert(dup_tree):
    """候选文件名含 TAB：'!' 行恰 5 字段（转义保证行结构、body 哈希可正常重算）、
    组旁 WARNING；未编辑 → --approve rc1（门照常工作）；--approve-all 也不执行 '!' 行
    （total=0、文件原地）；手动 '!'→'x' 后正文变更 → 门放行，但转义路径解析不到 →
    skipped_gone，绝不执行。"""
    t = dup_tree
    weird_rel = "B/we\tird.cfg"
    _add_pair(t, "A/orig.cfg", weird_rel, REN1_CONTENT)

    proc = _gate_run(t)
    out = console_output(proc)
    assert proc.returncode == 0, out
    pending = _latest_pending(t)
    text = pending.read_text()
    assert "# WARNING: pathological path" in text
    rows = _body_lines(pending)
    assert len(rows) == 1
    fields = rows[0].split("\t")
    assert fields[0] == "!", repr(fields[0])
    assert len(fields) == 5, fields            # 行结构未被裸 TAB 撕裂
    assert "we\\tird.cfg" in fields[1]          # 路径列内是转义形态，无裸 TAB
    assert "\t" not in fields[1]
    # 正文哈希完整可重算（'!' 行不破坏 body 定义）
    assert _recorded_body_sha(text) == _body_sha(text)

    # 未编辑 → 审阅门拒绝（rc1）
    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    assert proc.returncode == 1, console_output(proc)

    # --approve-all：'!' 行依然不执行（只有 x 行会被执行）
    proc = t.run("--approve", pending.name, "--approve-all",
                 "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 0,
                                     "mismatch": 0, "total": 0}
    assert (t.workdir / weird_rel).exists()

    # 手动篡改 '!'→'x'：正文变更门放行，转义路径无法解析 → skipped_gone
    pending.write_text(re.sub(r"(?m)^!", "x", pending.read_text()))
    proc = t.run("--approve", pending.name, "--action", "move", "-m", "trash")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert _approve_summary(out) == {"executed": 0, "skipped": 1,
                                     "mismatch": 0, "total": 1}
    assert "skipped_gone" in out
    assert (t.workdir / weird_rel).exists()
    assert moved_payload_files(t.workdir / "trash") == []


# ---------------------------------------------------------------------------
# spec §5：CLI 参数与输出位置——--renamed-pending-prefix、--export-candidates 注释头
# ---------------------------------------------------------------------------

def test_renamed_pending_prefix_directory_and_prefix_forms(dup_tree):
    """--renamed-pending-prefix：既有目录或尾分隔符取值 → 默认文件名落入该目录；
    其余取值 → 作为文件名前缀 <prefix>_<ts>.txt。cwd 不再产生默认清单。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    t.mkdir("review")

    # 形态 1：尾分隔符 → 目录 + 默认文件名
    proc = t.run("-d", "A", "B", "--try-run", "--renamed-pending-prefix", "review/")
    assert proc.returncode == 0, console_output(proc)
    in_dir = sorted((t.workdir / "review").glob("renamed_pending_*.txt"))
    assert len(in_dir) == 1 and _pending_files(t) == []

    # 形态 2：非目录取值 → 文件名前缀（review 目录存在但 "review/approval" 不是目录）
    proc = t.run("-d", "A", "B", "--try-run", "--renamed-pending-prefix", "review/approval")
    out = console_output(proc)
    assert proc.returncode == 0, out
    prefixed = sorted((t.workdir / "review").glob("approval_*.txt"))
    assert len(prefixed) == 1 and _pending_files(t) == []
    m = re.search(r"pending review file: (\S+)", out)
    assert m and m.group(1).startswith("review/approval_"), out


def test_export_candidates_unfiltered_but_annotated_under_gate(dup_tree):
    """--export-candidates 仍是"选择队列真相"：异名项照常入列，但 gate 截留时在
    清单末尾附注说明实际处理集更小。"""
    t = dup_tree
    _build_same_and_renamed(t)
    proc = t.run("-d", "A", "B", "--try-run", "--export-candidates", "list.txt")
    out = console_output(proc)
    assert proc.returncode == 0, out

    lines = (t.workdir / "list.txt").read_text().splitlines()
    paths = [ln for ln in lines if not ln.startswith("#")]
    notes = [ln for ln in lines if ln.startswith("# NOTE:")]
    assert len(paths) == 2                      # 同名+异名都在导出队列中
    assert len(notes) == 1 and "renamed-dupes gate is ON" in notes[0]
    assert "Candidate list exported: list.txt (2 file(s))" in out


def test_gate_applies_to_loaded_result_file(dup_tree):
    """gate 触发点在队列最终确定之后：--duplicates-result-file 载入的历史结果
    同样被拦截，异名候选零动作、照常进待审清单。"""
    t = dup_tree
    _add_pair(t, "A/orig.cfg", "B/renamed-copy.cfg", REN1_CONTENT)
    digest = hashlib.sha256(REN1_CONTENT).hexdigest()
    result = {digest: [
        {"path": "A/orig.cfg", "size": len(REN1_CONTENT), "type": ".cfg",
         "modified_time": 1.0, "priority": 1},
        {"path": "B/renamed-copy.cfg", "size": len(REN1_CONTENT), "type": ".cfg",
         "modified_time": 2.0, "priority": 2},
    ]}
    t.add_file("review_result.json", json.dumps(result).encode())

    proc = t.run("-d", "A", "B", "--try-run",
                 "--duplicates-result-file", "review_result.json")
    out = console_output(proc)
    assert proc.returncode == 0, out
    assert "[TRY RUN]" not in out
    assert GATE_SUMMARY.format(held=1, same=0) in out
    assert len(_body_lines(_latest_pending(t))) == 1
