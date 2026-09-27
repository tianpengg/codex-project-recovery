# Codex 项目与聊天记录关联恢复（Windows）

电脑重启或 Codex 更新后，侧边栏的项目文件夹突然消失，原来的聊天全部出现在“最近”。重新添加同一个文件夹，历史聊天仍没有回到对应项目。

本仓库把一次实际恢复成功的方法整理成通用 Python 脚本与中文操作说明。使用者已经确认项目及历史聊天恢复。**这是社区恢复方案，不是 OpenAI 官方工具，也不保证适用于所有版本。**

## 适用条件

- Windows 上的 Codex 桌面应用。
- 项目源文件仍存在，Codex 本地数据库中的项目与聊天记录仍存在。
- 数据目录内有 `.codex-global-state.json` 和 `state_5.sqlite`，且数据库包含脚本要求的 `projects`、`project_roots`、`threads` 字段。
- 故障属于项目登记、项目 ID 映射或聊天归属丢失。若只是界面分组设置变化，先检查设置。

如果项目或聊天已经被删除、数据在另一个 Windows 用户目录、或数据库结构发生变化，本工具不会自动重建。脚本遇到不兼容结构、归属冲突或目录不存在时会停止。

## 为什么重新添加文件夹不一定能解决

项目目录、聊天内容和侧边栏归属属于不同数据。恢复同一个目录的项目入口，不一定恢复旧聊天的项目关联。

本工具读取已有数据库，重建界面状态中的项目登记、排序、旧/新项目 ID 映射和聊天归属，并更新数据库的 `threads.project_id`。同时从无项目列表、待迁移列表中移除已恢复的聊天 ID。

它保留聊天正文、源代码、归档状态及其他聊天字段；不操作登录、文件夹信任或权限设置。

## 快速使用

需要 **Python 3.10 或以上版本**，只有标准库依赖，无需 `pip install`。Python 可从 [官方网站](https://www.python.org/downloads/windows/) 安装。

下载仓库并解压，在脚本目录打开 PowerShell。以下命令使用 Windows Python 启动器 `py`；也可将 `py -3` 换成 `python`。

### 1. 生成计划（只读）

```powershell
py -3 -X utf8 .\recover_projects.py prepare
```

默认数据目录为环境变量 `CODEX_HOME`，未设置时使用当前用户目录的 `.codex`。计划生成到脚本旁的 `.local-recovery/`。此步骤不修改 Codex 数据，可在应用运行时执行；如果正在写入造成读取失败或不一致，完全退出应用后重新生成。

有故障前的完整界面状态快照时，建议提供它：

```powershell
py -3 -X utf8 .\recover_projects.py prepare --snapshot "D:\backup\old-global-state.json"
```

快照是完整 JSON 状态对象，不能是聊天文件或 SQLite 文件。没有旧快照也可以生成计划，但缺失的归属将按**工作目录与项目根目录完全相同，且唯一匹配**推断。不要将目录相同视为旧归属一定相同：原本刻意放在“最近”的聊天也可能被推断进项目，请先核对计划。

### 2. 查看恢复范围

```powershell
py -3 -X utf8 .\recover_projects.py preview
```

查看项目名、目录、聊天数量、项目 ID、归属依据与跳过原因。完整计划位于 `.local-recovery/recovery-plan.json`，可只读检查。修改计划会导致摘要校验失败，需要重新生成。

### 3. 完全退出应用，再试恢复一个项目

先保存工作、等待正在执行的任务结束，通过退出菜单完全退出 Codex 和 ChatGPT。只关闭窗口可能仍有后台进程。脚本检测到相关进程会停止，不会自动结束进程。

```powershell
py -3 -X utf8 .\recover_projects.py apply --project "示例项目"
```

将 `示例项目` 换成预览中的真实项目名称，重名时使用项目 ID。脚本先备份，然后写入恢复配置。重新打开 Codex，确认该项目与历史聊天已经恢复。

### 4. 单项目成功后，再恢复全部

再次完全退出应用，然后运行：

```powershell
py -3 -X utf8 .\recover_projects.py apply --all
```

重新打开 Codex，检查项目列表及旧聊天。重复恢复相同范围不会重复添加项目或聊天。

也可以使用启动包装：` .\run.cmd prepare`、` .\run.cmd preview`、` .\run.cmd apply --project "示例项目"`。双击 `run.cmd` 不会自动执行恢复。

## 匹配与跳过规则

1. 只处理已识别的普通聊天来源 `vscode`、`cli`、`app`，跳过内部子任务和未知来源。
2. 优先采用当前/旧状态文件中的明确本地归属；与已有数据库归属冲突时停止。
3. 没有明确归属时，保留已有有效数据库归属。
4. 旧快照中明确无项目的聊天，不通过工作目录推断分配。
5. 剩余聊天仅使用唯一的精确根目录匹配，不按父目录、子目录或名称猜测。
6. 无法确认的聊天跳过；归档聊天保留归档状态，不会被取消归档。
7. 仅恢复当前数据库中已登记的项目，不创建仅见于旧快照的项目。

## 备份与撤销

每次写入前，保存到 `.local-recovery/backups/时间戳/`：

- SQLite 备份 API 生成的完整数据库备份，并校验完整性。
- 写入前后的界面状态。
- 本次修改的字段与数据库行，以及执行状态 `receipt.json`。

写入使用 SQLite 事务及 JSON 原子替换。一般异常会回退已写入的 JSON 和数据库事务。**两个文件不是同一个事务，断电或进程被强制结束仍可能中断；遇到这种情况应保留备份并先尝试撤销。**

完全退出应用后撤销最近一次恢复：

```powershell
py -3 -X utf8 .\recover_projects.py undo
```

撤销仅回退记录中的项目相关字段和归属行。如果应用已经继续改写这些字段，工具会拒绝覆盖新变化。此时保留备份并人工核对；不要直接覆盖整个数据库，否则可能丢失备份之后的新聊天。先试一个项目、再恢复全部会生成两份备份，撤销时每次回退一份。

## 自定义数据目录

```powershell
py -3 -X utf8 .\recover_projects.py prepare --home "D:\CodexData" --work-dir "D:\CodexRecovery"
py -3 -X utf8 .\recover_projects.py preview --home "D:\CodexData" --work-dir "D:\CodexRecovery"
py -3 -X utf8 .\recover_projects.py apply --all --home "D:\CodexData" --work-dir "D:\CodexRecovery"
```

后续命令必须使用同一组目录参数。不要把不同电脑的数据混合到同一个恢复目录。

## 公开分享与隐私

仓库仅包含通用脚本、说明和虚构数据测试。本机生成的计划、数据库、快照、备份含真实项目路径及聊天 ID，**请勿提交到公开仓库或贴进公开 Issue**。默认生成目录已加入 `.gitignore`，自定义工作目录需自行排除。脚本不联网、不收集或上传本机数据。

## 测试

```powershell
py -3 -X utf8 -m unittest -v test_recover_projects.py
```

测试全部使用临时目录与虚构数据库，覆盖范围选择、来源过滤、快照保留、重复执行、备份撤销、异常回退及冲突保护。测试中模拟进程退出检查；正式写入始终执行实际进程检查。

## 参考与限制

- [OpenAI Codex Issue #36663：社区恢复讨论](https://github.com/openai/codex/issues/36663)
- [OpenAI Codex Issue #42739：项目丢失相关报告](https://github.com/openai/codex/issues/42739)

本工具使用内部本地状态字段。Codex 更新后字段或数据库结构可能变化，即使结构校验通过也无法保证界面行为相同。适用版本以实际预览和单项目恢复结果为准，不能将本次成功推广为所有项目消失问题的通用修复。
