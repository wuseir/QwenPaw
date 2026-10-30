"""Encrypted, single-writer DeepTutor 1.7.0 handoff. No credentials in Git.

Windows, single-user SQLite installation; both computers use the same app path.
The bare Git cache has no checkout, so it cannot stage runtime files by accident.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "1.7.0"
MAGIC = b"DT-HANDOFF-1\n"
MAX_PACKED = 80 * 1024 * 1024
MAX_RAW = 2 * 1024 * 1024 * 1024
BASE_ROOTS = (
    "user/chat_history.db", "user/workspace", "memory", "learning_journal",
    "knowledge_bases", "parse_cache", "user/archive",
    "user/.runtime/workspace_presentations",
    "user/.runtime/workspaces.sqlite3",
)
CATALOG = "user/.runtime/workspaces.sqlite3"
SKIP_NAMES = {".env", "sync.key", "id_rsa", "id_ed25519", ".DS_Store"}


class HandoffError(RuntimeError):
    pass


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(name):
    parts = name.split("/")
    if (not name or "\\" in name or ":" in name or "\x00" in name
            or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts)
            or PurePosixPath(name).is_absolute()):
        raise HandoffError("快照包含不安全的文件路径。")
    return name


def confined(root, relative):
    safe_relative(relative)
    candidate = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise HandoffError("不支持符号链接或目录联接，请使用普通的本地学习目录。")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HandoffError("数据路径超出 DeepTutor 运行目录。") from exc
    return candidate


def database(path):
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\0"


def connect_read(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def logical_database_hash(path):
    digest = hashlib.sha256()
    with closing(connect_read(path)) as connection:
        for line in connection.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def catalog_rows(data):
    path = confined(data, CATALOG)
    if not path.exists():
        return []
    with closing(connect_read(path)) as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM workspaces")]


def roots_for(data):
    """Include registered content roots, never the system configuration snapshot."""
    roots = list(BASE_ROOTS)
    for row in catalog_rows(data):
        if row.get("owner_id") != "local-admin":
            raise HandoffError("当前版本只支持单用户 local-admin，不能复制多用户账户。")
        if row.get("kind") == "system":
            continue
        if row.get("kind") not in {"general", "workspace", "session"}:
            raise HandoffError("发现未知工作区类型；请先升级同步工具。")
        target = Path(row["path"])
        try:
            relative = target.resolve().relative_to(data.resolve()).as_posix()
        except ValueError as exc:
            raise HandoffError("存在运行目录外的工作区，不能静默漏同步。请使用 DeepTutor 管理的工作区。") from exc
        if not (re.fullmatch(r"user/workspaces/ws_[a-f0-9]{32}", relative)
                or re.fullmatch(r"user/\.runtime/session_workspaces/[A-Za-z0-9_-]+", relative)):
            raise HandoffError("自定义工作区路径不在支持的托管目录内。")
        roots.append(safe_relative(relative))
    roots = sorted(set(roots))
    if any(b.startswith(a + "/") for a in roots for b in roots if a != b):
        raise HandoffError("快照根目录重叠，请先检查工作区配置。")
    return roots


def iter_files(root):
    if not root.exists():
        return
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise HandoffError("快照数据中存在符号链接或目录联接。")
        if path.is_dir():
            continue
        if not path.is_file():
            raise HandoffError("快照中存在不支持的特殊文件。")
        if (path.name.endswith(("-wal", "-shm", ".lock"))
                or path.name in SKIP_NAMES or path.name.startswith(".env.")):
            continue
        yield path


def snapshot_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if database(source):
        with closing(connect_read(source)) as original, closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HandoffError("SQLite 完整性检查失败。")
            copy.commit()
    else:
        before = sha(source.read_bytes())
        shutil.copy2(source, target)
        if before != sha(target.read_bytes()) or before != sha(source.read_bytes()):
            raise HandoffError("复制期间文件发生变化，请等待任务结束后重试。")


def assert_idle(data):
    for root in roots_for(data):
        for path in iter_files(confined(data, root)):
            if not database(path):
                continue
            with closing(connect_read(path)) as connection:
                tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "turns" in tables and connection.execute(
                    "SELECT 1 FROM turns WHERE status IN ('queued','running','waiting_input') LIMIT 1"
                ).fetchone():
                    raise HandoffError("还有生成中或等待输入的对话，请在网页完成或停止任务后再同步。")


def collect(data, stage):
    roots = roots_for(data)
    files = {}
    total = 0
    for root in roots:
        source_root = confined(data, root)
        if source_root.is_dir():
            confined(stage, root).mkdir(parents=True, exist_ok=True)
            for directory in source_root.rglob("*"):
                if directory.is_dir():
                    confined(stage, directory.relative_to(data).as_posix()).mkdir(parents=True, exist_ok=True)
        for source in iter_files(source_root):
            relative = source.relative_to(data).as_posix()
            destination = confined(stage, relative)
            snapshot_file(source, destination)
            size = destination.stat().st_size
            total += size
            if total > MAX_RAW:
                raise HandoffError("未压缩学习数据超过 2 GiB；请分离大文件后重试。")
            files[relative] = {"sha256": sha(destination.read_bytes()), "bytes": size}
            if database(destination):
                files[relative]["logical_sha256"] = logical_database_hash(destination)
    return roots, files


def fingerprint(files):
    # SQLite headers can change on an idempotent application restart. Compare
    # schema + rows, while retaining byte hashes for archive integrity checks.
    content = {
        name: detail.get("logical_sha256", detail["sha256"])
        for name, detail in files.items()
        if name != "user/workspace/suggestions/starters.json"
    }
    return sha(json_bytes(content))


def encrypt_package(manifest, stage, key):
    manifest["directories"] = sorted(
        path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_dir()
        and any(path.relative_to(stage).as_posix() == root
                or path.relative_to(stage).as_posix().startswith(root + "/") for root in manifest["roots"])
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json_bytes(manifest))
        for name in sorted(manifest["files"]):
            archive.write(confined(stage, name), "data/" + name)
    if buffer.tell() + 64 > MAX_PACKED:
        raise HandoffError("压缩快照超过 80 MiB，未上传；请先分离大附件或向量索引。")
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, buffer.getvalue(), MAGIC)


def unpack_package(payload, stage, key, expected_data_path=None):
    if not payload.startswith(MAGIC) or len(payload) > MAX_PACKED:
        raise HandoffError("不支持的快照格式或快照过大。")
    start = len(MAGIC)
    try:
        plain = AESGCM(key).decrypt(payload[start:start + 12], payload[start + 12:], MAGIC)
    except Exception as exc:
        raise HandoffError("密钥不匹配，或快照被修改；没有恢复任何文件。") from exc
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(n.casefold() for n in names)) or len(names) > 100000:
            raise HandoffError("快照包含重复文件或文件数量超限。")
        if sum(i.file_size for i in infos) > MAX_RAW + 16 * 1024 * 1024:
            raise HandoffError("快照解压体积超限。")
        for info in infos:
            safe_relative(info.filename)
            if info.is_dir() or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise HandoffError("快照包含不支持的目录条目或链接。")
        if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
            raise HandoffError("快照清单无效。")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != 1 or manifest.get("deeptutor") != VERSION:
            raise HandoffError("DeepTutor 版本不一致，当前同步工具只支持 1.7.0。")
        if expected_data_path and os.path.normcase(manifest["data_path"]) != os.path.normcase(str(expected_data_path)):
            raise HandoffError("两台电脑的安装路径不一致。当前版本要求都安装在相同绝对路径。")
        roots = manifest["roots"]
        for root in roots:
            safe_relative(root)
            if root not in BASE_ROOTS and not (
                re.fullmatch(r"user/workspaces/ws_[a-f0-9]{32}", root)
                or re.fullmatch(r"user/\.runtime/session_workspaces/[A-Za-z0-9_-]+", root)
            ):
                raise HandoffError("清单试图恢复白名单外的数据。")
        if sorted(set(roots)) != sorted(roots) or any(
            b.startswith(a + "/") for a in roots for b in roots if a != b
        ):
            raise HandoffError("快照目录清单重叠或重复。")
        if set(names) != {"manifest.json", *("data/" + n for n in manifest["files"])}:
            raise HandoffError("快照文件和校验清单不一致。")
        for directory in manifest.get("directories", []):
            safe_relative(directory)
            if not any(directory == root or directory.startswith(root + "/") for root in roots):
                raise HandoffError("目录位于恢复范围外。")
            confined(stage, directory).mkdir(parents=True, exist_ok=True)
        for name, detail in manifest["files"].items():
            safe_relative(name)
            if not any(name == root or name.startswith(root + "/") for root in roots):
                raise HandoffError("文件位于恢复范围外。")
            content = archive.read("data/" + name)
            if len(content) != detail["bytes"] or sha(content) != detail["sha256"]:
                raise HandoffError("文件校验失败，没有恢复任何文件。")
            path = confined(stage, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if database(path):
                with closing(connect_read(path)) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise HandoffError("恢复前的数据库完整性检查失败。")
    return manifest


class GitTransport:
    def __init__(self, cache, remote, branch="deeptutor-sync", test_local=False):
        if not test_local and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", remote):
            raise HandoffError("请使用不包含令牌的 GitHub HTTPS 仓库地址。")
        if branch != "deeptutor-sync":
            raise HandoffError("快照只能写入 deeptutor-sync 分支，不能写入 main。")
        self.cache, self.remote, self.branch = cache, remote, branch
        if not cache.exists():
            result = subprocess.run(["git", "init", "--bare", str(cache)], capture_output=True)
            if result.returncode:
                raise HandoffError("Git 初始化失败，请安装 Git for Windows。")

    def git(self, *args, content=None):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        # Git does not automatically read Windows Internet Settings. Reuse
        # the configured system proxy for this subprocess only.
        proxies = urllib.request.getproxies()
        if not any(name.lower() == "https_proxy" for name in env) and proxies.get("https"):
            env["HTTPS_PROXY"] = proxies["https"]
        try:
            result = subprocess.run(
                ["git", "-c", "credential.interactive=never", "-c", "http.connectTimeout=12",
                 "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=25",
                 "--git-dir=" + str(self.cache), *args],
                input=content, capture_output=True, env=env, timeout=45,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise HandoffError("Git 超时或未安装；本地记录仍保留，请检查网络和 Git 登录。") from exc
        if result.returncode:
            # Do not echo credential-bearing errors from credential helpers.
            raise HandoffError("Git 操作失败；请检查网络、Git 登录和分支冲突。本地数据及快照仍保留。")
        return result.stdout

    def head(self):
        raw = self.git("ls-remote", self.remote, "refs/heads/" + self.branch).decode().strip()
        if not raw:
            return None
        self.git("fetch", "--no-tags", self.remote, "refs/heads/" + self.branch)
        return self.git("rev-parse", "FETCH_HEAD").decode().strip()

    def read(self, head):
        return self.git("show", head + ":snapshot.bin")

    def publish(self, payload, parent):
        blob = self.git("hash-object", "-w", "--stdin", content=payload).decode().strip()
        tree = self.git("mktree", content=f"100644 blob {blob}\tsnapshot.bin\n".encode()).decode().strip()
        parents = ["-p", parent] if parent else []
        commit = self.git("-c", "user.name=Learning handoff", "-c", "user.email=handoff@localhost",
                          "commit-tree", tree, *parents, "-m", "Encrypted DeepTutor learning handoff").decode().strip()
        # Normal fast-forward push. A concurrent upload rejects this sibling commit.
        self.git("push", self.remote, commit + ":refs/heads/" + self.branch)
        confirmed = self.head()
        if confirmed != commit or sha(self.read(commit)) != sha(payload):
            raise HandoffError("推送结果未能读回确认，未推进本机同步基线。")
        return commit


class Handoff:
    def __init__(self, app):
        self.app = app.resolve()
        self.data = self.app / "runtime" / "data"
        self.local = self.app / "sync-local"
        self.local.mkdir(parents=True, exist_ok=True)
        self.config_path = self.local / "config.json"
        self.state_path = self.local / "state.json"
        self.journal_path = self.local / "restore-journal.json"

    @contextmanager
    def lock(self):
        import msvcrt
        with (self.local / "operation.lock").open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise HandoffError("另一个同步操作正在运行，请不要重复点击。") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def settings(self):
        config = read_json(self.config_path)
        if not config:
            raise HandoffError("请先运行初始化命令，见 README。")
        return config

    def key(self):
        raw = (self.local / "sync.key").read_bytes().strip()
        try:
            key = base64.urlsafe_b64decode(raw)
            if len(key) != 32:
                raise ValueError()
            return key
        except Exception as exc:
            raise HandoffError("本地同步密钥文件无效。") from exc

    def transport(self):
        config = self.settings()
        return GitTransport(self.local / "git-cache.git", config["remote"])

    def check_version(self):
        if importlib.metadata.version("deeptutor") != VERSION:
            raise HandoffError("当前安装版本不匹配，要求 deeptutor==1.7.0。")
        os.environ["DEEPTUTOR_HOME"] = str(self.app / "runtime")
        from deeptutor.runtime.launcher import _reset_runtime_singletons
        _reset_runtime_singletons()
        from deeptutor.services.pocketbase_client import is_pocketbase_enabled
        from deeptutor.services.config import load_auth_settings
        if is_pocketbase_enabled():
            raise HandoffError("当前版本不支持 PocketBase 远程数据库。")
        if load_auth_settings()["enabled"]:
            raise HandoffError("当前版本只支持关闭账户登录的本地单用户安装。")
        users = self.data / "users"
        if users.exists() and any(users.iterdir()):
            raise HandoffError("当前版本只支持本地单用户数据库。")

    def stop(self):
        activity = self.data / "user/.runtime/workspace-activity.sqlite3"
        guard = None
        try:
            if activity.exists():
                guard = sqlite3.connect(activity, timeout=0)
                guard.execute("BEGIN EXCLUSIVE")
            assert_idle(self.data)
            result = subprocess.run([sys.executable, str(self.app / "manage_deeptutor.py"), "stop"], timeout=30)
            if result.returncode:
                raise HandoffError("DeepTutor 没有正常停止，未执行数据替换。")
        except sqlite3.OperationalError as exc:
            raise HandoffError("学习数据仍被请求或后台任务使用，请完成或停止任务后重试。") from exc
        finally:
            if guard is not None:
                guard.close()
        import socket
        for port in (8001, 3782):
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise HandoffError("DeepTutor 端口仍被占用；未执行快照恢复。")

    def start(self):
        result = subprocess.run([sys.executable, str(self.app / "manage_deeptutor.py"), "start"], timeout=100)
        if result.returncode:
            raise HandoffError("快照已恢复，但应用启动失败。请检查本机 logs；不要重复覆盖数据。")

    def initialize(self, remote, device, key_file=None):
        if self.config_path.exists() or self.state_path.exists() or (self.local / "sync.key").exists():
            raise HandoffError("本机已初始化，不能覆盖现有密钥和同步状态。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", device):
            raise HandoffError("设备名请使用 office、home 等简短英文。")
        # Validate without doing network I/O or replacing user data.
        GitTransport(self.local / "git-cache.git", remote)
        if key_file:
            key = Path(key_file).read_bytes().strip()
            if len(base64.urlsafe_b64decode(key)) != 32:
                raise HandoffError("导入的同步密钥无效。")
        else:
            key = base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256))
        with (self.local / "sync.key").open("xb") as handle:
            handle.write(key + b"\n")
        if os.name == "nt":
            identity = os.environ.get("USERDOMAIN", "") + "\\" + os.environ["USERNAME"]
            protected = subprocess.run(
                ["icacls", str(self.local), "/inheritance:r", "/grant:r",
                 identity + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True,
            )
            if protected.returncode:
                raise HandoffError("无法限制本地密钥目录权限，已停止初始化，请让 Codex 检查。")
        atomic_json(self.config_path, {"remote": remote, "device": device, "branch": "deeptutor-sync"})
        print("初始化完成。密钥仅保存在 sync-local/sync.key，请通过 U 盘等方式单独带到另一台电脑。")

    def make_payload(self, stage, state):
        roots, files = collect(self.data, stage)
        manifest = {
            "format": 1, "deeptutor": VERSION, "data_path": str(self.data),
            "device": self.settings()["device"], "created_at": datetime.now(timezone.utc).isoformat(),
            "revision": uuid.uuid4().hex, "parent_commit": state.get("head"),
            "roots": roots, "files": files,
        }
        return encrypt_package(manifest, stage, self.key()), manifest

    def finish(self):
        self.check_version()
        self.settings()
        self.stop()
        state = read_json(self.state_path)
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.local) as directory:
            payload, manifest = self.make_payload(Path(directory), state)
        outbox = self.local / "outbox"
        outbox.mkdir(exist_ok=True)
        saved = outbox / (manifest["revision"] + ".bin")
        saved.write_bytes(payload)
        print("本地加密快照已保存：" + str(saved))
        transport = self.transport()
        remote = transport.head()
        if remote != state.get("head"):
            # Handles a previously uncertain successful push, without republishing.
            if remote and sha(transport.read(remote)) == sha(payload):
                head = remote
            else:
                raise HandoffError("远端有未接收的新快照，不能覆盖。请保留 outbox，由 Codex 对比两边记录。")
        else:
            head = transport.publish(payload, remote)
        atomic_json(self.state_path, {"head": head, "fingerprint": fingerprint(manifest["files"])})
        print("上传并读回校验成功。现在可以关闭电脑，到另一台电脑点击“继续学习”。")

    def local_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix="check-", dir=self.local) as directory:
            _, files = collect(self.data, Path(directory))
        return fingerprint(files)

    def has_learning_content(self):
        # Fresh installation can contain empty schema databases and empty scaffolding.
        for root in roots_for(self.data):
            for path in iter_files(confined(self.data, root)):
                if root == CATALOG:
                    if any(r.get("kind") not in {"system", "general"} for r in catalog_rows(self.data)):
                        return True
                    continue
                if database(path):
                    with closing(connect_read(path)) as conn:
                        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                            if table in {"reading_schema", "schema_version", "schema_migrations", "mastery_schema_migrations"}:
                                continue
                            quoted = '"' + table.replace('"', '""') + '"'
                            if conn.execute("SELECT 1 FROM " + quoted + " LIMIT 1").fetchone():
                                return True
                elif path.stat().st_size:
                    relative = path.relative_to(self.data).as_posix()
                    if relative.startswith("user/workspace/personas/") and path.name == "PERSONA.md":
                        from deeptutor.runtime.home import PACKAGE_ROOT
                        preset = PACKAGE_ROOT / "deeptutor/services/persona/presets" / path.parent.name / "PERSONA.md"
                        if preset.is_file() and preset.read_text(encoding="utf-8") == path.read_text(encoding="utf-8"):
                            continue
                    if relative == "user/workspace/suggestions/starters.json":
                        suggestion = read_json(path)
                        if (suggestion.get("status") == "no-material"
                                and not suggestion.get("suggestions") and not suggestion.get("shown")):
                            continue
                    return True
        return False

    def rollback(self):
        journal = read_json(self.journal_path)
        if not journal:
            return
        backup = Path(journal["backup"])
        backup.resolve().relative_to((self.local / "backups").resolve())
        for item in reversed(journal["items"]):
            target = confined(self.data, item["root"])
            old = confined(backup / "data", item["root"])
            # A saved old target proves that its move completed, even if the
            # journal write immediately after that move was interrupted.
            if old.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, target)
            elif not item["existed"] and target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        prior = journal.get("prior_state")
        if prior:
            atomic_json(self.state_path, prior)
        elif self.state_path.exists():
            self.state_path.unlink()
        self.journal_path.unlink()

    def replace(self, stage, manifest, head):
        roots = sorted(set(roots_for(self.data)) | set(manifest["roots"]))
        backup = self.local / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        journal = {"backup": str(backup), "prior_state": read_json(self.state_path), "items": []}
        # Complete intent list is persisted before any move. A power failure
        # requires rollback on the next command, never silent partial acceptance.
        expanded_roots = list(roots)
        for root in roots:
            if root.endswith((".db", ".sqlite3", ".sqlite")):
                expanded_roots.extend(root + suffix for suffix in ("-wal", "-shm"))
        for root in expanded_roots:
            target = confined(self.data, root)
            journal["items"].append({"root": root, "existed": target.exists()})
        atomic_json(self.journal_path, journal)
        try:
            for item in journal["items"]:
                root = item["root"]
                target, source = confined(self.data, root), confined(stage, root)
                if target.exists():
                    old = confined(backup / "data", root)
                    old.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, old)
                if source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, target)
                    else:
                        shutil.copy2(source, target)
            # Content-relative paths in catalog must still map to known roots.
            roots_for(self.data)
            atomic_json(self.state_path, {"head": head, "fingerprint": self.local_fingerprint()})
            self.journal_path.unlink()
        except BaseException:
            self.rollback()
            raise
        print("恢复前的本机数据备份：" + str(backup))

    def resume(self, open_app=True):
        self.check_version()
        transport = self.transport()
        head = transport.head()
        if not head:
            raise HandoffError("远端还没有快照，请先在另一台电脑点击“结束学习并同步”。")
        payload = transport.read(head)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=self.local) as directory:
            stage = Path(directory)
            manifest = unpack_package(payload, stage, self.key(), self.data)
            self.stop()
            state = read_json(self.state_path)
            if not state:
                # Generate this computer's own system configuration snapshot.
                # That directory is deliberately absent from the handoff.
                from deeptutor.services.setup import init_user_directories
                from deeptutor.services.config import ensure_runtime_settings_files
                from deeptutor.services.workspace import get_content_workspace_service
                init_user_directories(self.app / "runtime")
                ensure_runtime_settings_files()
                get_content_workspace_service().system_binding()
            if head == state.get("head"):
                print("已经接收这个快照，保留当前本机学习进度。")
            else:
                if state and self.local_fingerprint() != state.get("fingerprint"):
                    raise HandoffError("本机还有未上传的变化，未覆盖。请保留两边记录，由 Codex 帮你处理。")
                if not state and self.has_learning_content():
                    raise HandoffError("首次接收时发现本机已有学习数据，未覆盖。请先导出备份并由 Codex 对比。")
                self.replace(stage, manifest, head)
                print("快照校验和恢复成功：" + manifest["created_at"])
        if open_app:
            self.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = parser.add_subparsers(dest="action", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--remote", required=True)
    initialize.add_argument("--device", required=True)
    initialize.add_argument("--key-file", type=Path)
    sub.add_parser("finish")
    resume = sub.add_parser("resume")
    resume.add_argument("--no-start", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    helper = Handoff(args.app_root)
    try:
        with helper.lock():
            if helper.journal_path.exists():
                helper.stop()
                helper.rollback()
                raise HandoffError("上次恢复中断，已回滚到恢复前状态。请检查后重新执行。")
            if args.action == "init":
                helper.initialize(args.remote, args.device, args.key_file)
            elif args.action == "finish":
                helper.finish()
            elif args.action == "resume":
                helper.resume(not args.no_start)
            else:
                print(json.dumps({"configured": helper.config_path.exists(),
                                  "last_confirmed_commit": read_json(helper.state_path).get("head"),
                                  "key_present": (helper.local / "sync.key").exists()}, indent=2))
    except (HandoffError, OSError, ValueError, sqlite3.Error) as exc:
        print("同步未完成：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
