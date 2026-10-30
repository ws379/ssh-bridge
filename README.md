# SSH Bridge

SSH Bridge 是一个面向 Codex 的轻量 MCP 服务，通过本机现有的 OpenSSH 配置连接远程开发机。它把远程工作区、文件操作、命令执行和端口转发收敛到精简的工具集，并复用 SSH 会话、已打开的项目和保存的任务，减少重复连接、重复说明和不必要的上下文输出。

项目不需要在远程机器安装 Codex CLI、常驻守护进程或第三方 Python 包。文件和命令通过压缩后的 Python worker 按需执行。

## 功能

- **项目工作区**：打开项目时一次性汇总目录概览、Git 状态、工具链和检测到的任务；后续操作可用相对路径继续。最近项目和任务按主机、项目隔离保存。
- **少而精的工具目录**：默认只展示 `workspace`、`read`、`edit`、`batch`、`exec` 和 `open_browser` 六个工具。工作区工具还提供搜索、日志尾部、任务保存与重跑、结果分页、连接重置和端口转发等动作。
- **降低上下文和传输开销**：读取默认限制为 16 KB，可按行读取或用 SHA-256 跳过未变化的内容；命令输出按需分页，文本回执不重复携带完整结果；批处理复用同一次 SSH 调用，完整工具目录还提供批量读取。
- **更快的重复操作**：文件操作复用持久 SSH worker；打开未变化的项目可返回简短状态。批处理可把读取、编辑、写入和测试放进一次往返。
- **可靠编辑**：精确替换必须唯一匹配；写入采用临时文件和原子替换，可使用读取结果中的 SHA-256 检测并发修改；路径检查会解析符号链接并限制在配置的工作区内。
- **远程文件浏览器**：支持目录分页、编辑冲突提示和未保存更改确认。只有支持 MCP Apps 的客户端会显示交互式面板。
- **SSH 端口转发**：建立到远程端口的本机回环转发，也可选择性地为远程命令建立临时 HTTP 代理反向隧道。代理只用于 `exec` 和批处理中的命令，不改变 SSH 自身的网络路由。
- **训练进程观察**：按名称追踪已有 Linux 进程，一次查看状态、启动时间、工作目录、日志尾部及关联 GPU；记录保存在本机，重连后仍可查询，日志支持增量读取。

## 环境要求

- 本机 Python 3.10 或更新版本，以及 OpenSSH `ssh` 命令。
- 远程主机 Python 3.8 或更新版本。
- 可正常使用的本机 `~/.ssh/config`、密钥或 SSH agent 配置。

## 配置

先在本机 OpenSSH 配置中设置自己的主机别名，例如：

```sshconfig
Host my-server
    HostName example.com
    User developer
    IdentityFile ~/.ssh/your_private_key
```

将 `SSH_BRIDGE_HOST` 设为该别名。SSH Bridge 不内置主机、用户名、密钥或远端目录；主机配置留在本机 SSH 配置文件中。

PowerShell 示例：

```powershell
$env:SSH_BRIDGE_HOST = "my-server"
$env:SSH_BRIDGE_ROOT = "/srv/my-project" # 可选：限制文件工作区根目录
python server.py
```

把仓库作为 Codex 插件使用时，插件清单会从启动环境读取配置变量。修改环境变量后，重新加载插件或重启 Codex。

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `SSH_BRIDGE_HOST` | 本机 OpenSSH 主机别名；必填 | 无 |
| `SSH_BRIDGE_PORT` | SSH 端口覆盖 | 使用 SSH 配置 |
| `SSH_BRIDGE_ROOT` | 文件工具和项目的远端根目录 | 无限制的 SSH 用户路径 |
| `SSH_BRIDGE_REMOTE_PYTHON` | 远端 Python 命令 | `python3` |
| `SSH_BRIDGE_REUSE` | 复用文件 SSH worker；设为 `0` 关闭 | `1` |
| `SSH_BRIDGE_TOOLSET` | `compact` 只展示六个常用工具；`full` 展示完整工具目录 | `compact` |
| `SSH_BRIDGE_PROXY_URL` | 本机回环 HTTP 代理，例如 `http://127.0.0.1:8080` | 无 |
| `SSH_BRIDGE_PROXY_REMOTE_PORT` | 临时反向隧道使用的远端回环端口 | `18080` |
| `SSH_BRIDGE_ALLOW_DANGEROUS` | 明确启用默认拦截的危险命令片段 | `0` |
| `SSH_BRIDGE_STATE_PATH` | 最近项目和已保存任务的本地状态文件路径 | 用户缓存目录 |

状态文件只保存在运行 SSH Bridge 的本机。不要把含有真实主机名、用户名、密钥路径或代理地址的本地配置和运行报告提交到仓库。

## 常见工作流

打开项目一次，后续读写与命令使用该项目上下文：

```json
{"action":"open","path":"/srv/my-project"}
```

保存并复用检查任务：

```json
{"action":"save_task","name":"verify","options":{"commands":["python -m unittest -q","git diff --check"]}}
{"action":"run","name":"verify","options":{"timeout_sec":300}}
```

批量操作可在一条请求里先编辑再运行测试，默认遇错停止。若命令输出较大，工具返回 `output_id`，之后通过工作区 `result` 分页读取。

### 追踪正在运行的训练进程

先打开项目，再用已有 PID 注册一个任务名称，日志路径相对于项目目录：

```json
{"action":"job_track","name":"train","options":{"pid":12345,"log_path":"logs/train.log"}}
{"action":"job_status","name":"train"}
{"action":"jobs","options":{"include_gpu":true}}
{"action":"gpu"}
```

也可以使用 `{"action":"job_status","options":{"pid":12345}}` 临时查看 PID，不保存记录。`job_track` 记录进程的启动身份；同名记录需要 `replace=true` 才能更新。`jobs` 可用 `offset` 和 `limit` 分页，`include_gpu=false` 可省去 GPU 查询。`job_untrack` 只移除本机记录，不会终止远程进程。

用 `job_logs` 增量读取日志；下一次把返回的 `next_offset` 和 `log_id` 传回。文件被替换或缩短到游标之前时，返回 `reset_required=true`，应从零开始读取。若末尾 UTF-8 字符尚未写完整，游标会停在该字符之前，等后续字节到达或增大 `max_bytes` 后再继续。

下面第二条假设首次返回的 `next_offset` 为 `4096`；`log_id` 也应替换成首次返回的实际值。

```json
{"action":"job_logs","name":"train","options":{"offset":0,"max_bytes":4096}}
{"action":"job_logs","name":"train","options":{"offset":4096,"log_id":"上次返回的log_id","max_bytes":4096}}
```

此功能观察已经存在的进程，不会启动、重启或停止训练。进程不再可见时无法推断其退出原因和退出码，返回的 `exit_code` 为 `null`；PID 被复用时会标记 `pid_reused`，不把新进程当成原任务。状态和 GPU 关联是采样结果；GPU 依赖 `nvidia-smi` 和 Linux 父子进程关系，MPS、容器 PID 命名空间或进程被重新托管时可能无法关联。只能读取当前用户可见的进程信息，GPU 列表则反映 `nvidia-smi` 可见的共享设备。

建立远端 3000 端口的本机转发：

```json
{"action":"forward","options":{"remote_port":3000}}
```

返回的本机端口只绑定到 `127.0.0.1`。可用 `ports` 查看活动转发，用 `close_forward` 关闭。

## 安全边界

文件操作可通过 `SSH_BRIDGE_ROOT` 限定目录，并会检查符号链接越界；写入支持哈希冲突检测。删除操作需要显式确认。默认危险命令拦截只是误操作防护，远程 shell 仍以 SSH 用户权限运行，不是安全沙箱。请仅连接可信主机，并按需授予远程账户权限。

## 本地验证

```powershell
python -m unittest -v test_bridge
```

测试涵盖 MCP stdio、连接复用和重连、批处理、输出预算、路径约束、并发写入冲突、分页和代理隔离。Windows 没有创建符号链接权限时，相应测试会跳过；Linux 环境可完整验证符号链接越界检查。

## 设计参考

项目借鉴了 VS Code Remote SSH 的远程项目工作方式、VS Code Tasks 的可复用任务、Zed 的连接复用，以及 JetBrains 的项目级远程工作流：

- [VS Code Remote SSH](https://code.visualstudio.com/docs/remote/ssh)
- [VS Code Tasks](https://code.visualstudio.com/docs/debugtest/tasks)
- [Zed Remote Development](https://zed.dev/docs/remote-development)
- [JetBrains 远程项目](https://www.jetbrains.com/help/idea/work-inside-remote-project.html)
