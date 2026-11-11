"""Administratively authorized GitHub Releases self-updates.

Only the pinned GitHub repo and a versioned ZIP + SHA256 sidecar are accepted.
The Windows detached bootstrapper is responsible for replacing code after this
process exits; runtime data, secrets and VM directories never enter the ZIP.
"""
from __future__ import annotations
import hashlib
import json
import uuid
import tempfile
import os
import shutil
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import zipfile

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
MAX_PACKAGE = 30 * 1024 * 1024
STATES = {"downloading", "verified", "handoff", "hot_applying", "preparing", "stopping", "backing_up", "applying", "restarting"}
HANDOFF_TIMEOUT_SECONDS = 90
HOT_PATHS = {"README.md", "VERSION"}
HOT_UI_EXTENSIONS = {".html", ".css", ".js"}


class UpdateError(RuntimeError):
    pass


# Only redirect to these GitHub-controlled hosts. Crucially, NEVER send a
# private token to a different host after an HTTP redirect.
DOWNLOAD_HOSTS = frozenset({
    "api.github.com", "github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "github-releases.githubusercontent.com",
})


class _RestrictedGitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS:
            raise UpdateError("GitHub 下载发生非预期重定向，已阻止")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and parsed.hostname != urlsplit(request.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def _numbers(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(version).strip())
    if not match:
        raise UpdateError("版本号格式不正确")
    return tuple(map(int, match.groups()))


def _get(url: str, token: str, max_bytes: int, accept: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise UpdateError("更新请求只能连接 GitHub 官方 API")
    headers = {"Accept": accept, "User-Agent": "VM-Orchestrator-Self-Updater/1.4.50",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(url, headers=headers)
    try:
        # Validate every redirect BEFORE connecting, not merely the final URL.
        opener = build_opener(_RestrictedGitHubRedirect())
        with opener.open(req, timeout=25) as response:
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in DOWNLOAD_HOSTS:
                raise UpdateError("更新下载来源未经认可")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise UpdateError("更新包超过大小限制")
            return payload
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise UpdateError("GitHub Release 不可读取：请检查私有仓库令牌及 Release 是否已发布") from exc
        raise UpdateError(f"GitHub API HTTP {exc.code}") from exc
    except URLError as exc:
        raise UpdateError("GitHub 网络连接失败") from exc


class UpdateService:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.current = (self.project_root / "VERSION").read_text(encoding="utf-8").strip()
        # Immutable process-start version: an overwritten VERSION file is not proof
        # that the replacement Python process has actually started.
        self.started_version = self.current
        self.started_at = time.time()
        self.repo = os.environ.get("VMO_UPDATE_REPO", "popowk/VM-Orchestrator").strip()
        self.token = os.environ.get("VMO_UPDATE_GITHUB_TOKEN", "").strip()
        self.work = self.project_root / "data" / "updates"
        self.status_file = self.work / "status.json"
        self._lock = threading.Lock()
        self._release = None
        if not REPO_RE.fullmatch(self.repo) or self.repo.startswith(".") or ".." in self.repo:
            raise UpdateError("VMO_UPDATE_REPO 必须是 owner/repository 格式")

    def _state(self, **fields):
        self.work.mkdir(parents=True, exist_ok=True)
        info = {"state": fields.pop("state"), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
        tmp = self.status_file.with_name("status-" + uuid.uuid4().hex + ".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.status_file)
        return info

    def status(self):
        details = {"state": "idle"}
        try:
            details = json.loads(self.status_file.read_text(encoding="utf-8-sig"))
            if not isinstance(details, dict):
                details = {"state": "idle"}
        except (OSError, ValueError, TypeError):
            pass
        # A recorded 'success' is historical evidence, not proof of the
        # currently serving Python process. Check all three versions on
        # every read. A hot-only UI update may change self.current without
        # restarting; a restart-mode update must have started new code.
        try:
            disk_version = (self.project_root / "VERSION").read_text(
                encoding="utf-8-sig").strip()
        except OSError:
            disk_version = ""
        intended = str(details.get("version", ""))
        if details.get("state") == "success":
            consistent = (bool(intended) and intended == self.current == disk_version
                          and (details.get("mode") != "restart"
                               or self.started_version == intended))
            if not consistent:
                details = {**details, "state": "version_mismatch",
                           "message": ("更新记录显示成功，但版本不一致：运行 "
                                       + self.current + "，磁盘 " + (disk_version or "未知")
                                       + "，目标 " + (intended or "未知")
                                       + "。请检查实际启动实例和安装目录，不要重复执行更新。")}
        elif details.get("state") == "handoff":
            try:
                age = time.time() - self.status_file.stat().st_mtime
                if age > HANDOFF_TIMEOUT_SECONDS:
                    # This is NOT proof that update failed; the detached worker
                    # might have succeeded despite delayed status visibility.
                    details = {**details, "state": "unconfirmed",
                               "message": "更新器接管状态尚未确认，请核对当前版本及更新日志；不要重复发起安装"}
            except OSError:
                pass
        details = {k: details.get(k) for k in
                   ("state", "at", "version", "message", "backup", "pid", "mode", "attempt", "verified_host_pid")
                   if k in details}
        return {"current": self.current, "disk_version": disk_version,
                "started_version": self.started_version, "repository": self.repo,
                "configured_token": bool(self.token), "update": details,
                "latest": (self._release or {}).get("tag_name")}

    def diagnostics(self):
        """Return this attempt's logs, never confuse legacy logs with new failures."""
        details = self.status().get("update", {})
        attempt = str(details.get("attempt", ""))
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            return {"attempt": attempt or None, "logs": {
                "current-attempt": "本次更新没有独立日志编号。下方历史成功日志不能说明本次更新成功。",
            }, "legacy_available": any((self.work / n).exists() for n in
                                      ("updater.log", "updater-bootstrap.log"))}
        out = {}
        for name in (f"updater-{attempt}.log", f"updater-bootstrap-{attempt}.log"):
            path = self.work / name
            try:
                with path.open("rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 12000))
                    value = f.read().decode("utf-8-sig", "replace")[-8000:]
                value = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", value)
                value = re.sub(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{10,}", "[REDACTED]", value)
                out[name] = value
            except OSError:
                out[name] = "本次尝试尚未生成此日志；不能用旧版本日志判断结果。"
        return {"attempt": attempt, "logs": out}

    def _latest(self):
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        data = _get(url, self.token, 512 * 1024, "application/vnd.github+json")
        try:
            release = json.loads(data)
            tag = str(release["tag_name"])
            version = tag.removeprefix("v")
            _numbers(version)
            if release.get("draft") or release.get("prerelease"):
                raise UpdateError("不允许自动安装草稿或预览版")
            file_name = f"VM-Orchestrator-v{version}.zip"
            assets = {a.get("name"): a for a in release["assets"] if isinstance(a, dict)}
            zip_asset = assets.get(file_name)
            checksum_asset = assets.get(file_name + ".sha256")
            if not zip_asset or not checksum_asset:
                raise UpdateError("Release 缺少对应 ZIP 或 .sha256 校验文件")
            for asset in (zip_asset, checksum_asset):
                if asset.get("state") != "uploaded" or not isinstance(asset.get("id"), int):
                    raise UpdateError("Release 中包含不可用的安装资源")
            return release, version, zip_asset, checksum_asset
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub Release 元数据异常") from exc

    def check(self):
        release, version, _, _ = self._latest()
        self._release = {"tag_name": release["tag_name"]}
        return {**self.status(), "latest": version,
                "available": _numbers(version) > _numbers(self.current),
                "published_at": release.get("published_at"),
                "release_url": release.get("html_url", ""),
                "notes": str(release.get("body") or "")[:4000]}

    def _download_asset(self, asset_id: int, size: int) -> bytes:
        return _get(f"https://api.github.com/repos/{self.repo}/releases/assets/{asset_id}",
                    self.token, size, "application/octet-stream")

    def install(self, version: str):
        with self._lock:
            if self.status().get("update", {}).get("state") == "version_mismatch":
                raise UpdateError("运行与磁盘版本不一致，请先修复安装状态再重试")
            if self._state_active():
                raise UpdateError("已经有一个更新正在进行")
            if _numbers(version) <= _numbers(self.current):
                raise UpdateError("只能升级到更高版本")
            attempt = uuid.uuid4().hex
            self._state(state="downloading", version=version, attempt=attempt,
                        message="正在获取 GitHub Release")
            threading.Thread(target=self._worker, args=(version, attempt), daemon=True).start()
        return {"accepted": True, "version": version}

    def _state_active(self):
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
            # A crashed updater should not lock out a machine forever.
            if data.get("state") not in STATES:
                return False
            age = time.time() - self.status_file.stat().st_mtime
            # Stale in-progress status requires explicit host repair, not a
            # second attempt while files may still be replaced.
            return data.get("state") in STATES
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_hot_path(path: str) -> bool:
        if path in HOT_PATHS:
            return True
        return (path.startswith("app/ui/") and
                Path(path).suffix.lower() in HOT_UI_EXTENSIONS and
                len(Path(path).parts) == 3)

    def _plan(self, package: zipfile.ZipFile, version: str):
        """Use a full byte comparison; do not infer safety from version labels.

        Backend/Agent/dependency changes force a verified restart. UI/README
        changes can be atomically replaced while Python keeps serving requests.
        """
        prefix = f"VM-Orchestrator-v{version}/"
        changes = []
        for item in package.infolist():
            if item.is_dir():
                continue
            rel = item.filename[len(prefix):]
            target = self.project_root.joinpath(*rel.split("/"))
            if not target.resolve().is_relative_to(self.project_root) or target.is_symlink():
                raise UpdateError("更新目标存在符号链接或重解析路径，拒绝热更新")
            incoming = package.read(item)
            try:
                same = target.is_file() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(incoming).digest()
            except OSError as exc:
                raise UpdateError("无法读取现有程序文件，拒绝热更新") from exc
            if not same:
                changes.append(rel)
        # Also check deletions. A removed backend or Guest file must never be
        # interpreted as a UI-only update just because the ZIP omits it.
        packaged = {i.filename[len(prefix):] for i in package.infolist() if not i.is_dir()}
        for managed in ("app", "guest_agent", "scripts", "integrations"):
            local_dir = self.project_root / managed
            if not local_dir.is_dir():
                continue
            for local_file in local_dir.rglob("*"):
                if not local_file.is_file() or "__pycache__" in local_file.parts:
                    continue
                rel = local_file.relative_to(self.project_root).as_posix()
                if (rel not in packaged and local_file.suffix.lower() in
                        (".py", ".ps1", ".cmd", ".bat", ".php", ".js", ".css", ".html")):
                    changes.append(rel)
        hot = bool(changes) and all(self._is_hot_path(path) for path in changes)
        return ("hot" if hot else "restart"), changes

    def _hot_apply(self, package: zipfile.ZipFile, version: str, changed: list[str], attempt: str):
        """Atomic per-file static swap with rollback. Never modify Python code."""
        backup_dir = self.work / "backups" / ("hot-v" + version + "-" + attempt[:8])
        backup_dir.mkdir(parents=True, exist_ok=False)
        originals: list[tuple[Path, bytes | None]] = []
        prefix = f"VM-Orchestrator-v{version}/"
        self._state(state="hot_applying", version=version, mode="hot", attempt=attempt,
                    message="仅页面资源变化：正在热替换（无需中断 Host/WSS）")
        try:
            # VERSION is written last; a failed swap must never claim success.
            order = sorted((p for p in changed if p != "VERSION")) + (["VERSION"] if "VERSION" in changed else [])
            for rel in order:
                if not self._is_hot_path(rel):
                    raise UpdateError("热更新白名单校验失败")
                target = self.project_root.joinpath(*rel.split("/"))
                if not target.resolve().is_relative_to(self.project_root) or target.is_symlink():
                    raise UpdateError("热更新目标包含不安全路径")
                original = target.read_bytes() if target.exists() else None
                originals.append((target, original))
                snapshot = backup_dir.joinpath(*rel.split("/"))
                if original is not None:
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.write_bytes(original)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix=".vmo-hot-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(package.read(prefix + rel))
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
            self.current = version
            self._state(state="success", version=version, mode="hot", attempt=attempt,
                        backup=str(backup_dir), message="静态资源热更新成功；Host、WSS 均未重启，请刷新网页")
        except Exception as exc:
            self.current = (self.project_root / "VERSION").read_text(encoding="utf-8").strip()
            for target, original in reversed(originals):
                try:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        fd, temp = tempfile.mkstemp(prefix=".vmo-rollback-", dir=target.parent)
                        with os.fdopen(fd, "wb") as f:
                            f.write(original)
                        os.replace(temp, target)
                except OSError:
                    pass
            raise UpdateError("热更新失败，已尝试恢复页面资源：" + str(exc)[:120]) from exc

    def _worker(self, expected_version: str, attempt: str):
        try:
            release, version, archive_asset, sha_asset = self._latest()
            if version != expected_version or _numbers(version) <= _numbers(self.current):
                raise UpdateError("下载过程中 Release 已变化，请重新检查")
            name = f"VM-Orchestrator-v{version}.zip"
            sha_raw = self._download_asset(sha_asset["id"], 4096).decode("utf-8", "strict").strip()
            match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?" + re.escape(name), sha_raw)
            if not match:
                raise UpdateError("SHA256 校验清单内容不符合预期")
            expected_hash = match.group(1).lower()
            github_digest = archive_asset.get("digest")
            if github_digest and github_digest != "sha256:" + expected_hash:
                raise UpdateError("Release 资源摘要与 SHA256 清单不一致")
            if isinstance(archive_asset.get("size"), int) and archive_asset["size"] > MAX_PACKAGE:
                raise UpdateError("GitHub 资源大于 30MB 上限")
            archive = self._download_asset(archive_asset["id"], MAX_PACKAGE)
            actual_hash = hashlib.sha256(archive).hexdigest()
            if actual_hash != expected_hash:
                raise UpdateError("下载包 SHA256 不匹配，已拒绝安装")
            stage = self.work / ("stage-v" + version)
            stage.mkdir(parents=True, exist_ok=True)
            zip_path = stage / name
            zip_path.write_bytes(archive)
            # Validate ZIP paths before invoking PowerShell or ending the service.
            with zipfile.ZipFile(zip_path) as z:
                entries = z.namelist()
                prefix = "VM-Orchestrator-v" + version + "/"
                if (not entries or len(entries) > 500 or
                    sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024 or
                    any(not n.startswith(prefix) or "\\" in n or
                        any(part in ("", ".", "..") for part in n[len(prefix):].rstrip("/").split("/"))
                        for n in entries if n != prefix)):
                    raise UpdateError("ZIP 文件目录结构或解压大小不安全")
                required = ["VERSION", "app/main.py", "Start-VM-Orchestrator.cmd", "Install-All.ps1"]
                if any(prefix + n not in entries for n in required):
                    raise UpdateError("更新包缺少核心文件")
                if z.read(prefix + "VERSION").decode("utf-8").strip() != version:
                    raise UpdateError("版本清单与 GitHub Release 不一致")
                # Refuse runtime state and symlinks in a release.
                if any(n[len(prefix):].startswith(("data/", ".git/", "backup-before-")) for n in entries):
                    raise UpdateError("更新包包含禁止覆盖的数据目录")
                if any((i.external_attr >> 16) & 0o170000 == 0o120000 for i in z.infolist()):
                    raise UpdateError("更新包包含不允许的符号链接")
            with zipfile.ZipFile(zip_path) as package:
                mode, changed = self._plan(package, version)
                if mode == "hot":
                    self._hot_apply(package, version, changed, attempt)
                    return
            if os.name != "nt":
                raise UpdateError("涉及后端的更新仅允许在 Windows 宿主机安装")
            script = self.project_root / "scripts" / "host" / "Apply-GitHubUpdate.ps1"
            if not script.is_file():
                raise UpdateError("缺少 Windows 自更新脚本")
            self._state(state="verified", version=version, mode="restart", attempt=attempt,
                        message="包含后端变更：完整性校验通过，准备安全重启")
            # PowerShell must run OUTSIDE scripts/, because that directory is
            # replaced in-place during upgrade. Capture early parser/startup
            # errors even if the updater never reaches its own logger.
            runner = self.work / ("Apply-GitHubUpdate-v" + version + ".ps1")
            # Windows PowerShell 5.1 treats UTF-8 without BOM as ANSI and
            # can fail before the updater writes its first log entry.
            try:
                runner_code = script.read_bytes().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise UpdateError("PowerShell updater is not valid UTF-8") from exc
            runner.write_bytes(bytes((0xEF, 0xBB, 0xBF)) + runner_code.encode("utf-8"))
            bootstrap_log = self.work / ("updater-bootstrap-" + attempt + ".log")
            acknowledgement = self.work / ("updater-ack-" + attempt + ".txt")
            try:
                acknowledgement.unlink(missing_ok=True)
            except OSError as exc:
                raise UpdateError("Unable to reset updater acknowledgement") from exc

            # Use standard, inspectable script-file invocation instead of
            # an encoded command. Endpoint-security policies can explicitly
            # audit/approve the VMO helper and updater by their file paths.
            # Do not disable security products or alter their trust settings.
            preflight = self.project_root / "scripts" / "host" / "Test-GitHubUpdate.ps1"
            if not preflight.is_file():
                raise UpdateError("缺少本地 PowerShell 预检脚本；未停止 Host")
            syntax_cmd = [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "RemoteSigned", "-File", str(preflight),
                "-UpdaterPath", str(runner),
            ]
            try:
                syntax_result = subprocess.run(
                    syntax_cmd, cwd=str(self.project_root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise UpdateError("PowerShell 预检超时；请检查火绒防护日志或终端策略（Host 未停止）") from exc
            except OSError as exc:
                code = getattr(exc, "winerror", None)
                suffix = "（Windows 错误码 " + str(code) + "）" if code else ""
                raise UpdateError("Windows 未允许启动 PowerShell 预检" + suffix +
                                  "；请核对火绒拦截记录（Host 未停止）") from exc
            if syntax_result.returncode != 0:
                bootstrap_log.write_bytes((syntax_result.stdout or b"")[-4000:])
                raise UpdateError(
                    "PowerShell 预检失败（返回码 " + str(syntax_result.returncode) +
                    "）；请检查本次 bootstrap 日志和火绒拦截记录（Host 未停止）")

            # Detached updater creates its own ACK immediately on startup;
            # no wrapper, hidden expression or encoded bootstrap is needed.
            cmd = [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "RemoteSigned", "-File", str(runner),
                "-ZipPath", str(zip_path), "-ExpectedSha256", str(expected_hash),
                "-Target", str(self.project_root), "-HostPid", str(os.getpid()),
                "-Version", str(version), "-Attempt", str(attempt),
            ]
            self._state(state="handoff", version=version, mode="restart", attempt=attempt,
                        message="已通过 PowerShell 语法预检，等待独立更新进程接管")
            with open(bootstrap_log, "wb", buffering=0) as log:
                try:
                    proc = subprocess.Popen(
                        cmd, cwd=str(self.project_root), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP |
                                       subprocess.CREATE_NO_WINDOW)
                    )
                except OSError as exc:
                    code = getattr(exc, "winerror", None)
                    suffix = "（Windows 错误码 " + str(code) + "）" if code else ""
                    raise UpdateError("Windows 未允许启动独立更新进程" + suffix +
                                      "；请检查火绒拦截日志（Host 未停止）") from exc
            # If the child exits before writing ACK, show a definite error;
            # do not misrepresent an old updater.log as evidence of success.
            for _ in range(30):
                if acknowledgement.is_file() or proc.poll() is not None:
                    break
                time.sleep(0.2)
            if not acknowledgement.is_file() and proc.poll() is not None:
                try:
                    last = json.loads(self.status_file.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    last = {}
                if last.get("attempt") == attempt and last.get("state") == "handoff":
                    self._state(state="failed", version=version, mode="restart",
                                attempt=attempt,
                                message="PowerShell exited before startup acknowledgement; check this attempt's bootstrap log")
            # Once ACK exists, the independent process owns the update. No
            # further writes here may overwrite preparing/success/rollback.
        except Exception as exc:
            # Never clobber a detached updater's definitive state.
            try:
                observed = json.loads(self.status_file.read_text(encoding="utf-8-sig"))
                if (observed.get("attempt") != attempt or observed.get("state") not in
                        ("success", "rolled_back", "preparing", "stopping", "backing_up", "applying", "restarting")):
                    self._state(state="failed", version=expected_version, mode="restart", attempt=attempt,
                                message=str(exc)[:250])
            except (OSError, ValueError):
                self._state(state="failed", version=expected_version, mode="restart", attempt=attempt,
                            message=str(exc)[:250])
