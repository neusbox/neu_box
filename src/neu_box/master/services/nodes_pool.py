import logging
import threading
import time
import uuid

import requests

from neu_box.master.paths import nodes_config_path

logger = logging.getLogger(__name__)


class Nodes:
    """单个节点的完整信息"""

    def __init__(self, node_id: str, name: str, ip: str, port: int):
        self.node_id = node_id
        self.name = name
        self.ip = ip
        self.port = port

        # ── 动态状态（由心跳或主动查询更新） ──
        self.status = 'offline'          # online | offline
        self.total_cpu = 0
        self.idle_cpu = 0
        self.total_mem = 0               # bytes
        self.idle_mem = 0                # bytes
        self.total_devices = 0
        self.idle_devices = 0
        self.dev_status = {}     # {minor: 1/0}, 1=忙碌 0=空闲
        self.active_sandboxes = 0
        self.last_heartbeat = 0.0        # timestamp

    def apply_status(self, data: dict):
        """用 worker /status 返回的数据更新本节点状态"""
        self.status = data.get('status', self.status)
        self.total_cpu = data.get('total_cpu', self.total_cpu)
        self.idle_cpu = data.get('idle_cpu', self.idle_cpu)
        self.total_mem = data.get('total_mem', self.total_mem)
        self.idle_mem = data.get('idle_mem', self.idle_mem)
        self.total_devices = data.get('total_devices', self.total_devices)
        self.idle_devices = data.get('idle_devices', self.idle_devices)
        self.dev_status = data.get('dev_status', self.dev_status)
        self.active_sandboxes = data.get('active_sandboxes', self.active_sandboxes)
        self.last_heartbeat = time.time()


class Nodes_Pool:
    """节点池（单例）—— 管理所有 worker 节点"""

    _instance = None

    def __init__(self):
        self.nodes: dict[str, Nodes] = {}    # node_id → Nodes
        self._config_path = str(nodes_config_path())

    @classmethod
    def get_nodes_pool(cls) -> 'Nodes_Pool':
        if cls._instance is None:
            obj = object.__new__(cls)
            obj.nodes = {}
            obj._config_path = str(nodes_config_path())
            obj._init_from_config()
            cls._instance = obj
        return cls._instance

    # ── 初始化 & 同步 ──────────────────────────────────────────

    def _init_from_config(self):
        """从 config.json 首次加载节点。"""
        self._sync_nodes_from_config()

    def _read_config_file(self) -> list[dict]:
        """读取 config.json 中 nodes_pool 数组，解析失败返回 []。"""
        import json
        try:
            with open(self._config_path) as f:
                cfg = json.load(f)
            return cfg.get('nodes_pool', [])
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def sync_from_config(self):
        """公开方法：主动从 config.json 同步节点（供 API 调用）。"""
        self._sync_nodes_from_config()

    def _sync_nodes_from_config(self):
        """从 config.json 同步节点：按 name 去重，已存在的保留 node_id，不存在的删除。"""
        nodes_cfg = self._read_config_file()

        # 按 name 建索引，方便查找
        old_by_name: dict[str, Nodes] = {}
        for node in self.nodes.values():
            if node.name:
                old_by_name[node.name] = node

        new_nodes: dict[str, Nodes] = {}
        for entry in nodes_cfg:
            host = entry.get('host', '').strip()
            port = entry.get('port', 5000)
            name = entry.get('name', '').strip()
            if not host or not name:
                continue

            existing = old_by_name.get(name)
            if existing:
                existing.ip = host
                existing.port = port
                new_nodes[existing.node_id] = existing
            else:
                node_id = str(uuid.uuid4())
                node = Nodes(node_id, name, host, port)
                new_nodes[node_id] = node
                logger.info('新增节点 %s (%s @ %s:%s)', node_id, name, host, port)

        # 移除不再存在于 config 中的节点
        for node_id in list(self.nodes.keys()):
            if node_id not in new_nodes:
                logger.info('移除节点 %s', node_id)
        self.nodes = new_nodes

    # ── CRUD ───────────────────────────────────────────────────

    def add_node(self, node: Nodes):
        self.nodes[node.node_id] = node

    def remove_node(self, node_id: str):
        self.nodes.pop(node_id, None)

    def get_node_by_id(self, node_id: str) -> Nodes | None:
        return self.nodes.get(node_id)

    def get_node_by_host_port(self, host: str, port: int) -> Nodes | None:
        """按 host + 端口查找节点，用于去重。"""
        for node in self.nodes.values():
            if node.ip == host and node.port == port:
                return node
        return None

    # ── 供前端/API 调用的列表 ─────────────────────────────────

    def get_all_nodes(self) -> list[dict]:
        """返回所有节点摘要，供前端选择器使用"""
        result = []
        for node in self.nodes.values():
            result.append({
                'node_id': node.node_id,
                'name': node.name,
                'ip': node.ip,
                'port': node.port,
                'status': node.status,
                'total_cpu': node.total_cpu,
                'idle_cpu': node.idle_cpu,
                'total_mem': node.total_mem,
                'idle_mem': node.idle_mem,
                'total_devices': node.total_devices,
                'idle_devices': node.idle_devices,
                'dev_status': node.dev_status,
                'active_sandboxes': node.active_sandboxes,
            })
        return result

    # ── 状态采集 ───────────────────────────────────────────────

    def query_node_status(self, node_id: str) -> dict:
        """主动向 worker 查询实时状态并更新本地记录"""
        node = self.get_node_by_id(node_id)
        if not node:
            raise ValueError(f'节点 {node_id} 不存在')

        try:
            resp = requests.get(
                f'http://{node.ip}:{node.port}/status',
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            node.apply_status(data)
            node.status = 'online'
            return data
        except requests.RequestException as e:
            node.status = 'offline'
            return {'error': '无法连接到节点', 'details': str(e)}

    def query_all_nodes_status(self) -> dict[str, dict]:
        """批量查询所有节点状态"""
        results = {}
        for node_id in self.nodes:
            results[node_id] = self.query_node_status(node_id)
        return results

    # ── 定期轮询 ───────────────────────────────────────────────

    _polling: bool = False
    _poll_thread: threading.Thread | None = None
    _poll_interval: int = 15         # 默认每 15 秒轮询一次

    def start_polling(self, interval: int = 15):
        """启动后台轮询线程，定期查询所有 worker 的 /status。"""
        if Nodes_Pool._polling:
            return
        Nodes_Pool._poll_interval = interval
        Nodes_Pool._polling = True
        Nodes_Pool._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name='node-status-poller',
        )
        Nodes_Pool._poll_thread.start()
        logger.info('后台轮询已启动，每 %ss 查询所有节点状态', interval)

    def stop_polling(self):
        """停止后台轮询。"""
        Nodes_Pool._polling = False
        if Nodes_Pool._poll_thread:
            Nodes_Pool._poll_thread.join(timeout=2)

    def _poll_loop(self):
        while Nodes_Pool._polling:
            t0 = time.monotonic()
            self._poll_all_nodes()
            # 用实际耗时修正 sleep，保证轮询间隔稳定
            elapsed = time.monotonic() - t0
            sleep_time = max(0, Nodes_Pool._poll_interval - elapsed)
            time.sleep(sleep_time)

    def _poll_all_nodes(self):
        """向所有节点请求 /status 并更新本地记录。每次先同步 config.json 中的节点列表。"""
        self._sync_nodes_from_config()
        for node_id, node in list(self.nodes.items()):
            try:
                resp = requests.get(
                    f'http://{node.ip}:{node.port}/status',
                    timeout=5,
                )
                resp.raise_for_status()
                data = resp.json()
                node.apply_status(data)
                node.status = 'online'
            except requests.RequestException:
                node.status = 'offline'

    # ── 通用转发 ────────────────────────────────────────────────

    def forward_to_node(self, node_id: str, endpoint: str,
                        req: dict, timeout: int = 30) -> requests.Response:
        """向指定 worker 节点转发请求。

        Args:
            node_id:  目标节点 UUID
            endpoint: Worker 上的路径，如 '/command/run'
            req:      JSON 请求体
            timeout:  HTTP 超时秒数

        Returns:
            requests.Response 对象
        """
        node = self.get_node_by_id(node_id)
        if not node:
            raise ValueError(f'节点 {node_id} 不存在')
        try:
            resp = requests.post(
                f'http://{node.ip}:{node.port}{endpoint}',
                json=req,
                timeout=timeout,
            )
            logger.debug('转发 → %s %s 状态 %s', node_id, endpoint, resp.status_code)
            if not resp.ok and resp.text and resp.text.strip().startswith('<!'):
                raise ValueError(f'Worker 返回错误: {resp.text[:200]}')
            return resp
        except requests.RequestException as e:
            raise ValueError(f'无法连接节点 {node_id}: {e}')

    def forward_get_to_node(self, node_id: str, endpoint: str,
                            params: dict = None, timeout: int = 30) -> requests.Response:
        """向指定 worker 节点转发 GET 请求。

        Args:
            node_id:  目标节点 UUID
            endpoint: Worker 上的路径，如 '/command/queue'
            params:   URL 查询参数
            timeout:  HTTP 超时秒数

        Returns:
            requests.Response 对象
        """
        node = self.get_node_by_id(node_id)
        if not node:
            raise ValueError(f'节点 {node_id} 不存在')
        try:
            resp = requests.get(
                f'http://{node.ip}:{node.port}{endpoint}',
                params=params,
                timeout=timeout,
            )
            logger.debug('GET → %s %s 状态 %s', node_id, endpoint, resp.status_code)
            if not resp.ok and resp.text and resp.text.strip().startswith('<!'):
                raise ValueError(f'Worker 返回错误: {resp.text[:200]}')
            return resp
        except requests.RequestException as e:
            raise ValueError(f'无法连接节点 {node_id}: {e}')
