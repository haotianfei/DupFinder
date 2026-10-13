"""CLI 进程边界 smoke：help 退出码、既有参数全量 help 冒烟、一次最小真实扫描。

只断言当前已有行为。空文件跳过、cross-dirs-only 语义、--try-run 不写正式缓存等
属后续计划中的行为升级，本套件不对它们预设断言。
"""

import hashlib
import json
import os
import re


def test_help_exits_zero(dup_tree):
    """--help 退出码 0，stdout 有 usage。"""
    proc = dup_tree.run("--help")
    assert proc.returncode == 0
    assert "usage:" in proc.stdout


def test_help_lists_every_existing_flag(dup_tree):
    """既有参数全量 help 冒烟：每个长选项与短选项都出现在帮助文本中。"""
    proc = dup_tree.run("--help")
    assert proc.returncode == 0
    out = proc.stdout
    for opt in (
        "--directories",
        "--action",
        "--priority-order",
        "--move-to-dir",
        "--try-run",
        "--min-size",
        "--cache-batch",
        "--cross-dirs-only",
        "--exclude",
        "--exclude-file",
        "--retain",
        "--retain-file",
        "--duplicates-result-file",
    ):
        assert opt in out, f"help 缺少既有参数 {opt}"
    for short in ("-d", "-p", "-m", "-n"):
        assert re.search(rf"(^|\s){short}(\s|,)", out, re.M), f"help 缺少短选项 {short}"


def test_missing_required_directories_exits_two(dup_tree):
    """缺少必填 --directories 时 argparse 以退出码 2 报错（seam 能正确传播错误码）。"""
    proc = dup_tree.run("--action", "move")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert "--directories" in proc.stderr


def test_minimal_try_run_scan_reports_without_formal_artifacts(dup_tree):
    """两个临时目录 + --try-run 的最小扫描：退出码 0、日志里程碑与演练动作齐备，
    但不写任何正式产物（缓存、结果 JSON、临时文件），源文件逐字节不变。"""
    dup_tree.build_minimal()
    before = dup_tree.snapshot()

    proc = dup_tree.run_scan()  # -d A B --try-run
    assert proc.returncode == 0, proc.stderr
    console = proc.stdout + proc.stderr

    # 日志里程碑：控制台流与 workdir 日志文件都出现目录处理记录
    # （不绑定具体 stdout/stderr 归属，后续若日志收紧改流也不受影响）
    for milestone in ("Processing directory: A", "Processing directory: B"):
        assert milestone in console
    log_text = (dup_tree.workdir / "logs" / "DupFinder.log").read_text()
    assert "Processing directory: A" in log_text
    assert "no cache or result file written" in console

    # 演练报告仍然点名那一对重复文件（每个重复组恰有一个成员被列为待处理）
    would_lines = [ln for ln in console.splitlines() if "[TRY RUN]" in ln]
    assert len(would_lines) == 1
    mentioned = [rel for rel in ("A/x_cross.txt", "B/x_cross.dup") if rel in would_lines[0]]
    assert len(mentioned) == 1

    # 正式产物零写入
    assert not (dup_tree.workdir / "file_cache.json").exists()
    assert list(dup_tree.workdir.glob("duplicates_*.json")) == []
    assert list(dup_tree.workdir.glob("*.tmp")) == []

    # 文件系统终态：源文件逐字节、mtime、权限全部未变
    dup_tree.assert_no_file_changed(before)
    dup_tree.assert_byte_identical("A/x_cross.txt", dup_tree.cross_content)
    dup_tree.assert_byte_identical("B/x_cross.dup", dup_tree.cross_content)


def test_full_scan_run_writes_wellformed_result_json(dup_tree):
    """不带 --try-run 的一次真实扫描：结果 JSON 结构正确、缓存正常落盘、重复对恰一个被处理。"""
    dup_tree.build_minimal()
    expected_sha = hashlib.sha256(dup_tree.cross_content).hexdigest()
    content = dup_tree.cross_content

    proc = dup_tree.run("-d", "A", "B")  # 默认 action=move，未给 -m → 原位改名 .dup_finder
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console

    for milestone in ("Processing directory: A", "Processing directory: B"):
        assert milestone in console
    assert "Saved file_dict to duplicates_" in console

    # 结果 JSON：workdir 下唯一、结构正确、内容即那一对跨目录重复文件
    data, path = dup_tree.load_result_json()
    assert path.parent == dup_tree.workdir  # 产物落在隔离树内
    assert list(data) == [expected_sha], "应只有一个以文件内容 sha256 为 key 的重复组"
    group = data[expected_sha]
    assert {(e["path"], e["size"], e["type"]) for e in group} == {
        ("A/x_cross.txt", len(content), ".txt"),
        ("B/x_cross.dup", len(content), ".dup"),
    }
    for entry in group:
        assert set(entry) == {"path", "size", "type", "modified_time", "priority"}
        assert entry["type"] == os.path.splitext(entry["path"])[1]
        assert isinstance(entry["modified_time"], float)
        assert isinstance(entry["priority"], int) and entry["priority"] >= 0

    # 恰有一个成员被改名处理，另一个逐字节原样保留
    processed = [rel for rel in ("A/x_cross.txt", "B/x_cross.dup")
                 if (dup_tree.workdir / (rel + ".dup_finder")).exists()]
    kept = [rel for rel in ("A/x_cross.txt", "B/x_cross.dup")
            if (dup_tree.workdir / rel).exists()]
    assert len(processed) == 1 and len(kept) == 1 and processed[0] != kept[0]
    dup_tree.assert_byte_identical(kept[0], content)

    # 真实运行照常写哈希缓存（两个路径都入缓存）
    cache = json.loads(dup_tree.cache_path().read_text())
    assert set(cache) == {"A/x_cross.txt", "B/x_cross.dup"}
    assert all(v["file_id"] == expected_sha for v in cache.values())
