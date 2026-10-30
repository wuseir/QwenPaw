# DeepTutor 双端学习接续

这是为 QwenPaw 学习准备的独立辅助工具，不是 DeepTutor 官方同步功能，也不修改两个项目的业务代码。

## 每天怎样用

公司和家里都安装完成后：

1. 开始学习：双击 `D:\DeepTutor\继续学习.cmd`，下载最新快照，校验、备份本机旧数据、恢复并打开网页。
2. 在 DeepTutor 原对话中继续学习。中途停在一个知识点没有关系，已经保存的消息会保留。
3. 离开电脑前，先等待生成、上传、建索引等任务完成，或在网页中停止它们。
4. 双击 `D:\DeepTutor\结束学习并同步.cmd`。它停止本机服务，生成加密快照并上传。**看到“上传并读回校验成功”再关闭电脑。**
5. 换电脑后重复第 1 步。同一时段只在一台电脑上学习。

“结束学习并同步”会关掉服务，不会删除学习数据。同步失败时，原数据和 `sync-local/outbox/` 中的加密文件仍在本地。不要用强制推送、删库或覆盖密钥来修复。

## 分支和隐私

- `main`：QwenPaw 源码、学习文档和本工具。
- `deeptutor-sync`：只保存一个 `snapshot.bin`，历史版本由 Git 保留。
- 公开仓库的所有分支都公开，**不存在私密分支**。快照用随机 256 位密钥和 AES-GCM 加密；上传前完成加密，文件完整性由认证标签及内部 SHA-256 校验。
- 密钥只在 `D:\DeepTutor\sync-local\sync.key`，不能提交到 Git，也不要发到聊天中。第一次回家通过 U 盘等独立方式带过去。失去密钥将无法从 Git 解密。
- 分支名称、文件大小和提交时间仍公开。需要连这些信息也保密时，应改用独立私有仓库。
- 原始对话、数据库、备份、配置及密钥不进入 main。上传器使用独立的裸 Git 缓存，只能写 `deeptutor-sync`，不执行 `git add .`。

## 同步范围

按 DeepTutor **1.7.0 已安装源码**核对的本地路径：

| 内容 | 相对于 `runtime/data/` 的路径 |
| --- | --- |
| 会话、消息、轮次事件、部分练习记录 | `user/chat_history.db` |
| 学习进度、笔记、读书、阅读、写作及旧附件等 | `user/workspace/` |
| 长期记忆及记忆摘要 | `memory/` |
| 学习日志 | `learning_journal/` |
| 本地知识库及解析缓存 | `knowledge_bases/`、`parse_cache/` |
| 工作区登记 | `user/.runtime/workspaces.sqlite3` |
| 新版工作区中的附件、输出及数据 | 登记在册的 `user/workspaces/ws_.../`，排除 system 工作区 |
| 历史文件、展示资源、托管会话工作区 | `user/archive/`、`user/.runtime/workspace_presentations/` 和登记在册的会话目录 |

不复制 `user/settings/`、system 工作区的配置快照、模型 API Key、登录状态、MCP 连接配置、程序依赖、前端构建、日志和后台进程。两个电脑的模型配置分别在本地设置。

这里同步的是已经落盘的学习状态，不能把正在执行的模型请求或 Agent 进程搬到另一台电脑。外部数据库、远端 RAG 服务、定时任务、Partner 运行状态也不在范围内。代码仓库、外部引用文件仍须另外拉取。

## 当前支持边界

- 两台 Windows 电脑，**都安装在 `D:\DeepTutor`**，使用同一个 `runtime/data` 绝对路径。
- `deeptutor[full]==1.7.0`；本地单用户、关闭账户登录、SQLite 存储。
- 支持默认工作区及运行目录内由 DeepTutor 管理的工作区。发现外部工作区时会停止，避免漏同步。不要把公司整个项目目录注册进同步范围。
- 单份加密快照不超过 80 MiB，未压缩数据不超过 2 GiB。超过就停止上传，不截断数据；大视频、原始资料和大型向量索引应改用专门存储方案。
- 每次保存完整快照，Git 历史会增长，适合现阶段少量学习材料。不要为了瘦身直接重写分支历史。
- 回家须自行配置模型。旧对话若引用公司电脑上的模型配置，需要在本机重新选择可用模型；不同 Embedding 模型生成的知识库索引不能当成兼容索引使用。

## 公司电脑初始化

本轮由 Codex 代办。重复初始化会被拒绝，避免重置密钥或同步基线。

```powershell
D:\DeepTutor\.venv\Scripts\python.exe -X utf8 D:\DeepTutor\sync-kit\install_sync.py
D:\DeepTutor\.venv\Scripts\python.exe -X utf8 D:\DeepTutor\sync-kit\deeptutor_sync.py init --remote https://github.com/wuseir/QwenPaw.git --device office
```

Git 使用本机已有凭据；没有登录时，先用 Git Credential Manager 正常登录 GitHub，再重试。程序不会索取 GitHub Token 或把 Token 写进仓库地址。网络代理优先沿用当前系统设置，仅作用于同步子进程，不修改全局 Git 配置。

## 家庭电脑第一次接续

先拉取仓库中的最新工具包：`tools/deeptutor-sync/`。已有仓库先检查 `git status -sb`、分支和远端，保留未提交更改，不重置工作区。

### 1. 准备运行环境

核实家里的实际环境，安装 Python 3.12、Git for Windows。然后准备独立虚拟环境：

```powershell
py -3.12 -m venv D:\DeepTutor\.venv
D:\DeepTutor\.venv\Scripts\python.exe -m pip install "deeptutor[full]==1.7.0"
D:\DeepTutor\.venv\Scripts\python.exe -m pip install -r tools\deeptutor-sync\requirements-sync.txt
D:\DeepTutor\.venv\Scripts\python.exe -m pip check
```

需要 Node 22.23.4 Windows x64 便携版，解压后可执行文件位于：

`D:\DeepTutor\tools\node-v22.23.4-win-x64\node.exe`

官方下载：

- [node-v22.23.4-win-x64.zip](https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-x64.zip)
- [官方 SHA-256 清单](https://nodejs.org/dist/v22.23.4/SHASUMS256.txt)

公司已核对的 ZIP SHA-256：`cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20`。家庭仍需核对下载文件，不能沿用公司结论。

### 2. 安装按钮并导入密钥

在 QwenPaw 仓库根目录运行（把 `E:\sync.key` 换成 U 盘上的真实位置）：

```powershell
D:\DeepTutor\.venv\Scripts\python.exe -X utf8 tools\deeptutor-sync\install_sync.py --app-root D:\DeepTutor
D:\DeepTutor\.venv\Scripts\python.exe -X utf8 D:\DeepTutor\sync-kit\deeptutor_sync.py init --remote https://github.com/wuseir/QwenPaw.git --device home --key-file E:\sync.key
```

先导入快照，再开始在家创建对话。若本机已经有学习数据，工具会拒绝覆盖，由 Codex 比较两边并保留原记录。

### 3. 恢复并验证

双击 `D:\DeepTutor\继续学习.cmd`。服务仅监听本机：

- 页面：`http://127.0.0.1:3782`
- 后端：`http://127.0.0.1:8001`

确认会话标题、最后一条消息、附件及学习进度与公司一致，再在本机设置模型，发送一条真实消息检验续聊。**自动化测试不代表家庭网络、模型及浏览器验收已经通过。**

建议发给家里 Codex：

> 阅读 tools/deeptutor-sync/README.md，按实际环境安装同版本 DeepTutor 和同步工具。密钥由我在本机从 U 盘导入，不要读取或输出密钥内容。先接收 deeptutor-sync 快照，检查保存的会话、最后一条消息和附件；模型凭据只在本机设置。不要强制覆盖本机已有记录，不要把运行数据提交到 main。恢复好后，按 docs/learning/ 的进度继续 QwenPaw 第一单元，先让我思考和操作。

## 故障处理

| 提示 | 处理 |
| --- | --- |
| 有运行中/等待输入对话 | 在网页完成或停止，再点击结束按钮；工具不会默默截断它 |
| Git 失败 | 保留本机数据和 outbox；检查网络、系统代理与 Git 登录，修好后重试 |
| 本机有未上传变化，远端也更新 | 停止两边继续写入，让 Codex 对比备份和 outbox；不会自动合并 SQLite |
| 密钥不匹配或文件损坏 | 核实从公司带来的密钥；不要重新生成密钥覆盖原文件 |
| 路径/版本不一致 | 对齐安装路径、DeepTutor 版本后再恢复 |
| 端口被占用 | 检查占用程序，工具不会停止不属于启动器的进程 |
| 恢复中断 | 下一次同步会先回滚旧数据并停止；保留提示和本机备份，再检查原因 |

恢复前备份位于 `D:\DeepTutor\sync-local\backups\`，它们是本机原始数据，不上传。自动回滚并不能覆盖磁盘损坏等所有情况；恢复失败时不要手工删除这个目录。

## 验证与实现依据

在安装 DeepTutor 1.7.0 的独立虚拟环境执行：

```powershell
D:\DeepTutor\.venv\Scripts\python.exe -X utf8 D:\DeepTutor\sync-kit\test_handoff.py
```

测试使用临时目录和本地裸 Git 远端，覆盖完整会话、附件、进度和记忆恢复，真实 DeepTutor 会话存储及 FastAPI 路由读回，WAL 数据、空目录、配置排除、错误密钥、篡改、越界路径、版本/路径不符、运行中对话、回滚、冲突推送和离线上传保留。

冲突检测比较 SQLite 的实际表结构和记录，避免仅因数据库文件头变化而误报。启动页的派生建议缓存也不作为未上传学习内容；修改过的对话、笔记、附件和记忆仍参与比较。

读取实现依据：安装包内 `services/path_service.py`、`services/workspace/context.py`、`catalog.py`、`data_migration.py`、`services/session/sqlite_store.py`、`services/storage/attachment_store.py`、`services/memory/paths.py`。官方导出有快照能力，但已核查路由没有通用外部 ZIP 恢复接口；本工具自行提供加密传输、恢复事务和冲突保护，不宣称这是官方跨设备合并。

这些辅助代码由 AI 按用户要求编写，不能记成学习者独立完成的业务功能。学习进度仍根据本人回答和实际实验更新。
