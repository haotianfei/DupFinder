"""DupFinder 端到端测试底座：唯一 seam = CLI 进程边界。

测试只通过 subprocess 运行 `python3 dup_finder.py ...`，断言外部行为：
退出码、控制台/日志里程碑、文件系统终态、产物文件（缓存 JSON、结果 JSON）。
不断言任何内部函数细节。

== 正式缓存/日志的隔离机制（红线保障） ==

阅读源码确认两类产物的落点规则不同：

1. 哈希缓存与结果 JSON 相对 **子进程 cwd**：
   - dup_finder.py 的 `find_duplicates(..., cache_file='file_cache.json')`
     与 `lock_file = f"{cache_file}.lock"` 是相对路径；
   - main() 写 `output_file = f"duplicates_{ts}.json"` 也是相对路径。
2. 日志相对 **脚本文件所在目录**（与 cwd 无关）：
   - logger.py `logs_dir = os.path.join(os.path.dirname(__file__), 'logs')`。

因此仅改 cwd 不足以保护仓库根的 logs/。本底座的可靠隔离方式：
每个用例先把仓库顶层 `dup_finder.py`、`logger.py` 复制到该用例私有 tmp_path，
再以 cwd=该 tmp_path 运行 `python3 <tmp>/dup_finder.py ...`——缓存、锁、日志、
结果 JSON 全部落在 tmp 内；仓库根的真实产物（file_cache.json 15747 条真实哈希缓存、
file_cache.json.lock、logs/、duplicates_*.json、out/）连导入都不发生（dup_finder
只在子进程中以副本形式执行），绝不会被触碰。

autouse fixture `protect_repo_artifacts` 额外在每个用例前后对受保护产物做
mtime_ns/size 快照比对（它们都在 .gitignore 里，git status 不可见，故用时间戳对比），
任何泄漏立即让用例失败。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DUPLICATE_GLOB = "duplicates_*.json"
LOG_BASENAMES = ("DupFinder.log", "logger.log")
# 子进程的缓存文件名由 dup_finder 内部默认值决定（CLI 无参数可改），helper 与之一致
CACHE_BASENAME = "file_cache.json"

# 各场景内容长度：0（两个空文件）与刻意成对的重复内容之外，其余全局唯一。
LEN_CROSS = 1000       # 跨目录重复对共享大小
LEN_INDIR = 2000       # 目录内部重复对共享大小
LEN_SYMTARGET = 3000   # 软链接与其目标（跟随读，内容相同）
LEN_UNIQUE_A = 4100    # size 全局唯一文件 1
LEN_UNIQUE_B = 7200    # size 全局唯一文件 2
LEN_UNREADABLE = 900   # 不可读文件（大小也全局唯一）

_PAYLOAD_UNIT = b"DUPFINDER-PAYLOAD-"


def _payload(n):
    """生成指定长度、内容确定的字节串。"""
    return (_PAYLOAD_UNIT * (n // len(_PAYLOAD_UNIT) + 1))[:n]


def _file_state(path):
    """单文件终态指纹：(size, mtime_ns, mode, sha256(content)|None, link_target|None)。

    stat/open 均跟随软链接，与扫描器当前行为一致；不可读文件 sha 为 None（哨兵）。
    """
    st = path.stat()
    try:
        with open(path, "rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        sha = None
    link = os.readlink(path) if path.is_symlink() else None
    return (st.st_size, st.st_mtime_ns, st.st_mode, sha, link)


# --------------------------------------------------------------------------
# 红线守卫：受保护的仓库根既有产物
# --------------------------------------------------------------------------

def _stat_pair(path):
    """路径的 (mtime_ns, size) 指纹；不可 stat（被外部删除/权限）记哨兵 ("__MISSING__", -1)。

    哨兵使"受保护产物被外部删除/出现"表现为干净的字典差异（断言失败），
    而不是让测试框架在快照阶段 crash。
    """
    try:
        st = path.stat()
    except OSError:
        return ("__MISSING__", -1)
    return (st.st_mtime_ns, st.st_size)


def _protected_state():
    state = {}
    paths = sorted(REPO_ROOT.glob("*.py"))
    paths += sorted(REPO_ROOT.glob("file_cache.json"))
    paths += sorted(REPO_ROOT.glob("file_cache.json.lock"))
    paths += sorted(REPO_ROOT.glob(DUPLICATE_GLOB))
    for d in (REPO_ROOT / "logs", REPO_ROOT / "out"):
        state[f"{d}/"] = _stat_pair(d)
        if d.is_dir():
            paths += sorted(d.rglob("*"))
    for p in paths:
        state[str(p)] = _stat_pair(p)
    return state


@pytest.fixture(autouse=True)
def protect_repo_artifacts(request):
    """每个用例前后比对仓库根受保护产物的 mtime_ns/size，任何改动即失败。

    校验用 request.addfinalizer 先注册、后取基线快照：即使用例在 fixture
    构造阶段崩溃，teardown 检查仍会执行，且以断言失败（而非框架异常）呈现。
    """
    before = None

    def _verify():
        nonlocal before
        if before is None:
            pytest.fail("守卫基线快照未建立，无法确认仓库根受保护产物未被改动")
        after = _protected_state()
        assert after == before, (
            "红线告警：本用例期间仓库根的正式缓存/日志/结果产物发生了变化！"
        )

    request.addfinalizer(_verify)
    before = _protected_state()


# --------------------------------------------------------------------------
# DupTree：隔离工作树 + 场景构造 + CLI seam + 终态断言辅助
# --------------------------------------------------------------------------

class DupTree:
    """一棵可安全运行 DupFinder CLI 的隔离工作树（脚本副本 + 场景数据 + 产物都在 workdir 下）。

    - 场景目录用相对路径传给 CLI（扫描根为 A/ 与 B/），因此缓存 key、结果 JSON 里的
      path 均为形如 "A/x.txt" 的相对路径，与 cwd=workdir 的子进程一致；
    - SCAN_ARGS 为默认扫描参数（-d A B --try-run）：演练模式，绝不物理改动源文件；
    - 子进程只会读写 workdir 下的 file_cache.json（dup_finder 内部默认值，CLI 无法
      指定缓存名），helper 的 prewarm/断言与之对齐。
    """

    SCAN_ARGS = ("-d", "A", "B", "--try-run")

    def __init__(self, workdir):
        self.workdir = Path(workdir)
        self.root_a = self.workdir / "A"
        self.root_b = self.workdir / "B"

    # ---- 原始构造子（后续测试可自由组合出任意树） ----

    def mkdir(self, rel):
        p = self.workdir / rel
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_file(self, rel, data=b""):
        p = self.workdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def add_symlink(self, link_rel, existing_target_rel):
        """同目录相对软链接（link 与目标同目录，target 用裸文件名）。"""
        link = self.workdir / link_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(Path(existing_target_rel).name)
        return link

    def add_unreadable(self, rel, data=b""):
        """chmod 000 的不可读普通文件（root 环境下仍无法用权限阻止 root 读，用例已按 euid 跳过断言）。"""
        p = self.add_file(rel, data)
        p.chmod(0)
        return p

    # ---- 七类场景中的六类目录树场景（第七类"缓存预热/未热"见 prewarm_cache / factory） ----

    def add_cross_duplicate_pair(self):
        """跨目录重复对：A 与 B 各一个内容相同的文件。"""
        self.cross_content = _payload(LEN_CROSS)
        a = self.add_file("A/x_cross.txt", self.cross_content)
        b = self.add_file("B/x_cross.dup", self.cross_content)
        return a, b

    def add_within_dir_duplicates(self):
        """目录内部重复：A 内（含一个子目录）两个内容相同的文件。"""
        self.indir_content = _payload(LEN_INDIR)
        y1 = self.add_file("A/y_indir1.txt", self.indir_content)
        y2 = self.add_file("A/sub/y_indir2.txt", self.indir_content)
        return y1, y2

    def add_zero_byte_files(self):
        """0 字节文件：A、B 各一（当前实现下两者同哈希；对空文件的特殊处理属后续计划中的行为升级，此处不预设断言）。"""
        return self.add_file("A/empty.bin"), self.add_file("B/empty.bin")

    def add_symlink_file(self):
        """软链接文件：B/sym_link.txt -> B/sym_target.txt。"""
        self.sym_content = _payload(LEN_SYMTARGET)
        target = self.add_file("B/sym_target.txt", self.sym_content)
        link = self.add_symlink("B/sym_link.txt", "B/sym_target.txt")
        return target, link

    def add_unreadable_file(self):
        """不可读文件（chmod 000）。"""
        self.unreadable_content = _payload(LEN_UNREADABLE)
        return self.add_unreadable("A/secret.bin", self.unreadable_content)

    def add_unique_size_files(self):
        """size 全局唯一文件：A、B 各一，大小不与树内任何其他文件相同。"""
        ua = self.add_file("A/unique_a.bin", _payload(LEN_UNIQUE_A))
        ub = self.add_file("B/unique_b.bin", _payload(LEN_UNIQUE_B))
        return ua, ub

    def build_minimal(self):
        """最小可复现重复：只有一对跨目录重复文件。"""
        self.cross_a, self.cross_b = self.add_cross_duplicate_pair()
        return self

    def build_full_scenarios(self):
        """一棵树内构造全部六类目录场景（缓存态由 factory/prewarm 提供为第七类）。"""
        self.cross_a, self.cross_b = self.add_cross_duplicate_pair()
        self.indir_1, self.indir_2 = self.add_within_dir_duplicates()
        self.zero_a, self.zero_b = self.add_zero_byte_files()
        self.sym_target, self.sym_link = self.add_symlink_file()
        self.unreadable_file = self.add_unreadable_file()
        self.unique_a, self.unique_b = self.add_unique_size_files()
        return self

    # ---- 缓存预热/未热 ----

    def cache_path(self):
        return self.workdir / CACHE_BASENAME

    def prewarm_cache(self):
        """把当前树以正式 JSON 格式（{相对路径: {file_id, modified_time, size}}）预热进缓存文件。

        与既有缓存 JSON 格式逐字段对齐，供"缓存预热起跑"用例复用。
        跳过不可读文件（无法取哈希）与软链接（当前扫描器会跟随并纳入；跳过软链接
        属后续计划中的行为升级，本预热先采用跳过语义，后续测试可自由组合）。返回写入条目数。
        """
        entries = {}
        for base in ("A", "B"):
            for p in sorted((self.workdir / base).rglob("*")):
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
                try:
                    with open(p, "rb") as f:
                        file_id = hashlib.file_digest(f, "sha256").hexdigest()
                except OSError:
                    continue
                entries[str(p.relative_to(self.workdir))] = {
                    "file_id": file_id,
                    "modified_time": st.st_mtime,
                    "size": st.st_size,
                }
        target = self.cache_path()
        target.write_text(json.dumps(entries))
        return len(entries)

    # ---- CLI seam ----

    def run(self, *args):
        """在 workdir 里以子进程运行脚本副本——唯一进入被测系统的方式。"""
        cmd = [
            sys.executable,
            str(self.workdir / "dup_finder.py"),
            *args,
        ]
        return subprocess.run(
            cmd, cwd=str(self.workdir), capture_output=True, text=True, timeout=180
        )

    def run_scan(self, *extra_args):
        """默认扫描：-d A B --try-run（演练，不动源文件）。"""
        return self.run(*self.SCAN_ARGS, *extra_args)

    # ---- 终态快照与断言辅助 ----

    def _scope_paths(self, under):
        paths = []
        for base in under:
            root = self.workdir / base
            if not root.exists():
                continue
            for p in sorted(root.rglob("*")):
                if p.is_symlink() or p.is_file():
                    paths.append(p)
        return paths

    def snapshot(self, under=("A", "B")):
        """记录扫描树内所有文件的终态指纹（含被删除路径在前快照中的存在性）。"""
        return {str(p.relative_to(self.workdir)): _file_state(p) for p in self._scope_paths(under)}

    def changed_paths(self, before, under=("A", "B")):
        """返回与 before 快照相比发生变化（内容/权限/mtime/链接目标）或新增/消失的相对路径集合。"""
        after = self.snapshot(under)
        keys = set(before) | set(after)
        return {k for k in keys if before.get(k) != after.get(k)}

    def assert_no_file_changed(self, before, under=("A", "B")):
        diff = sorted(self.changed_paths(before, under))
        assert not diff, f"文件终态被改动: {diff}"

    def assert_file_unchanged(self, rel, before):
        assert rel in before, f"{rel} 不在前次快照中"
        current = _file_state(self.workdir / rel)
        assert current == before[rel], f"{rel} 已变化"

    def byte_identical(self, rel, data):
        """某文件是否存在且逐字节等于给定内容。"""
        p = self.workdir / rel
        return p.exists() and p.read_bytes() == data

    def assert_byte_identical(self, rel, data):
        assert (self.workdir / rel).exists(), f"{rel} 不存在"
        assert (self.workdir / rel).read_bytes() == data, f"{rel} 内容不是逐字节原样"

    # ---- 产物辅助 ----

    def artifact_paths(self):
        candidates = [
            self.cache_path(),
            self.cache_path().with_name(CACHE_BASENAME + ".lock"),
            *(self.workdir / "logs" / n for n in LOG_BASENAMES),
        ]
        candidates += sorted(self.workdir.glob(DUPLICATE_GLOB))
        return sorted({p for p in candidates if p.exists()})

    def artifact_digests(self):
        """workdir 内产物文件 -> sha256，便于断言"产物是否变化"。"""
        return {
            str(p.relative_to(self.workdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.artifact_paths()
        }

    def load_result_json(self):
        """加载唯一的结果 JSON（duplicates_*.json），返回 (dict, path)。"""
        files = sorted(self.workdir.glob(DUPLICATE_GLOB))
        assert len(files) == 1, (
            f"workdir 下应有且仅有 1 个 {DUPLICATE_GLOB}，实际: {[f.name for f in files]}"
        )
        return json.loads(files[0].read_text()), files[0]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _make_workdir(base):
    """复制顶层脚本到独立目录并建好 A/ B/ 扫描根——一切产物都只会落在这里。"""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(REPO_ROOT / "dup_finder.py"), str(base / "dup_finder.py"))
    shutil.copy2(str(REPO_ROOT / "logger.py"), str(base / "logger.py"))
    (base / "A").mkdir(exist_ok=True)
    (base / "B").mkdir(exist_ok=True)
    return DupTree(base)


@pytest.fixture
def dup_tree(tmp_path):
    """冷缓存、无场景文件的空白隔离树（脚本已复制就绪）。"""
    return _make_workdir(tmp_path)


@pytest.fixture
def dup_tree_factory(tmp_path):
    """按需在同一 tmp_path 下建多棵互不干扰的隔离树。

    典型用法：缓存"预热/未热"两种起跑态各建一棵；不同 name 对应不同子目录，
    各自的 file_cache.json / duplicates_*.json / logs 完全隔离。
    """

    def _factory(name):
        return _make_workdir(tmp_path / f"wd-{name}")

    return _factory


@pytest.fixture
def full_tree(dup_tree):
    """包含全部六类目录场景的冷缓存树。"""
    dup_tree.build_full_scenarios()
    return dup_tree
