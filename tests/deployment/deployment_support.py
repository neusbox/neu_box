"""实机验收（第 3 层）的共享支撑。

这一层跑在装了 worker RPM 的部署机上：真 Worker 进程、真设备、真容器，
通过 ``neuboxctl test`` 调起。它**不是**开发机上的便利测试 ——
缺任何前置条件都直接失败并把缺什么写清楚，没有 skip。

模块内容：
  * :class:`HttpResult` / :class:`WorkerClient` —— 不依赖第三方库的 HTTP 客户端
  * :class:`Deployment` —— 夹具与用例共用的操作集合（任务、设备、沙盒、
    进程、Docker、服务停/起）

所有 HTTP 请求都显式绕过代理（等价于 curl 的 ``--noproxy '*'``）：部署机
常见 http_proxy 设置会把发往 127.0.0.1 的请求送进代理。
"""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

DEFAULT_URL = "http://127.0.0.1:59075"
DEFAULT_CONFIG = "/etc/neu-box/worker.env"
DEFAULT_MANIFEST = "/usr/share/neu-box/manifest.json"
DEFAULT_SERVICE = "neuboxd.service"
# 起服走 `neuboxctl setup`；RPM 在 /usr/sbin 装了这个符号链接。
DEFAULT_CTL = "/usr/sbin/neuboxctl"
DEFAULT_DEVICE_FILTER = "davinci[0-9]+"
DEFAULT_REAPER_INTERVAL = 30.0

# 单个 HTTP 请求的上限；任务与 Reaper 的等待用各自的 deadline 控制。
HTTP_TIMEOUT = 30.0
TASK_TIMEOUT = 180.0
REAPER_TIMEOUT = 240.0
POLL_INTERVAL = 1.0

# open(2) 探测的结果：设备节点在沙盒外的可见性判定。
OPEN_OK = 0
OPEN_DENIED = 1
OPEN_INCONCLUSIVE = 2
OPEN_TIMEOUT = 3

PROBE_SCRIPT_NAME = "probe_device_open.sh"

# 沙盒内探测脚本：$1 = 要探测的设备节点。
#
# 用 bash + coreutils timeout（两种错误消息都认：LC_ALL=C 下应当是英文，但
# 嵌入式环境的 libc 消息目录不一定完整）。输出 INSIDE_OPEN_* 标记供调用方在
# 任务日志里断言；退出码同时表达结论：
#   0 = 打开成功，1 = 权限类错误（设备授权生效），2 = 无从判断，3 = 超时。
PROBE_SCRIPT = """#!/bin/bash
node="$1"
message="$(LC_ALL=C timeout 10 bash -c 'exec 3<>"$1"' _ "$node" 2>&1)"
rc=$?
if [ "$rc" -eq 0 ]; then echo INSIDE_OPEN_OK; exit 0; fi
if [ "$rc" -eq 124 ]; then echo INSIDE_OPEN_TIMEOUT; exit 3; fi
case "$message" in
    *'Operation not permitted'*|*'Permission denied'*|\\
    *'不允许的操作'*|*'权限不够'*)
        echo INSIDE_OPEN_DENIED; exit 1 ;;
esac
echo "INSIDE_OPEN_INCONCLUSIVE rc=$rc ${message:-}"
exit 2
"""


class WorkerUnreachable(RuntimeError):
    """Worker 的 HTTP 接口当前不可达。"""


class HttpResult:
    """一次 HTTP 往返；非 2xx 不抛异常，交给调用方断言。"""

    __slots__ = ("status", "text", "url")

    def __init__(self, status: int, text: str, url: str):
        self.status = status
        self.text = text
        self.url = url

    def json(self):
        """解析 JSON 响应体；响应体不是 JSON 时 pytest.fail。"""
        try:
            return json.loads(self.text)
        except ValueError:
            pytest.fail(
                f"{self.url} 返回的响应体不是 JSON（HTTP {self.status}）:\n"
                f"{self.text[:2000]}",
                pytrace=False,
            )

    def value(self, key: str):
        """取 JSON 对象里的字段；缺字段时 pytest.fail（不返回 None 糊过去）。"""
        payload = self.json()
        if not isinstance(payload, dict) or key not in payload:
            pytest.fail(
                f"{self.url} 的响应缺少字段 {key!r}（HTTP {self.status}）:\n"
                f"{self.text[:2000]}",
                pytrace=False,
            )
        return payload[key]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<HttpResult {self.status} {self.url} {self.text[:200]!r}>"


class WorkerClient:
    """Worker HTTP 客户端；只依赖标准库，便于 PyInstaller 冻结。"""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = HTTP_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout
        # 空 ProxyHandler 关掉环境变量代理，等价于 curl --noproxy '*'。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(
        self,
        method: str,
        path: str,
        payload=None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> HttpResult:
        url = self.url + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                return HttpResult(response.status, response.read().decode("utf-8", "replace"), url)
        except urllib.error.HTTPError as exc:
            return HttpResult(exc.code, exc.read().decode("utf-8", "replace"), url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise WorkerUnreachable(f"{method} {url} 失败: {reason}") from exc

    def get(self, path: str, **kwargs) -> HttpResult:
        return self._call("GET", path, **kwargs)

    def post(self, path: str, payload=None, **kwargs) -> HttpResult:
        return self._call("POST", path, payload=payload, **kwargs)

    def delete(self, path: str, payload=None, **kwargs) -> HttpResult:
        return self._call("DELETE", path, payload=payload, **kwargs)

    def request(self, method: str, path: str, payload=None, **kwargs) -> HttpResult:
        """任意方法的请求；用于验证"已经删掉的路由确实 404"。"""
        return self._call(method, path, payload=payload, **kwargs)

    # ── 具体端点 ────────────────────────────────────────────────

    def healthz(self) -> HttpResult:
        return self.get("/healthz")

    def root(self) -> HttpResult:
        return self.get("/")

    def node_status(self) -> HttpResult:
        return self.get("/status")

    def maintenance(self) -> HttpResult:
        return self.get("/maintenance")

    def list_tasks(self) -> HttpResult:
        return self.get("/tasks")

    def task(self, task_id: str) -> HttpResult:
        return self.get(f"/tasks/{task_id}")

    def task_log(self, task_id: str, **params) -> HttpResult:
        return self.get(f"/tasks/{task_id}/log", params=params or None)

    def create_task(self, payload: dict) -> HttpResult:
        return self.post("/tasks", payload)

    def delete_tasks(self, task_ids: list[str]) -> HttpResult:
        return self.delete("/tasks", {"task_ids": list(task_ids)})

    def acquire(self, payload: dict) -> HttpResult:
        return self.post("/sandbox/acquire", payload)

    def acquire_status(self, request_id: str) -> HttpResult:
        return self.get(f"/sandbox/acquire/{request_id}")

    def release(self, sandbox_name: str, **kwargs) -> HttpResult:
        """释放沙盒；销毁要收容器，可能比普通请求慢，允许调用方放宽超时。"""
        return self.post("/sandbox/release", {"sandbox_name": sandbox_name}, **kwargs)

    def dev_list(self, username: str | None = None) -> HttpResult:
        params = {"username": username} if username else None
        return self.get("/sandbox/list", params=params)

    def sandbox_status(self, **params) -> HttpResult:
        return self.get("/sandbox/status", params=params)

    def pause(self) -> HttpResult:
        return self.post("/maintenance/pause", {})

    def resume(self) -> HttpResult:
        return self.post("/maintenance/resume", {})

    def register_container(self, payload: dict) -> HttpResult:
        return self.post("/container/register", payload)


def read_env_file(path: str) -> dict[str, str]:
    """读 RPM 装的 KEY=VALUE 配置文件；文件不存在返回空字典。"""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.readlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Deployment:
    """夹具与用例共用的操作集合。

    所有方法只做"做这件事并返回结果"，判定放在用例里 —— 这样失败信息能
    带上用例自己的上下文。
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        user: str = "",
        *,
        config: str = DEFAULT_CONFIG,
        manifest: str = DEFAULT_MANIFEST,
        service: str = DEFAULT_SERVICE,
        ctl: str = DEFAULT_CTL,
        task_timeout: float = TASK_TIMEOUT,
        reaper_timeout: float = REAPER_TIMEOUT,
        poll: float = POLL_INTERVAL,
    ):
        self.url = url.rstrip("/")
        self.client = WorkerClient(self.url)
        self.user = user or current_user()
        self.config_path = config
        self.manifest_path = manifest
        self.service = service
        # 服务停/起用的可执行文件：停服只需 systemctl（读 MainPID），起服要
        # `neuboxctl setup`。注意 setup/pause 内部的单元名是产品硬编码的
        # `neuboxd.service`，--deployment-service 改的是本套件的观察对象。
        self.ctl_binary = ctl
        self.task_timeout = float(task_timeout)
        self.reaper_timeout = float(reaper_timeout)
        self.poll = float(poll)
        self.tempdir = tempfile.mkdtemp(prefix="neu-box-deployment-")
        os.chmod(self.tempdir, 0o777)
        self._probe_script = os.path.join(self.tempdir, PROBE_SCRIPT_NAME)
        with open(self._probe_script, "w", encoding="utf-8") as stream:
            stream.write(PROBE_SCRIPT)
        os.chmod(self._probe_script, 0o755)
        self.created_tasks: list[str] = []
        self.created_sandboxes: list[str] = []
        self.created_containers: list[str] = []
        self._children: list[subprocess.Popen] = []
        self._config = None

    # ── 会话收尾 ────────────────────────────────────────────────

    def cleanup(self) -> None:
        """尽力回收本会话造出来的东西；失败只报告，不覆盖用例结论。"""
        for container in self.created_containers:
            self.remove_container(container)
        for process in self._children:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            except BaseException:
                pass
        for name in list(self.created_sandboxes):
            try:
                self.client.release(name)
            except BaseException:
                pass
        if self.created_tasks:
            try:
                self.client.delete_tasks(list(self.created_tasks))
            except BaseException:
                pass
        shutil.rmtree(self.tempdir, ignore_errors=True)

    # ── Worker 配置 ─────────────────────────────────────────────

    @property
    def config(self) -> dict[str, str]:
        if self._config is None:
            self._config = read_env_file(self.config_path)
        return self._config

    @property
    def device_filter(self) -> str:
        return self.config.get("NEU_BOX_DEVICE_FILTER") or DEFAULT_DEVICE_FILTER

    @property
    def reaper_interval(self) -> float:
        """收尸周期，秒；默认与 Worker 的 NEU_BOX_SANDBOX_REAPER_INTERVAL 一致。"""
        raw = self.config.get("NEU_BOX_SANDBOX_REAPER_INTERVAL") or ""
        try:
            return max(1.0, float(raw))
        except ValueError:
            return float(DEFAULT_REAPER_INTERVAL)

    def manifest(self) -> dict:
        try:
            with open(self.manifest_path, encoding="utf-8") as stream:
                return json.load(stream)
        except OSError as exc:
            pytest.fail(
                f"读不到安装清单 {self.manifest_path}: {exc}；"
                "该文件由 worker RPM 安装，缺失说明装包不完整",
                pytrace=False,
            )
        except ValueError as exc:
            pytest.fail(f"{self.manifest_path} 不是合法 JSON: {exc}", pytrace=False)

    # ── 任务 ────────────────────────────────────────────────────

    def task_payload(
        self,
        command: str,
        *,
        device_num: int = 0,
        device_ids: list | None = None,
        cpu: int = 0,
        memory: int = 0,
        mem_unit: str = "GB",
        priority: int = 0,
        est_time: int = 0,
        user: str | None = None,
        target: dict | None = None,
    ) -> dict:
        payload = {
            "user_id": user or self.user,
            "command": command,
            "device_num": device_num,
            "cpu": cpu,
            "memory": memory,
            "mem_unit": mem_unit,
            "priority": priority,
            "est_time": est_time,
        }
        if device_ids:
            payload["device_num"] = 0
            payload["device_ids"] = [str(value) for value in device_ids]
        if target is not None:
            payload["target"] = target
        return payload

    def submit(self, command: str, **kwargs) -> str:
        """提交任务并返回 task_id；非 202 直接失败。"""
        payload = self.task_payload(command, **kwargs)
        result = self.client.create_task(payload)
        if result.status != 202:
            pytest.fail(
                f"提交任务被拒绝（HTTP {result.status}）: {result.text[:1000]}\n"
                f"请求体: {json.dumps(payload, ensure_ascii=False)}",
                pytrace=False,
            )
        task_id = result.value("task_id")
        self.created_tasks.append(task_id)
        return task_id

    def get_task(self, task_id: str) -> dict:
        result = self.client.task(task_id)
        if result.status != 200:
            pytest.fail(
                f"查询任务 {task_id} 失败（HTTP {result.status}）: {result.text[:1000]}",
                pytrace=False,
            )
        return result.json()

    def wait_task(self, task_id: str, *, timeout: float | None = None) -> dict:
        """等任务进入终态（completed / failed）并返回它的完整表示。"""
        deadline = time.time() + (timeout or self.task_timeout)
        state = ""
        while time.time() < deadline:
            task = self.get_task(task_id)
            state = task.get("status", "")
            if state in {"completed", "failed"}:
                return task
            time.sleep(self.poll)
        pytest.fail(
            f"任务 {task_id} 在 {timeout or self.task_timeout:.0f}s 内没有结束"
            f"（当前状态 {state or 'unknown'}）",
            pytrace=False,
        )

    def wait_task_running(self, task_id: str, *, timeout: float | None = None) -> dict:
        """等任务进入 running，返回此刻的状态表示（含已分配设备）。"""
        deadline = time.time() + (timeout or self.task_timeout)
        state = ""
        while time.time() < deadline:
            task = self.get_task(task_id)
            state = task.get("status", "")
            if state == "running":
                return task
            if state in {"completed", "failed"}:
                pytest.fail(
                    f"任务 {task_id} 在观察到 running 之前就结束了（状态 {state}）；"
                    f"日志:\n{self.task_log_text(task_id)[:2000]}",
                    pytrace=False,
                )
            time.sleep(self.poll)
        pytest.fail(
            f"任务 {task_id} 在 {timeout or self.task_timeout:.0f}s 内没有进入 running"
            f"（当前状态 {state or 'unknown'}）",
            pytrace=False,
        )

    def task_log_text(self, task_id: str, **params) -> str:
        result = self.client.task_log(task_id, raw=1, **params)
        if result.status != 200:
            pytest.fail(
                f"读取任务 {task_id} 日志失败（HTTP {result.status}）: {result.text[:1000]}",
                pytrace=False,
            )
        return result.text

    def wait_log_contains(self, task_id: str, marker: str, timeout: float = 60.0) -> str:
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            text = self.task_log_text(task_id)
            if marker in text:
                return text
            time.sleep(self.poll)
        pytest.fail(
            f"任务 {task_id} 的日志在 {timeout:.0f}s 内没有出现 {marker!r}；"
            f"当前日志:\n{text[:2000]}",
            pytrace=False,
        )

    def probe_command(self, node: str, hold: float = 0.0) -> str:
        """构造一条"在沙盒里探测设备节点"的任务命令。

        ``hold`` 是打印结论之后再存活几秒 —— 调用方需要趁任务还在沙盒里的
        时候从外面做对照探测。
        """
        command = f"bash {shlex.quote(self._probe_script)} {shlex.quote(node)}"
        if hold > 0:
            command += f"; rc=$?; sleep {hold}; exit $rc"
        return command

    def queue(self) -> list[dict]:
        """``GET /tasks`` 的队列快照（running 在前，然后是按出队顺序的 queued）。"""
        result = self.client.list_tasks()
        if result.status != 200:
            pytest.fail(
                f"GET /tasks 失败（HTTP {result.status}）: {result.text[:1000]}",
                pytrace=False,
            )
        return result.json().get("queue") or []

    def queue_entry(self, task_id: str) -> dict | None:
        for entry in self.queue():
            if entry.get("task_id") == task_id:
                return entry
        return None

    # ── 节点状态 / 设备 ─────────────────────────────────────────

    def status(self) -> dict:
        result = self.client.node_status()
        if result.status != 200:
            pytest.fail(
                f"GET /status 失败（HTTP {result.status}）: {result.text[:1000]}",
                pytrace=False,
            )
        return result.json()

    def idle_minors(self) -> list[int]:
        """当前空闲设备的 minor 列表，升序。"""
        dev_status = self.status().get("dev_status") or {}
        return sorted(int(minor) for minor, busy in dev_status.items() if not busy)

    def total_devices(self) -> int:
        return int(self.status().get("total_devices") or 0)

    def idle_devices(self) -> int:
        return int(self.status().get("idle_devices") or 0)

    def wait_idle_at_least(self, expected: int, timeout: float = 60.0) -> None:
        """等空闲设备恢复到 expected 张以上；用于确认设备真的被释放。"""
        deadline = time.time() + timeout
        idle = -1
        while time.time() < deadline:
            idle = self.idle_devices()
            if idle >= expected:
                return
            time.sleep(self.poll)
        pytest.fail(
            f"设备未在 {timeout:.0f}s 内恢复到至少 {expected} 张空闲"
            f"（当前 {idle} 张）；可能有沙盒没有释放",
            pytrace=False,
        )

    def wait_idle_at_most(self, expected: int, timeout: float = 30.0) -> None:
        """等空闲设备降到 expected 张以下；用于确认设备真的被占住了。"""
        deadline = time.time() + timeout
        idle = -1
        while time.time() < deadline:
            idle = self.idle_devices()
            if idle <= expected:
                return
            time.sleep(self.poll)
        pytest.fail(
            f"设备没有在 {timeout:.0f}s 内降到 {expected} 张空闲以下"
            f"（当前 {idle} 张）；沙盒可能没有真的占住设备",
            pytrace=False,
        )

    def device_nodes(self) -> dict[int, str]:
        """受管设备节点：minor → /dev 路径。

        用 Worker 自己的 ``NEU_BOX_DEVICE_FILTER`` 扫 /dev，与 Worker 的
        ``devices.scan_nodes`` 同一口径 —— 不靠猜名字。
        """
        try:
            pattern = re.compile(self.device_filter)
        except re.error as exc:
            pytest.fail(
                f"{self.config_path} 里的 NEU_BOX_DEVICE_FILTER 不是合法正则: {exc}",
                pytrace=False,
            )
        nodes: dict[int, str] = {}
        try:
            entries = os.listdir("/dev")
        except OSError as exc:
            pytest.fail(f"无法列出 /dev: {exc}", pytrace=False)
        for entry in sorted(entries):
            path = os.path.join("/dev", entry)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISCHR(info.st_mode) or not pattern.fullmatch(entry):
                continue
            nodes[os.minor(info.st_rdev)] = path
        return nodes

    def device_node(self, minor: int) -> str:
        """按 minor 找到设备节点；找不到就失败并说明用了什么过滤器。"""
        nodes = self.device_nodes()
        if minor not in nodes:
            pytest.fail(
                f"/dev 下没有 minor={minor} 且匹配 NEU_BOX_DEVICE_FILTER="
                f"{self.device_filter!r} 的字符设备；已发现 {sorted(nodes)}",
                pytrace=False,
            )
        return nodes[minor]

    def device_number(self, minor: int) -> str:
        """minor → 本机实际 major:minor（不假设 major 是常数）。"""
        info = os.stat(self.device_node(minor))
        return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"

    @staticmethod
    def probe_device_open(path: str, timeout: int = 10) -> int:
        """沙盒外探测设备节点能否打开。

        用 fork 出来的子进程 + SIGALRM 限时，不依赖 coreutils 的 timeout，
        也不会让驱动 open 卡住整个验收。
        """
        if not hasattr(os, "fork"):  # pragma: no cover - Linux 上恒有
            pytest.fail("os.fork 不可用，无法做设备 open 探测", pytrace=False)
        pid = os.fork()
        if pid == 0:  # 子进程
            try:
                signal.alarm(timeout)
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                os.close(descriptor)
                os._exit(OPEN_OK)
            except PermissionError:
                os._exit(OPEN_DENIED)
            except OSError:
                os._exit(OPEN_INCONCLUSIVE)
            except BaseException:
                os._exit(OPEN_INCONCLUSIVE)
        _, status = os.waitpid(pid, 0)
        if os.WIFSIGNALED(status):
            return OPEN_TIMEOUT
        return os.WEXITSTATUS(status)

    # ── 沙盒 ────────────────────────────────────────────────────

    def acquire_payload(
        self,
        pid: int,
        *,
        device_num: int = 1,
        device_ids: list | None = None,
        cpu: int = 0,
        memory: int = 0,
        mem_unit: str = "GB",
        user: str | None = None,
    ) -> dict:
        payload = {
            "username": user or self.user,
            "pid": pid,
            "device_num": 0 if device_ids else device_num,
            "cpu": cpu,
            "memory": memory,
            "mem_unit": mem_unit,
        }
        if device_ids:
            payload["device_ids"] = [str(value) for value in device_ids]
        return payload

    def acquire_sandbox(self, payload: dict, *, timeout: float | None = None) -> dict:
        """申请沙盒并等它落地（资源不足时排队，轮询 acquire_id）。"""
        limit = timeout or self.task_timeout
        result = self.client.acquire(payload)
        if result.status == 201:
            body = result.json()
            self.created_sandboxes.append(body.get("sandbox_name", ""))
            return body
        if result.status != 202:
            pytest.fail(
                f"申请沙盒被拒绝（HTTP {result.status}）: {result.text[:1000]}\n"
                f"请求体: {json.dumps(payload, ensure_ascii=False)}",
                pytrace=False,
            )
        request_id = result.value("acquire_id")
        deadline = time.time() + limit
        state = "queued"
        while time.time() < deadline:
            polled = self.client.acquire_status(request_id)
            if polled.status == 201:
                body = polled.json()
                self.created_sandboxes.append(body.get("sandbox_name", ""))
                return body
            if polled.status == 202:
                state = "queued"
            elif polled.status == 404:
                pytest.fail(
                    f"acquire 请求 {request_id} 的结果已被消费或不存在"
                    f"（HTTP 404）: {polled.text[:1000]}",
                    pytrace=False,
                )
            else:
                pytest.fail(
                    f"acquire 请求 {request_id} 失败（HTTP {polled.status}）:"
                    f"{polled.text[:1000]}",
                    pytrace=False,
                )
            time.sleep(self.poll)
        pytest.fail(
            f"acquire 请求 {request_id} 在 {limit:.0f}s 内没有拿到资源"
            f"（状态 {state}）",
            pytrace=False,
        )

    def release_sandbox(self, sandbox_name: str) -> HttpResult:
        result = self.client.release(sandbox_name)
        if result.status == 200 and sandbox_name in self.created_sandboxes:
            self.created_sandboxes.remove(sandbox_name)
        return result

    def sandboxes(self, username: str | None = None) -> list[dict]:
        result = self.client.dev_list(username)
        if result.status != 200:
            pytest.fail(
                f"GET /sandbox/list 失败（HTTP {result.status}）: {result.text[:1000]}",
                pytrace=False,
            )
        return result.json().get("sandboxes") or []

    def sandbox_of_pid(self, pid: int) -> dict:
        """``GET /sandbox/status?pid=`` 的原始响应。"""
        result = self.client.sandbox_status(pid=pid)
        if result.status != 200:
            pytest.fail(
                f"GET /sandbox/status?pid={pid} 失败（HTTP {result.status}）: "
                f"{result.text[:1000]}",
                pytrace=False,
            )
        return result.json()

    def sandbox_of_container(self, container_ref: str) -> HttpResult:
        """``GET /sandbox/status?container=``：容器当前登记在哪个沙盒。

        这是从外部观察"容器登记有没有落到库里"的唯一接口，返回值里
        ``sandbox_name`` 为 null 就表示没有登记。
        """
        return self.client.sandbox_status(container=container_ref)

    def wait_container_registered(self, container_ref: str,
                                  timeout: float = 60.0) -> str:
        deadline = time.time() + timeout
        payload = {}
        while time.time() < deadline:
            result = self.sandbox_of_container(container_ref)
            if result.status != 200:
                pytest.fail(
                    f"GET /sandbox/status?container={container_ref} 失败"
                    f"（HTTP {result.status}）: {result.text[:1000]}",
                    pytrace=False,
                )
            payload = result.json()
            name = payload.get("sandbox_name")
            if name:
                return name
            time.sleep(self.poll)
        pytest.fail(
            f"容器 {container_ref} 在 {timeout:.0f}s 内没有登记到任何沙盒"
            f"（hook 没跑，或登记被拒）",
            pytrace=False,
        )

    def wait_container_unregistered(self, container_ref: str,
                                    timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.sandbox_of_container(container_ref)
            if result.status == 200 and not result.json().get("sandbox_name"):
                return
            time.sleep(self.poll)
        pytest.fail(
            f"容器 {container_ref} 的登记在 {timeout:.0f}s 内没有被注销",
            pytrace=False,
        )

    def find_sandbox(self, sandbox_name: str) -> dict | None:
        for record in self.sandboxes():
            if record.get("name") == sandbox_name:
                return record
        return None

    def wait_sandbox_gone(self, sandbox_name: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.find_sandbox(sandbox_name) is None:
                return
            time.sleep(self.poll)
        pytest.fail(
            f"沙盒 {sandbox_name} 在 {timeout:.0f}s 内仍存在于 /sandbox/list",
            pytrace=False,
        )

    # ── 本机进程 ────────────────────────────────────────────────

    def spawn(self, argv: list[str], *, user: str | None = None,
              env: dict | None = None, cwd: str | None = None) -> subprocess.Popen:
        """起一个本机进程（默认就是测试用户身份）。"""
        options: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": cwd or self.tempdir,
        }
        if env is not None:
            options["env"] = {**os.environ, **env}
        if user:
            options["user"] = user
        try:
            process = subprocess.Popen(argv, **options)
        except (OSError, ValueError) as exc:
            pytest.fail(
                f"无法以用户 {user or current_user()} 启动 {argv}: {exc}",
                pytrace=False,
            )
        self._children.append(process)
        return process

    def spawn_sleeper(self, seconds: int = 600, *, user: str | None = None):
        """起一个长期存活的进程，用于 acquire / join 类用例。"""
        return self.spawn(["sleep", str(seconds)], user=user)

    def spawn_terminal(self, seconds: int = 600) -> subprocess.Popen:
        """起一个"借出去当终端"的进程：归属必须是验收用户，且当前不在沙盒里。

        ``setsid`` 让它脱离本进程的会话 —— 否则沙盒销毁时的 cgroup.kill 会
        把测试进程自己一起带走。
        """
        return self.spawn(
            ["setsid", "sleep", str(seconds)], user=self.user,
        )

    def fork_child_in_place(self, seconds: int = 600,
                            tag: str = "child") -> tuple[subprocess.Popen, int]:
        """起一个"接到放行信号就 fork 一个子进程、然后等着"的进程。

        返回 (父进程, 子进程 PID)。父进程被 kill 之后子进程留在同一个 cgroup
        里 —— Reaper 用例要的正是"父进程死了、子进程还活着"这个状态；fork 必
        须发生在 acquire 之后，所以要用 :meth:`fork_now` 分两步走。
        """
        gate = os.path.join(self.tempdir, f"{tag}.go")
        pid_file = os.path.join(self.tempdir, f"{tag}.pid")
        for path in (gate, pid_file):
            try:
                os.remove(path)
            except OSError:
                pass
        process = self.spawn(
            ["bash", "-c",
             f"while [ ! -e {shlex.quote(gate)} ]; do sleep 0.2; done; "
             f"setsid sleep {int(seconds)} & echo $! > {shlex.quote(pid_file)}; "
             f"wait"],
            user=self.user,
        )
        return process, 0

    def fork_now(self, tag: str = "child", timeout: float = 10.0) -> int:
        """放行上一步的父进程，返回它 fork 出来的子进程 PID。"""
        gate = os.path.join(self.tempdir, f"{tag}.go")
        pid_file = os.path.join(self.tempdir, f"{tag}.pid")
        with open(gate, "w", encoding="utf-8"):
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(pid_file, encoding="utf-8") as stream:
                    child = int(stream.read().strip() or 0)
            except (OSError, ValueError):
                child = 0
            if child > 0:
                return child
            time.sleep(0.1)
        pytest.fail(
            f"子进程没有在 {timeout:.0f}s 内写出 PID 文件 {pid_file}",
            pytrace=False,
        )

    def kill(self, pid: int) -> None:
        """杀掉本用例起的进程；已经没了就当成功。"""
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            pytest.fail(f"无法结束进程 {pid}: {exc}", pytrace=False)

    @staticmethod
    def inside_sandbox() -> bool:
        """当前进程是不是已经在某个 Neu Box 沙盒里。"""
        try:
            with open("/proc/self/cgroup", encoding="utf-8") as stream:
                return any("sandbox_" in line for line in stream)
        except OSError:
            return False

    def wait_process_gone(self, pid: int, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not process_alive(pid):
                return
            time.sleep(self.poll)
        pytest.fail(f"进程 {pid} 在 {timeout:.0f}s 内没有退出", pytrace=False)

    def wait_process_alive(self, pid: int, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if process_alive(pid):
                return
            time.sleep(0.1)
        pytest.fail(f"进程 {pid} 没有活到预期的时间点", pytrace=False)

    @staticmethod
    def process_cgroup(pid: int) -> str:
        """读 /proc/<pid>/cgroup 的 cgroup v2 路径（"0::<path>" 的第三段）。"""
        try:
            with open(f"/proc/{pid}/cgroup", encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("0::"):
                        return line.strip().split("::", 1)[1]
        except OSError:
            pass
        return ""

    # ── 服务控制（停服/起服类用例） ─────────────────────────────
    #
    # 单元里有 ``RefuseManualStop=yes``：``systemctl stop`` 和 ``systemctl
    # restart`` 会被 systemd 拒绝（这是刻意的 —— 见
    # ``src/neu_box/maintenance/pause.py`` 的 ``stop_worker_after_cleanup``），
    # 所以这一层不能再用 systemctl 停服务。两种动作各有对应的做法：
    #
    #   * 停：取 MainPID 直接发 SIGTERM —— pause 在排空、备份、cleanup 之后
    #     做的也是这一件事。**不能改用 ``neuboxctl pause``**：它先等
    #     ``quiet=true``（运行任务结束、沙盒全部回收），而下面对应的用例恰恰
    #     要在"有任务在跑 / 有沙盒和容器登记"的时候把 Worker 停掉，走 pause
    #     只会一直等到超时。
    #   * 起：走被认可的启动路径 ``neuboxctl setup``（迁移 + 检查数据库、
    #     以暂停状态拉起、等 /healthz 通过、恢复调度并清掉 ``.paused`` 标记）。
    #     直接 ``systemctl start`` 虽然不被 RefuseManualStop 拦，但它绕过了
    #     迁移和健康检查，而且遇到遗留的 ``.paused`` 标记会让 Worker 以暂停态
    #     起来 —— 验收要验的是产品文档里那条启动路径。

    def require_service_control(self) -> None:
        """停/起类用例的前置：root + systemd + 管理 CLI。"""
        if os.geteuid() != 0:
            pytest.fail(
                "停/起 Worker 需要 root：请用 sudo 运行验收"
                "（给 MainPID 发 SIGTERM 和 neuboxctl setup 都按 root 设计）",
                pytrace=False,
            )
        if shutil.which("systemctl") is None:
            pytest.fail(
                "找不到 systemctl，无法停/起 Worker；本层验收必须在 systemd 部署机上跑",
                pytrace=False,
            )
        if not os.access(self.ctl_binary, os.X_OK):
            pytest.fail(
                f"前置缺失：找不到可执行的 neuboxctl {self.ctl_binary!r}；"
                f"起服走 `neuboxctl setup`，该文件由 neuboxd RPM 装在 "
                f"/usr/sbin/neuboxctl。路径不同时用 --deployment-ctl 指定",
                pytrace=False,
            )

    def service_active(self) -> bool:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", self.service],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def worker_main_pid(self) -> int:
        """systemd 认的 Worker 主进程；pause 停服时取的就是这个值。"""
        result = subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", self.service],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"systemctl show --property=MainPID --value {self.service} 失败"
                f"（退出码 {result.returncode}）: {(result.stdout or '').strip()[:1000]}",
                pytrace=False,
            )
        raw = (result.stdout or "").strip()
        if not raw.isdigit():
            pytest.fail(
                f"{self.service} 的 MainPID 不是数字: {raw!r}", pytrace=False,
            )
        pid = int(raw)
        if pid <= 1:
            pytest.fail(
                f"{self.service} 的 MainPID={pid}：服务没在跑，无法停服",
                pytrace=False,
            )
        return pid

    def stop_worker(self, timeout: float = 60.0) -> None:
        """停 Worker：给 MainPID 发 SIGTERM（和 pause 停服做的事一样）。

        ``systemctl stop`` 会被单元的 ``RefuseManualStop=yes`` 拒绝，这里复现
        ``pause.py`` 的 ``stop_worker_after_cleanup()``：取 MainPID、发 SIGTERM。
        SIGTERM 属于 systemd 视为正常退出的信号，不会触发 ``Restart=on-failure``，
        所以 Worker 会保持停止，直到显式 ``setup`` 把它拉起来。
        """
        self.require_service_control()
        pid = self.worker_main_pid()
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            pytest.fail(f"无法给 Worker MainPID {pid} 发 SIGTERM: {exc}", pytrace=False)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.service_active():
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"{self.service} 在 {timeout:.0f}s 内没有停下来", pytrace=False)
        # systemctl is-active 在 "deactivating" 阶段就已经返回非 0，而 setup
        # 会拒绝在服务仍在运行时启动，所以还要确认进程真的退出了。
        self.wait_process_gone(pid, timeout=timeout)
        self.wait_worker_down(timeout=20.0)

    def run_ctl(self, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
        """调用公开管理 CLI；失败时直接 pytest.fail，成功后返回结果。"""
        self.require_service_control()
        argv = [self.ctl_binary, "--config", self.config_path, *args]
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            pytest.fail(
                f"{' '.join(argv)} 在 {timeout:.0f}s 内没有返回:\n"
                f"{output.strip()[:2000]}",
                pytrace=False,
            )
        if result.returncode != 0:
            pytest.fail(
                f"管理命令失败：{' '.join(argv)} 退出码 {result.returncode}:\n"
                f"{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        return result

    def start_worker(self, timeout: float = 90.0) -> None:
        """起 Worker：调用公开的 ``neuboxctl setup``。

        ``neuboxctl`` 允许用 ``--config`` 覆盖配置文件，所以验收仍然可以遵守
        ``--deployment-config``，而不需要绕过管理 CLI 去调 daemon。
        """
        self.require_service_control()
        argv = [
            self.ctl_binary, "--config", self.config_path, "setup",
            "--timeout", str(int(timeout)),
        ]
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                # setup 自己会等 --timeout 秒的 /healthz；再给迁移、备份和
                # systemctl 操作留余量，超时说明它卡住了。
                timeout=timeout + 120.0,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            pytest.fail(
                f"{' '.join(argv)} 在 {timeout + 120.0:.0f}s 内没有返回:\n"
                f"{output.strip()[:2000]}",
                pytrace=False,
            )
        if result.returncode != 0:
            pytest.fail(
                f"启动 Worker 失败：{' '.join(argv)} 退出码 {result.returncode}:\n"
                f"{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        self.wait_worker_up(timeout=timeout)

    def restart_worker(self, timeout: float = 90.0) -> None:
        """停 + 起（``systemctl restart`` 被拒，这里拆成两个被认可的动作）。"""
        self.stop_worker()
        self.start_worker(timeout=timeout)

    def wait_worker_up(self, timeout: float = 90.0) -> None:
        """等 /healthz 回来；超时失败并说明服务是否 active。"""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                result = self.client.healthz()
            except WorkerUnreachable as exc:
                last = str(exc)
            else:
                if result.status == 200:
                    return
                last = f"HTTP {result.status}: {result.text[:400]}"
            time.sleep(self.poll)
        pytest.fail(
            f"Worker 在 {timeout:.0f}s 内没有恢复健康"
            f"（service active={self.service_active()}）；最后一次探测: {last}",
            pytrace=False,
        )

    def wait_worker_down(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.client.healthz()
            except WorkerUnreachable:
                return
            time.sleep(self.poll)
        pytest.fail(f"Worker 在 {timeout:.0f}s 内仍然接受 HTTP 请求", pytrace=False)

    # ── Docker（容器组用例） ────────────────────────────────────

    def docker(self, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        """跑一条 docker 命令；不检查退出码，调用方自己判定。"""
        if shutil.which("docker") is None:
            pytest.fail(
                "找不到 docker 命令：容器组验收需要在部署机上装 Docker",
                pytrace=False,
            )
        try:
            return subprocess.run(
                ["docker", *args],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"docker {' '.join(args)} 在 {timeout:.0f}s 内没有返回",
                pytrace=False,
            )

    def docker_info(self) -> str:
        result = self.docker("info", timeout=60)
        if result.returncode != 0:
            pytest.fail(
                "docker info 失败，Docker 守护进程不可用:\n"
                f"{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        if "Server Version" not in result.stdout:
            pytest.fail(
                "docker info 没有 Server Version，docker 客户端没能连上 daemon:\n"
                f"{result.stdout[:2000]}",
                pytrace=False,
            )
        return result.stdout

    def default_runtime(self) -> str:
        """dockerd 的 default-runtime；空串表示没配（退回 runc）。

        没有注解注入就没有登记，所以这一项是容器组的第一道前置。
        """
        result = self.docker("info", "--format", "{{.DefaultRuntime}}", timeout=60)
        if result.returncode != 0:
            pytest.fail(
                "docker info --format '{{.DefaultRuntime}}' 失败，"
                "Docker 守护进程不可用:\n"
                f"{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        return (result.stdout or "").strip()

    def container_removed(self, reference: str) -> bool:
        """容器是否已不存在（docker inspect 失败即认为已回收）。"""
        result = self.docker("inspect", "--format", "{{.Id}}", reference)
        return result.returncode != 0

    def wait_container_removed(self, reference: str, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.container_removed(reference):
                return
            time.sleep(self.poll)
        pytest.fail(
            f"容器 {reference} 在 {timeout:.0f}s 内仍然存在（docker inspect 仍然成功）",
            pytrace=False,
        )

    def local_images(self) -> list[str]:
        """本机已有的镜像（``仓库:标签``）。"""
        result = self.docker(
            "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", timeout=60,
        )
        if result.returncode != 0:
            pytest.fail(
                f"docker image ls 失败:\n{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        return [
            line.strip() for line in (result.stdout or "").splitlines()
            if line.strip() and "<none>" not in line
        ]

    def resolve_image(self) -> str:
        """容器用例要用的镜像。

        优先 ``NEU_BOX_CONTAINER_IMAGE``；否则取本机第一个可用镜像 —— 部署机
        通常不联网，绝不在这里 docker pull。
        """
        requested = (os.environ.get("NEU_BOX_CONTAINER_IMAGE") or "").strip()
        available = self.local_images()
        if requested:
            if requested not in available:
                pytest.fail(
                    f"前置缺失：NEU_BOX_CONTAINER_IMAGE={requested!r} 不在本机"
                    f"已有镜像里；已有镜像: {available or '(无)'}",
                    pytrace=False,
                )
            return requested
        if not available:
            pytest.fail(
                "前置缺失：本机没有任何 docker 镜像，容器组用例需要一个带 "
                "shell 的镜像；先 docker load / docker pull 一个，或用 "
                "NEU_BOX_CONTAINER_IMAGE 指定（本层不会自己联网拉镜像）",
                pytrace=False,
            )
        return available[0]

    def container_devices(self) -> list[str]:
        """容器要挂进去的设备节点：与 Worker 给容器挂的一致（全部受管节点）。"""
        nodes = self.device_nodes()
        return [nodes[minor] for minor in sorted(nodes)]

    def docker_run(self, *args: str, timeout: float = 180.0,
                   detach: bool = False) -> subprocess.CompletedProcess:
        """起一个容器；`--rm` 一律不加，收尾由调用方或 Worker 负责。"""
        argv = ["run"]
        if detach:
            argv.append("-d")
        argv.extend(args)
        return self.docker(*argv, timeout=timeout)

    def container_id_of(self, reference: str) -> str:
        """容器引用 → 完整 ID（登记接口认的是完整 ID）。"""
        result = self.docker("inspect", "--format", "{{.Id}}", reference)
        if result.returncode != 0:
            pytest.fail(
                f"docker inspect {reference} 失败:\n"
                f"{(result.stdout or '').strip()[:2000]}",
                pytrace=False,
            )
        return (result.stdout or "").strip()

    def remove_container(self, reference: str) -> None:
        """尽力删掉容器；失败只记不抛（收尾路径）。"""
        try:
            self.docker("rm", "-f", "-v", reference, timeout=60)
        except BaseException:  # pytest.fail 抛的是 BaseException 子类
            pass


# ── 容器组共用的动作（容器组与慢组都要用） ──────────────────────

# 容器内探测设备节点：在子 shell 里 open，父 shell 拿退出码和错误消息。
# 只认退出码不认消息文本 —— 嵌入式镜像的 libc 消息目录不一定完整。
# 探测完再挂住，方便外面观察登记状态。
CONTAINER_PROBE_MARKER = "CONTAINER_PROBE_RC="
CONTAINER_OPEN_OK = "CONTAINER_PROBE_RC=0"

_CONTAINER_PROBE_TEMPLATE = (
    "out=$( ( exec 3<{node} ) 2>&1 ); rc=$?; "
    "printf 'CONTAINER_PROBE_RC=%s\\n' \"$rc\"; "
    "printf 'CONTAINER_PROBE_MSG=%s\\n' \"$out\"; "
    "sleep {hold}"
)


def container_probe_command(node: str, hold: int = 60) -> str:
    """容器里跑的探测脚本：打开设备节点，打印退出码，再挂住 hold 秒。"""
    return _CONTAINER_PROBE_TEMPLATE.format(node=node, hold=hold)


def container_device_args(d: "Deployment") -> list[str]:
    """容器要挂的设备节点参数：与 Worker 自己挂的一致（全部受管节点）。"""
    argv: list[str] = []
    for node in d.container_devices():
        argv.extend(["--device", node])
    return argv


def run_container(
    d: "Deployment",
    image: str,
    *,
    annotation: str | None = None,
    command: str,
    detach: bool = True,
    timeout: float = 180.0,
) -> tuple[str, subprocess.CompletedProcess]:
    """起一个容器；返回 ``(容器名, docker 结果)``。

    一律用 ``--entrypoint sh``：镜像自己的 ENTRYPOINT 会把命令前缀掉，探测
    脚本就静默地不执行了。容器名自己取（并登记进会话收尾），失败路径上也
    能精确地找回来。
    """
    name = f"neu-box-acc-{secrets.token_hex(6)}"
    argv = ["--name", name]
    if annotation:
        argv += ["--annotation", f"sandbox_cgroup={annotation}"]
    argv += container_device_args(d)
    argv += ["--entrypoint", "sh", image, "-c", command]
    d.created_containers.append(name)
    return name, d.docker_run(*argv, detach=detach, timeout=timeout)


def container_log(d: "Deployment", reference: str) -> str:
    result = d.docker("logs", reference, timeout=60)
    return (result.stdout or "") + (result.stderr or "")


def wait_container_log_count(d: "Deployment", reference: str, marker: str,
                             count: int, timeout: float = 60.0) -> str:
    """等日志里出现第 count 个 marker —— 容器重启会往同一个日志流里追加。"""
    deadline = time.time() + timeout
    text = ""
    seen = 0
    while time.time() < deadline:
        text = container_log(d, reference)
        seen = text.count(marker)
        if seen >= count:
            return text
        time.sleep(d.poll)
    pytest.fail(
        f"容器 {reference} 的日志里 {marker!r} 只出现了 {seen} 次，期望至少 "
        f"{count} 次；日志:\n{text[:2000]}",
        pytrace=False,
    )


def require_container_not_running(d: "Deployment", reference: str, *,
                                  context: str) -> None:
    """容器没跑起来：记录不存在，或存在但 ``State.Running`` 为 false。"""
    result = d.docker("inspect", "--format", "{{.State.Running}}", reference)
    if result.returncode != 0:
        return  # 容器记录已被回收，干净
    running = (result.stdout or "").strip()
    if running != "false":
        pytest.fail(
            f"{context}，但容器 {reference} 仍在运行"
            f"（docker inspect 的 State.Running={running!r}）",
            pytrace=False,
        )


def current_user() -> str:
    """当前进程的 Linux 用户名。"""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - 匿名 uid
        return str(os.getuid())


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def process_alive(pid: int) -> bool:
    """进程是否还活着（僵尸算不活）。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            fields = stream.read().rsplit(")", 1)[1].split()
    except OSError:
        return False
    return bool(fields) and fields[0] != "Z"


def shell_command(script: str, *args: str) -> str:
    """把一段 shell 脚本 + 位置参数拼成可提交的 command。"""
    quoted = " ".join(shlex.quote(str(value)) for value in args)
    return f"bash -c {shlex.quote(script)} _ {quoted}".strip()


def frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
