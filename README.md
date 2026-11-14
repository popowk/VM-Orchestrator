# VM Orchestrator — 功能总览（v1.4.50）

VM Orchestrator 是基于 Windows + VMware Workstation Pro 的多客户虚拟机运营管理系统。正式发布包仅包含运行程序与必要安装/运维脚本；此文件为唯一的产品说明。

## 主要功能

- **统一后台**：管理员登录、CSRF 防护、客户实例和商品模板绑定、实例状态、资源配额、快照、开关机、续费及到期策略。
- **思博业务系统**：使用 `integrations/sibhosting/public/plugins/host/vmo_vmware` 中的 v1.3.3 插件；支持订单开通、暂停、恢复、终止、续费与到期同步、状态/部署任务查询，以及思博 V3 `template + vars` 客户详情页。**思博插件安装在思博服务器，升级 VMO Host 不会自动替换已安装的 PHP 插件。**
- **客户门户**：一次性票据登录、服务状态、截图、快照和关机/重装自助、执行历史、限额、公告弹窗。公告按原文纯文本保留换行、空行、缩进，后台预览与客户端使用一致的空白字符规则。
- **远程运维**：WSS Guest Agent、CMD（8,000 字符）、PowerShell（12,000 字符）远程命令、实时状态、WSS 诊断、可开启/关闭的约 1 秒截图预览。
- **Guest Agent V2**：HTTPS/WSS 身份认证、心跳和指令、自动检测匹配版本并安全升级；旧 Agent 的独立 V1 文件为兼容既有客户保留。
- **系统更新**：管理员检查 GitHub Releases，一键下载并校验 SHA-256；比较版本包实际文件变化，仅 HTML/CSS/JS/说明时原子热替换且不重启服务；Python/依赖/Guest 变化时由独立 PowerShell 接管、备份、重启、健康验证及失败回滚。升级状态按单次尝试编号记录，日志不混用历史成功记录；新旧服务 PID 和健康版本联合校验，未确认接管时明确提示人工核查。首次在正式宿主机操作前应备份并验证回滚。
- **网络与权限**：Host 8765 管理端与 Guest 8766 HTTPS 网关运行在统一 Python 后端进程中，权限隔离，客户不得访问管理员 API。

## 目录说明

| 位置 | 用途 |
| --- | --- |
| `Install-All.cmd` | 首次安装或手动升级入口 |
| `Start-VM-Orchestrator.cmd` | 启动服务 |
| `Manage.cmd` | 日常管理菜单 |
| `Repair-Stuck-Update.cmd` | Windows 更新卡住时的恢复入口 |
| `app/` | Host、API、授权、业务逻辑与后台/客户 UI |
| `guest_agent/` | Guest Agent V1 兼容层、V2 服务及安装脚本 |
| `scripts/host/` | Host 安装、网络、更新器和维护模块 |
| `scripts/guest/` | Win10 模板、OOBE 和注册工具 |
| `integrations/sibhosting/.../vmo_vmware/` | 可部署的思博 V3 插件 v1.3.3 |
| `integrations/app-report/` | 应用状态上报示例 JSON |
| `tools/`、`.github/` | 源码发布及 Release 构建（正式更新 ZIP 不包含） |

## 安装与升级

1. Windows 账户需具备 VMware、Python、PowerShell 和所需文件权限。建议先备份整个安装目录、`data/`、虚拟机目录与配置密钥。
2. 解压到与已安装目录**不同**的位置，在旧版 8765/8766 停止后使用 `Install-All.cmd` 或 `Install-All.ps1` 安装。不要直接删除 `data`，也不要在模板机里预置真实客户注册信息。
3. 打开 `http://127.0.0.1:8765/health` 确认版本及健康状态，再测试后台、WSS、客户门户及一台测试 VM。
4. 将 `integrations/sibhosting/public/plugins/host/vmo_vmware/` 整个目录复制到思博服务器对应插件路径，保留插件 ID `vmo_vmware`，重启 PHP-FPM/清理必要模板缓存后验证客户端详情页及一次性票据登录。
5. 生产更新建议经管理后台「系统更新」获取 GitHub 正式 Release；需要先在备份环境验收 Windows 独立更新/回滚。

## 运维及安全提醒

- 8765 管理端不要直接暴露到公网，远程客户只允许访问必要的客户路由；业务密钥不能与管理员密钥混用。
- 保留 Windows Host 与 Guest 的数据库、证书、DPAPI 密钥、客户身份与安装数据；代码升级不得覆盖它们。
- 更新检查和 SHA-256 校验解决完整性与传输差错，不等同于签名验证；管理后台应限制管理员权限。
- 目前发布包的语法、HTTP 和模拟场景可测试，但实际 VMware Workstation、思博环境、Windows 服务重启与回滚仍需在目标机器验收。


### Windows updater reliability (v1.4.50)

On Windows, the Host validates the copied updater using the local PowerShell
parser, writes it as UTF-8 with BOM, then launches it with Unicode-safe
parameters and a per-attempt startup acknowledgement. An upgrade from older
versions whose launcher is already failing should use the standalone
`Repair-Stuck-Update.ps1` recovery path once.


## Guest Agent V2-only

New customer Windows guests install only the V2 automatic Windows service. The same V2 service securely enrolls with the VM-specific one-time token over the pinned HTTPS Guest Gateway, maintains WSS, and applies vetted per-VM Agent update packages with SHA-256 validation and rollback. The V1 polling agent, V1 installer, and V1 task are no longer shipped or supported. Existing enrolled V2 identities are retained during updates.


## Verify system upgrades

The update status now compares the currently serving Host version, the on-disk
VERSION file and the requested release. A historical `success` entry does not
prove that the intended process is serving requests. The detached updater
checks ownership of both ports by the same new PID and verifies the live and
disk versions across three consecutive probes before recording success.
If those versions disagree, inspect the installed directory and process
startup method before retrying. Static UI-only updates remain restart-free.


### Windows updater PID=0 recovery (v1.4.50)

When manual recovery starts with no Host listener, the detached updater no
longer queries Windows ProcessId=0 (System Idle). If VMO is still running or
restarts before replacement, its PID is verified using both ports 8765/8766,
the Python process name, /health PID/capability/version and the local VERSION
file. A redacted WMI CommandLine is accepted only if all positive checks pass.
Any ambiguous service or mismatched version blocks replacement. Failed
upgrades only restore code after checking that both ports are free.

## Disk cleanup and safe Host launch (v1.4.50)

Authenticated administrators can preview and delete *only* recognized VMO
update staging directories (older than 7 days), obsolete updater logs (older
than 30 days), and old code rollback directories (older than 30 days, always
retaining the two newest). Old program rollback directories are opt-in.

Completed reinstall backups are a **separate manual-only** category. A backup
must be identified by a SUCCEEDED vm_reinstalls DB row, be a direct child of
Customers/.vmo-reinstall-backups with the expected name, have an intact current
replacement VM, and pass VMware power, lock, reparse, and linked disk guards.
The irreversible delete action requires a fresh fingerprint and typed approval.
On success the backup_deleted_at timestamp is added without deleting reinstall
history. Failed or ambiguous backups are never automatically deleted. Running
VMs, golden templates, databases, credentials, and approved update packages are
outside the cleanup allowlist.

Host updates now launch a hidden PowerShell Host instead of a new CMD console.
The manual CMD launcher no longer pauses after an error, and a previously
verified legacy launcher may be closed individually. Other cmd.exe processes
are never targeted.


### Release integration audit

A release must include the cleanup API routes, the admin navigation, the separate
old-VM backup selection, and the cleanup manager. CI checks their presence to
prevent a clean build with an unwired feature.


## v1.4.54 — C:\dsh 便携软件自动更新

每台已注册的 Windows Guest Agent v2.0.25+ 自动只读扫描 C:\dsh 下最多 32 个一级程序目录，并向管理员界面上报发现的程序、候选启动文件、版本及版本证据来源。不运行任何发现的软件。扫描到的目录本身不是升级授权：只能由管理员审核并上传完整的 ZIP 发布包。

在「软件更新」上传审核过的 ZIP：只需填写软件名称和启动文件名；ZIP 只能包含一个顶层程序目录。例如，MyApp.zip 内为 MyApp/MyApp.exe、MyApp/VERSION、MyApp/assets/...，客户端目标目录即 C:\dsh\MyApp。启动文件只能是该目录中的 EXE、BAT 或 CMD（可以是相对路径，不能带命令参数）。程序目录和启动文件一经登记，不允许新上传包在无提示情况下更换启动文件。

版本识别：优先从上传包的 VERSION、version.txt、package.json、app.json、manifest.json 或可选自定义版本文件读取；管理员可手动覆盖版本。若无可识别版本，Host 以 build-加 SHA-256 前 12 位作为唯一包指纹，明确不将指纹冒充软件语义版本。Guest 升级后还能读取安装文件的 EXE ProductVersion/FileVersion。两种版本来源分别记录，避免从错误的 EXE 判断安装包版本。

持久化记录：
- portable_profiles：软件名称、ZIP 顶层目录名、对应 C:\dsh 目录、启动文件、可选版本文件和识别规则。
- portable_package_meta：每次上传对应的软件档案与版本识别来源。
- update_packages：审核发布包、软件版本、SHA-256 校验值、大小和上传时间；不从未批准的远程 URL 执行下载。
- guest_tasks：每台客户机的更新任务、状态、更新阶段及最终处理结果；Guest 心跳单独保留真实已安装版本和来源。

安装流程：先经独立 HTTPS 认证通道下载并校验 ZIP → 验证单目录和启动文件 → 解压暂存 → 继承已有用户数据/配置等需要保留的目录 → 检查当前程序是否运行 → 若原先运行，先安全识别和停止其自身进程 → 原目录改名为带任务号的备份 → 新目录切换 → 按相同版本来源核对 → **原来运行则以原 Windows 登录用户会话启动并验证；原来关闭则保持关闭**。失败时尽可能恢复原文件夹、运行状态，保留诊断目录；未知结果不自动重复执行。更新前备份的旧版本默认不会立即自动删除。

可单机下发、批量更新全部匹配的在线客户机，或在上传时勾选并确认“上传后自动下发”。旧版安装器适配器保留在高级兼容区。该方案仅针对可整体替换的便携目录，不替代 MSI、驱动、Windows 服务或依赖特定静默安装参数的软件。Guest 需先升级到 v2.0.25；Windows 安全软件的进程控制规则需在真实宿主机验证。



## v1.4.57 — 客户布局、挂起误判和单台/全部软件首次部署

**关机状态**：vmrun 的运行列表不能区分已关机与挂起。新版同时只读检查 VMX 的 \`checkpoint.vmState\`：如果 VMX 引用现有 .vmss 则报告“挂起”；引用损坏/文件不存在报告“未知”；引用为空但目录存在 .vmss 时报告“已关机（残留挂起文件风险）”。**界面上的“已关机”不代表可以重装**：所有有残留 .vmss/.lck 的实例仍会被服务器端的安全检查阻止，且不提供自动删除或丢弃挂起内存的操作。需先在 VMware Workstation 中核实并妥善处理。屏幕截图只读且 16:9；电源和重装左右并排，快照在下方；客户自助操作及最近两条执行记录左右并排。

**统一发布**：管理员上传 ZIP 只是创建一条审核发布记录，不会自动部署。软件版本库每个版本只有一个“单台或全部在线客户机”下拉框和“下发软件”按钮。全部部署必须单独确认；服务器仅给 READY、已注册、十分钟内有心跳且 Guest Agent **2.0.27 或以上** 的客户机排队。离线/忙碌/Agent 过旧的客户机明确列为跳过，不会在将来突然自动安装。每台 VM 独立校验 ZIP/SHA-256、执行并回报任务。

**首次安装**：如果 Guest 的 \`C:\dsh\<ZIP顶层目录>\` 不存在，使用新的事务化首装：验证相对启动文件名、解压暂存、建立任务所有权标记、原子切换为指定目录、按版本文件核验；由于原来未运行，**默认安装后不自动启动**。如果该目录已经存在但找不到登记的启动文件，则拒绝覆盖，以保护未知应用数据。替换已有程序时维持原有逻辑：先记录运行状态，必要时停止旧进程，保留原目录备份，替换文件；原来运行就重启并验证，原来没运行则保持关闭。失败会尽可能回滚/隔离本任务创建的目录，不能证明归属时必须人工处理。部署锁防止同软件被同时更新。

**记录**：客户最近执行记录最多两条，仅展示当前客户能查看的经过凭据过滤的 stdout/stderr 或软件任务关键结果；管理员远程运维的客户自助操作采用下拉选择+执行，最近执行记录每页五条。管理员服务仍保留进程日志及审计记录。

新版本不包含远程鼠标或键盘控制，也不支持将普通 MSI/驱动安装包当作便携目录更新。GitHub Actions 的静态检查和自动测试不能替代用户实际 Windows、VMware、火绒环境的部署与重装验证。


## v1.4.58 · 持久自动推送 / 单一生效版本 / 右侧客户管理栏

**软件版本库**每个便携程序目录只有一个 *当前生效 ZIP 发布版本*，仍保留历史 ZIP、任务结果和审计记录（历史版本不再显示为可下发选项）。同一个软件名称不能绑定到第二个程序目录。上传完全相同的 ZIP 视为重复提交，不会再次推送；若 ZIP 内容变了而版本号未变，上传会报冲突，请提升版本号。上传新版会停用之前的发布，并安全取消尚未领取的旧版本任务；已领取/正在执行的更新不会强制终止，由原来的事务保护完成。

管理员在每个软件版本详情中单独启用 **“自动推送全部客户机”**（默认关闭、明确二次确认），系统将对符合条件的在线客户机自动下发当前版本；Guest Agent 2.0.27+ 后续上线或恢复心跳时，也会自动领取这个软件的最新版本。每个 ZIP/VM 组合自动提交最多一次；失败、断连结果未知和正在运行的任务都不会被重复自动执行。新版本发布后产生新的 package ID，作为下一次自动部署的依据。没有安装软件时是首次安装并保持关闭；已安装则按原运行状态更新。离线、未注册、Agent 版本不足、VM 不是 READY 或已有任务的客户机不会盲目入队。关掉开关会取消尚未领取的自动任务，但不会中断已经开始的更新。需要人工重装时仍可手动选择单台或全部。变更仅对当前软件档案生效，不会启用其它软件的自动部署。

**客户网页**：宽屏电脑采用约 80% 实时屏幕截图和约 20% 纵向管理栏；右侧从上到下依次是电源操作、系统重装和快照管理。窄屏自动叠放。截图容器仍为 16:9，首次截图成功加载后隐藏“尚未获取屏幕截图”占位，避免出现多余的深色底栏。屏幕仅供单次只读截图，不开放键盘鼠标控制。

**保护约束**：不会静默停止老版正在执行的更新，也不会把旧 ZIP 自动恢复为当前版本；仅允许每个软件目录一个生效版本。历史自动部署失败不自动重试，需管理员核实并手动处理。上线前建议先选择测试客户机验证防护软件及应用启动行为。


## v1.4.59 · 思博客户自助操作管理员预设长命令

「思博客户服务 → 客户自助操作 → 管理员预设命令」与「远程运维 → 远程命令」现在遵循相同的最大长度：**CMD 8,000 字符，PowerShell 12,000 字符**（加上原有的 64 KiB UTF-8 JSON 传输上限）。界面根据命令环境动态调整字符数限制，显示实时计数；后端的 Pydantic 与保存校验同样按所选命令环境执行，不再以旧版统一 2,000 字符拒绝长命令。SQLite 的 TEXT 列保持不变，已有预设和历史执行记录不受影响。

此调整只允许管理员保存更长的预设，不向客户泄露命令正文，也不绕过原有租户/商品权限、CSRF、审计、请求去重、30 秒防重复触发和 Guest WSS 认证。实际触发仍经过远程运维同一个 RemoteStore.queue 命令运输通道，且与 Guest Agent 执行器的对应字符限制一致。对于超过 2,000 字符的命令，目标 Guest Agent 需要 v2.0.21 或更新版本；某些 CMD 命令因引号/参数展开可能触及 Windows 自身 8,191 字符命令行上限，届时需要使用文件脚本或 PowerShell。


## v1.4.60 — GitHub 正式 Release 自动检查与缺失备份记录

### 软件更新 GitHub 源
先上传一次安全的便携 ZIP，登记程序目录和启动文件。然后在软件版本库展开对应软件，设置公开 GitHub 仓库地址，例如 https://github.com/popowk/dsh-fish-soul-bridage 。
自动检查默认关闭；启用后 Host 启动后和每约六小时读取仓库最新正式 Release。需要时可手动点击“立即检查并验证”。
仅允许公开 github.com/所有者/仓库 URL。只接受该仓库正式 Release 内的 ZIP 文件（如果有多个 ZIP 必须精确选择），不下载 GitHub 源代码 ZIP。
验证包括：HTTPS 来源、允许的 CDN 重定向、大小最大512MB、GitHub digest（若提供）、SHA-256、ZIP CRC、单顶层目录、启动文件、VERSION 一致性、版本非降级、同版本不同 SHA 拒绝。
全部验证完成后才会注册新包并替换服务器唯一生效版本；如果该软件的客户端自动推送开关为开启，则向符合条件的在线 Guest 下发；否则只更新服务器版本库。
之前的客户自动推送策略不受 GitHub 检查开关影响。未安装时首装默认保持关闭，已安装时按之前运行状态恢复。失败、未知执行结果不自动重试。
该示例仓库的 Windows-Full ZIP 顶层是 dsh-fish-soul-bridage，内有 Start.cmd 和 VERSION，由项目自身发布工作流验证。

### 历史重装备份文件缺失
如果重装数据库记录真实存在、路径符合规范，但备份目录已经不存在，空间清理另列为“已缺失的历史重装备份记录”，按0字节显示。
管理员勾选并输入“确认备份缺失”，仅给历史行写入 backup_missing_at 时间，保留 SUCCEEDED、原路径、全部重装审计，不写 backup_deleted_at，也不删除任何文件。
目录重新出现时不能确认缺失；路径不匹配和数据库无关联的孤立备份一律只标记为受保护，不能自动删除。
