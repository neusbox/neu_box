"""沙盒收尸与启动恢复。

两条路径 + 一个循环：

  - ``recover_on_startup``：进程启动时把 DB 和 cgroup 的实际状态对齐
    （上次崩溃留下的 running 任务沙盒、半成品 CREATING、孤儿记录）。
  - ``run_once``：周期性扫描 —— 空 cgroup、DESTROYING、文件系统上的孤儿，
    该销毁的销毁。
  - ``start``：后台线程跑 ``run_once``，**等待用 epoll 而不是 sleep** ——
    容器退出的事件会把它立刻叫醒（见 SbxManager 的容器退出监听）。

这里只负责"发现"，销毁动作全部交给 ``SbxManager.destroy_sandbox``。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from neu_box.config import env_int
from neu_box.runtime import cgroup

logger = logging.getLogger(__name__)


class Reaper:
    """沙盒收尸器；状态都在 SbxManager 上，这里只拿它当协作方。"""

    def __init__(self, manager):
        self.mgr = manager
        # Startup recovery can be transiently blocked by an unavailable
        # Docker daemon. Keep the durable task/sandbox pair for the periodic
        # loop instead of requiring another Worker restart to retry it.
        self._pending_task_recovery = {}

    # ── 启动恢复 ─────────────────────────────────────────────

    def recover_on_startup(self):
        """重启后核对 DB 与 cgroup 实际状态，清理已不存在的沙盒记录。"""
        # A command task that was ``running`` when the previous Worker died
        # has no executor left to reap its cgroup.  Match the deterministic
        # task sandbox name before the queue marks that task failed, and let
        # native destroy kill any surviving child processes.  Acquire
        # sandboxes are intentionally not inferred from their PID-shaped
        # names: they belong to the terminal and may outlive a service
        # restart.
        running_task_names = {}
        get_queue_tasks = getattr(self.mgr.db, 'get_queue_tasks', None)
        if get_queue_tasks is not None:
            try:
                running_task_names = {
                    f"sbx_{task['user_id']}_{task['task_id']}.slice": task
                    for task in get_queue_tasks()
                    if task.get('status') == 'running'
                }
            except Exception:
                logger.exception('恢复时读取 running task 失败，保留 sandbox 待重试')
        # A durable CREATING row means the previous process stopped between
        # recording the intent and completing native create/join.  Running
        # command tasks take precedence: their deterministic sandbox is
        # disposable and must be failed/destroyed rather than adopted.  A
        # populated CREATING cgroup without a running task is an acquire
        # terminal and is adopted; an empty one is stale and destroyed.
        records = {
            record['name']: record for record in self.mgr.db.list_sandboxes()
        }
        for name in self.mgr.list_sandboxes():
            record = records.get(name)
            if name in running_task_names:
                task = running_task_names[name]
                logger.warning(
                    "恢复: 清理 Worker 崩溃遗留的命令 sandbox '%s'", name,
                )
                # The executor may have crashed before register_container
                # inserted a row. Remove those containers by their durable
                # Worker label before destroying the sandbox reservation.
                removed = self.mgr.remove_docker_containers_for_sandbox(name)
                if removed is None:
                    # Without a successful Docker scan/removal the label is
                    # the only durable handle for a pre-registration
                    # container. Keep the task and sandbox records intact so
                    # a later startup can retry instead of orphaning it.
                    logger.error(
                        "恢复: sandbox '%s' 的 Docker 容器未能完整回收，"
                        '保留记录等待重试', name,
                    )
                    self._pending_task_recovery[name] = task
                    continue
                # Queue recovery will not see this task after a successful
                # destroy removes the sandbox record, so persist the terminal
                # task result here as part of the same startup recovery.
                try:
                    self.mgr.db.update_task_result(
                        task['task_id'], 'failed', -1, '', '',
                        error='Worker 可能在执行过程中重启',
                    )
                except Exception:
                    logger.exception("恢复: 标记命令任务 '%s' 失败", task['task_id'])
                if not self.mgr.destroy_sandbox(name):
                    logger.error(
                        "恢复: 命令 sandbox '%s' 清理失败，保留记录等待 Reaper 重试",
                        name,
                    )
                continue
            if record and record.get('state') == 'CREATING':
                if os.path.isdir(cgroup.path(name)):
                    try:
                        snapshot = cgroup.read_snapshot(name)
                        if snapshot is None:
                            # The directory disappeared between the guard and
                            # the snapshot.  Do not promote a half-created
                            # intent to ACTIVE; route it through native
                            # destroy so a saved BPF reservation is retried.
                            if not self.mgr.destroy_sandbox(name):
                                logger.error(
                                    "恢复: CREATING sandbox '%s' 清理失败，"
                                    "保留记录等待 Reaper 重试",
                                    name,
                                )
                            continue
                        pids, populated = snapshot
                        if not populated:
                            logger.warning(
                                "恢复: 空 CREATING sandbox '%s'，清理记录",
                                name,
                            )
                            if not self.mgr.destroy_sandbox(name):
                                logger.error(
                                    "恢复: sandbox '%s' 清理失败，保留记录等待 Reaper 重试",
                                    name,
                                )
                            continue
                        if not self.mgr.db.activate_sandbox(name, pids):
                            logger.error(
                                "恢复: sandbox '%s' 激活状态写入失败，保留记录等待重试",
                                name,
                            )
                        else:
                            logger.warning(
                                "恢复: populated sandbox '%s' 从 CREATING 接管为 ACTIVE",
                                name,
                            )
                    except OSError:
                        logger.exception("恢复: 读取 sandbox '%s' 状态失败", name)
                        continue
                else:
                    logger.warning(
                        "恢复: CREATING sandbox '%s' 的 cgroup 已不存在，清理记录",
                        name,
                    )
                    if not self.mgr.destroy_sandbox(name):
                        logger.error(
                            "恢复: sandbox '%s' 清理失败，保留记录等待 Reaper 重试",
                            name,
                        )
                continue
            if not os.path.isdir(cgroup.path(name)):
                logger.warning(
                    "恢复: 沙盒 '%s' 的 cgroup 已不存在，清理设备预留",
                    name,
                )
                # 仍需经过 destroy 脚本清理 eBPF map，不能只删 DB，否则
                # reserved_devices 中的卡会一直保持预留状态。
                if not self.mgr.destroy_sandbox(name):
                    logger.error(
                        "恢复: 沙盒 '%s' 清理失败，保留记录等待 Reaper 重试",
                        name,
                    )
            else:
                logger.info("恢复: 沙盒 '%s' 仍存活", name)

        # 最后再按 label 扫一遍 Docker：崩溃窗口留下的容器没有 ``containers``
        # 行，上面这轮把沙盒记录清掉之后就再没有入口按名字找到它了。这一步
        # 反过来查，只看沙盒记录已经不在的容器；dockerd 不可用就下一轮再来，
        # 不阻塞任何已经完成的对账。
        orphans = self.mgr.reap_orphan_labelled_containers()
        if orphans:
            logger.warning("恢复: 收掉 %s 个无主容器", orphans)


    # ── 周期收尸 ─────────────────────────────────────────────

    def run_once(self) -> int:
        """清理进程已退出的沙盒，释放设备资源。

        Returns:
            清理的沙盒数量。
        """
        cleaned = 0
        interval = max(1, env_int("NEU_BOX_SANDBOX_REAPER_INTERVAL", 30))
        # 与 create/join/destroy/allocate 共用同一把锁，避免 Neu Box 在
        # “确认空”与 destroy 之间把新进程加入刚被判定为空的沙盒。
        with self.mgr.lock:
            sandbox_names = self.mgr.list_sandboxes()
            logger.debug("共 %s 个沙盒待检查", len(sandbox_names))

            # Retry startup cleanup that was blocked by a transient Docker
            # outage.  Do this before the generic empty-cgroup scan, otherwise
            # the unregistered labelled container could be mistaken for an
            # ordinary empty sandbox.
            pending = getattr(self, '_pending_task_recovery', {})
            for name, task in list(pending.items()):
                if name not in sandbox_names:
                    pending.pop(name, None)
                    continue
                removed = self.mgr.remove_docker_containers_for_sandbox(name)
                if removed is None:
                    continue
                try:
                    self.mgr.db.update_task_result(
                        task['task_id'], 'failed', -1, '', '',
                        error='Worker 可能在执行过程中重启',
                    )
                except Exception:
                    logger.exception("恢复重试: 标记命令任务 '%s' 失败", name)
                if self.mgr.destroy_sandbox(name):
                    pending.pop(name, None)
                    cleaned += 1

            for name in sandbox_names:
                record = self.mgr.db.get_sandbox(name)
                if not record:
                    continue

                # A command sandbox is marked DESTROYING before native
                # cleanup.  Retry it even when a crashed command left child
                # processes behind; waiting for ``populated=0`` would make
                # the residual permanent and block the next upgrade.
                if record.get('state') == 'DESTROYING':
                    if self.mgr.destroy_sandbox(name):
                        cleaned += 1
                    continue

                # CREATING spans native create through the first successful
                # join.  A slow host launch or Docker exec must not be
                # mistaken for an orphan merely because it exceeds the
                # reaper interval; startup recovery handles stale CREATING
                # rows after a process restart.
                if record.get('state') == 'CREATING':
                    continue

                # 容器任务: 沙盒 cgroup 里永远没有进程（容器不住在里面），
                # 所以"空"不等于"已结束"。名下还有活着的容器就不能回收 ——
                # 否则设备预留和容器归属会一起被释放，容器会在失去授权的
                # 状态下继续跑，表现成"驱动装了没生效"。
                if self.mgr.containers_of(name, alive_only=True):
                    continue

                try:
                    snapshot = cgroup.read_snapshot(name)
                except OSError as exc:
                    # 无法确认时保留资源并在下一轮重试；不能把读取错误当空，
                    # 否则可能杀掉仍在运行的任务。
                    logger.error(
                        "收尸无法读取沙盒 '%s' 的 cgroup 状态，将重试: %s",
                        name, exc,
                    )
                    continue

                if snapshot is None:
                    logger.warning(
                        "清理残留沙盒记录 '%s' (cgroup 已不存在)", name
                    )
                    if self.mgr.destroy_sandbox(name):
                        cleaned += 1
                    continue

                pids, populated = snapshot
                if record.get('pids', []) != pids:
                    self.mgr.db.update_sandbox_pids(name, pids)
                if populated:
                    continue

                # create 和首次 join 之间允许一个 Reaper 周期，避免刚创建
                # 的空 cgroup 被后台线程提前回收。
                try:
                    created_at = float(record.get('created_at') or 0)
                except (TypeError, ValueError):
                    created_at = 0
                if time.time() - created_at < interval:
                    continue

                # 销毁前再次读取，覆盖第一次读取后刚发生的 join/fork/exit。
                try:
                    confirmation = cgroup.read_snapshot(name)
                except OSError as exc:
                    logger.error(
                        "收尸二次确认沙盒 '%s' 失败，将重试: %s",
                        name, exc,
                    )
                    continue
                if confirmation is not None:
                    confirmed_pids, confirmed_populated = confirmation
                    if confirmed_pids != pids:
                        self.mgr.db.update_sandbox_pids(name, confirmed_pids)
                    if confirmed_populated:
                        continue

                logger.warning("清理空沙盒 '%s' (cgroup 层级无进程)", name)
                if self.mgr.destroy_sandbox(name):
                    cleaned += 1

            # 补充：文件系统孤儿扫描（cgroup 目录存在但 DB 无记录）。
            # destroy_sandbox 会验证 cgroup 与 eBPF 清理结果；失败不计为
            # cleaned，下一轮仍会从文件系统再次发现并重试。
            db_names = set(self.mgr.list_sandboxes())
            fs_names = set(self.mgr.list_sandboxes_via_native())
            orphans = fs_names - db_names
            orphan_cleaned = []
            for name in orphans:
                logger.warning("清理文件系统孤儿沙盒 '%s' (DB 无记录)", name)
                if self.mgr.destroy_sandbox(name):
                    cleaned += 1
                    orphan_cleaned.append(name)
            if orphans:
                logger.warning(
                    "文件系统孤儿清理完成: 成功=%s, 待重试=%s",
                    sorted(orphan_cleaned),
                    sorted(orphans - set(orphan_cleaned)),
                )

        # 无主容器清扫必须留在 ``self.mgr.lock`` 外面：这一步真的要连 Docker，
        # 不能让 dockerd 的延迟把创建/销毁一起拖住。沙盒记录还在的容器这里
        # 一概不碰，归它们自己的销毁路径管。
        orphans = self.mgr.reap_orphan_labelled_containers()
        if orphans:
            logger.warning("收尸: 收掉 %s 个无主容器", orphans)

        return cleaned


    # ── 定时收尸（Reaper） ───────────────────────────────────────

    def _loop(self):
        """后台收尸线程主循环。每隔 sandbox_reaper_interval 秒执行一次收尸。"""
        # interval 在循环内、try 内读：配置写坏时不能把收尸线程直接带走
        # （线程死了就再也没人听容器退出事件了），夹到 >=1 秒避免忙等。
        interval = None
        logger.warning("定时收尸线程已启动")

        while True:
            try:
                if interval is None:
                    interval = max(
                        1, env_int("NEU_BOX_SANDBOX_REAPER_INTERVAL", 30))
                    logger.warning("收尸间隔 = %ss", interval)
                t0 = time.monotonic()
                logger.debug("开始收尸扫描...")
                cleaned = self.run_once()
                cleaned += self.mgr.reconcile_containers()
                remaining = len(self.mgr.list_sandboxes())
                if cleaned > 0:
                    logger.warning("本轮收尸完成: 清理=%s, 剩余沙盒=%s", cleaned, remaining)
                else:
                    logger.debug("本轮收尸完成: 清理=%s, 剩余沙盒=%s", cleaned, remaining)
                # 用实际耗时修正等待，保证间隔稳定。这里的等待是 epoll 而
                # 不是 sleep：容器退出的事件会把它立刻叫醒，收尸周期不会
                # 拖慢"容器已经没了、登记还在"的清理。
                elapsed = time.monotonic() - t0
                retired = self.mgr.wait_container_events(max(0, interval - elapsed))
                if retired:
                    logger.warning("容器退出事件: 注销 %s 条归属", retired)
            except Exception as e:
                logger.error("收尸异常: %s", e, exc_info=True)
                self.mgr.wait_container_events(interval or 30)

    def start(self):
        """启动后台收尸线程（daemon 线程，随主进程退出）。"""
        t = threading.Thread(target=self._loop, daemon=True, name='sbx-reaper')
        t.start()
        return t
