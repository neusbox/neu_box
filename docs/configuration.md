# Neu Box Worker 配置

Worker 从 `/etc/neu-box/worker.env` 读取配置，读取 `NEU_BOX_*` 变量和 `LOG_LEVEL`；旧 shell 配置键不兼容。安装与升级流程见 [部署与升级手册](deployment.md)。

## 配置变量

| 变量 | 默认值 | 含义 |
|---|---|---|
| `NEU_BOX_PORT` | `59075` | Worker 监听端口 |
| `NEU_BOX_LISTEN` | `0.0.0.0` | Worker 监听地址 |
| `NEU_BOX_HTTP_THREADS` | `8` | Waitress HTTP 线程数 |
| `NEU_BOX_DEVICE_FILTER` | `davinci[0-9]+` | 设备名完整匹配正则；GPU 可设为 `nvidia[0-9]+` |
| `NEU_BOX_DB_PATH` | `/var/lib/neu-box/worker/neu_box.db` | SQLite 数据库 |
| `NEU_BOX_TASK_LOG_DIR` | `/var/lib/neu-box/worker/task-logs` | 任务日志目录 |
| `NEU_BOX_LOG_DIR` | `/var/log/neu-box` | 服务日志目录 |
| `NEU_BOX_BACKUP_DIR` | `/var/backups/neu-box` | 数据库及维护时的配置备份目录 |
| `NEU_BOX_SANDBOX_EXECUTABLE` | `/usr/libexec/neu-box/neu-box-sandbox` | 沙盒管理 CLI |
| `NEU_BOX_DEVICE_INFO_SCRIPT` | `/usr/share/neu-box/info/npu_info.sh` | 设备状态脚本；GPU 节点改为 `gpu_info.sh` |
| `NEU_BOX_SANDBOX_REAPER_INTERVAL` | `30` | Reaper 扫描间隔（秒） |
| `NEU_BOX_COMMAND_TIMEOUT` | `0` | 命令超时（秒），`0` 表示不限制 |
| `NEU_BOX_COMMAND_MAX_COMPLETED` | `200` | 已完成任务保留上限 |
| `NEU_BOX_COMMAND_QUEUE_RECENT` | `30` | 状态接口返回的近期任务上限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

RPM 默认配置模板见 `deploy/config/worker.env.example`。

## 旧配置迁移

`setup` 会把旧配置迁移到新键：

- 旧版小写键转换为 `NEU_BOX_*`；
- `db_dir` 转换为 `NEU_BOX_DB_PATH`；
- 旧 `/opt` 或 `sandbox.sh` 路径指向 RPM 提供的 native sandbox 和设备信息脚本。

迁移是幂等的；命令行显式环境变量优先，未知或自定义配置会保留。修改配置后按“暂停、修改、`setup`”的顺序恢复，不能直接重启服务。
