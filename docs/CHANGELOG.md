## v1.4.40 · Guest Agent automated upgrades and WSS diagnostic categories

- Host checks authenticated Guest WSS heartbeat versions and, when a newer bundled Guest Agent exists, queues a per-VM SHA256-verified upgrade via the existing rollback-capable SYSTEM installer. Enabled by default; set `VMO_GUEST_AGENT_AUTO_UPDATE=0` on Host to disable.
- Existing v2.0.21 Guests receive the first upgrade without installing a separate helper; no new Guest listening port is used.
- Agent upgrades are deferred while remote operations or software updates are queued or running. Retries are at least 90 minutes apart and limited to 3 per 24 hours per VM/version.
- WSS errors now print fixed, non-secret phase categories (enrollment, config, TLS, handshake, identity and session), rather than the uninformative `ValueError (internal error)`.
- Transient socket reset or timeout continues to be retried automatically; trusted CA and per-VM identity stay unchanged.
- Guest Agent bundled version: v2.0.22. As with prior releases, upgrade runner preserves protected state and rolls back if Windows service installation fails. Windows/VMware end-to-end testing remains required.

## v1.4.39（2026-11-11）
- 以用户提供的 v1.4.38 Review-NonBlocking-Fixed 为基础合并，保留客户自助历史状态不阻塞、新任务仍隔离及人工核对。
- 恢复安全的 GitHub Releases 自动更新页和 Windows 独立安装与回滚。
- 恢复 CMD 8,000 / PowerShell 12,000 字符长命令，Agent v2.0.21。
- 恢复 1fps 可关闭的本机 VNC 截图预览，管理员授权，不额外开放网络端口。
- 已合并并检查前端控制与后端端点；生产端到端更新仍需 Windows 真机测试。

## 1.4.38 (2026-11-11)
- Client self-service action button always displays “执行操作”; remove the review-related notice and “查看执行限制” UI.
- UNKNOWN, TIMED_OUT, VERIFY_REQUIRED and ACKNOWLEDGED history records no longer block customer self-service actions or their 30-second cooldown.
- Preserve in-flight execution exclusion, atomic SQLite reservation, idempotency, authorization and Guest-online checks.
- Administrator review remains as an audit action “标记已核对” rather than an unlock requirement.
- Preserve v1.4.37 announcement formatting, five-record views and product-template reinstall fallback.

## 1.4.37 (2026-11-11)
- Keep customer announcement line breaks, add safe preview.
- Fix client action button blocked-state UX and secure UUID fallback.
- Show last five named self-service operations and last five localized VM jobs.
- Preserve durable operation labels and check blockers beyond visible history.

## 1.4.36 (2026-11-10)
- Repair customer reinstall fallback to currently sealed template for same product only.
- Reconcile stale self-service commands and queued/claimed update tasks; review unknown outcomes, prevent automatic retries.
- Admin/customer self-service UI redesign and accessible 48px customer controls.
- Admin review refuses to unblock while Guest command/update is pending; cross-process reservation serialized by SQLite.
- Preserve existing tenant and administrator route isolation.

# v1.4.34 · 单进程 / UI 与发布包整合（2026-11-10）

- 后端改为 `app/unified_server.py`，单 Python 进程同时运行两个独立的 FastAPI/Uvicorn 实例，分别绑定管理端 8765 HTTP 和客户机 8766 HTTPS；不共用路由或监听端口。
- 统一启动入口：`Start-VM-Orchestrator.cmd` → `scripts/host/Start-Unified.ps1`；移除旧版两个后端的单独启动脚本；升级后必须先退出旧服务。
- 管理后台「思博客户服务」拆为客户实例、商品与模板、客户自助、公告与额度、迁移绑定五个工作区，并新增服务搜索、状态筛选与快速额度入口。
- 合并 Guest Agent 与 Windows 10 模板工具中的重复源码：`guest_agent/`、`scripts/guest/` 各保留一份，Windows 10 BUILD Toolkit 通过 `Create-Win10-Template-Kit.cmd` 生成。
- 生产包不再携带测试目录、历史同步脚本、测试数据库、已淘汰启动入口；安装器只将已确认过时的发行文件归档到升级备份，不覆盖现有数据/密钥。
- 保留公告及每月额度、客户操作排序、服务续费与到期强制关机、思博 API 集成、Guest Agent WSS 连接和远程运维业务契约。

---

## v1.4.33 — 客户自助操作、公告和额度管理
- 客户自助操作下拉选择与管理员排序，最近记录仅五条。
- 客户公告首次弹窗，支持1–720小时提示间隔，重新发布新公告强制展示。
- 全局与单客户额度：快照数量上限、北京时间自然月恢复次数和重装次数。
- 客户操作执行前宿主机原子校验，并在同步下发失败时返还次数。

## v1.4.32 — 更新包删除、客户界面精简与命令结果修正

- 更新包库：管理员可删除不再使用的 ZIP。已经绑定自助操作、仍在更新/核验中的包不可删除；原任务记录和审计保留。删除在数据库留下不可再次下发的 tombstone，安全地从下载目录移除 ZIP。
- 客户 UI：移除指定冗余文案；最近任务显示提交/完成的北京时间；不影响 iframe 的分区刷新。
- 自助命令：前台/后台启动以 Guest 已确认的 `launched + PID` 为启动接受结果，不再因 `exit_code=null` 被误判；宿主机兼容旧 Guest 的相同报告；等待模式只把退出码 0 视为完成，超时则标为结果未知且不重试。
- API：`DELETE /api/fleet/packages/{package_id}`，仅管理员可用，浏览器请求要求 CSRF；API 密钥权限沿用现有管理范围。
- Guest Agent v2.0.20 为可选升级：Host 端已经对旧 Guest 的启动回报做兼容修正。

## v1.4.31 — 思博商品黄金模板绑定管理（2026-11-10）

- 管理页面的商品绑定增加修改模板、取消编辑和删除绑定；保持原有业务开通 API 兼容。
- 绑定删除只移除商品映射，不影响模板、已有 VM、客户服务或到期计划；阻止删除正在开通的商品。
- 写入商品绑定更改审计；新增管理员 API 集成测试。

## v1.4.30 — 管理员登录与预览异常处理（2026-11-10）

- 管理员密码允许任何非空长度；连续失败 3 次锁定 15 分钟，SQLite 持久化。
- 清空登录页预填 `admin`；移除重复 Web Console 8765 副标题。
- 客户预览票据表单校验与可诊断的异常响应；增加登录及预览专项测试。
- 保留 v1.4.29 客户自助操作及 v1.4.28 到期软关机策略。

## v1.4.30 — 客户自助操作（2026-11-10）

- 后台按商品预配置 CMD/PowerShell 命令或审核过的 ZIP 更新任务；默认禁用，可随时启用/撤销。
- 客户软件更新板块替换为「自助操作」，不接受客户提供命令、包或 VM 标识；数据库保留操作目录及历史执行审计。
- WSS/Guest 队列复用，超时/未核验结果阻塞后续动作，管理员核实后解除；客户状态分区刷新。
- 修复一次性票据 SQL 读取漏掉 `expires_at` 的旧问题；不改变 v1.4.28 到期关机逻辑。

## v1.4.28（宿主机仅运行期间处理到期）

- Host 启动即检测到期，不安装 Windows 开机启动任务。
- 到期先软关机，600 秒后仍运行才强制关机；SQLite 保存阶段与时间，Host 重启恢复倒计时。
- 续费阻止旧到期任务强制关机；当前业务接口和管理员账户/API 密钥方案不变。
- 取消要求 VMO_EXPIRY_SERVICE_READY / VMO_EXPIRY_ACCEPTED 的健康检查门槛；提供 `host_process_only` 状态说明。

## v1.4.27 — Admin login, revocable keys, Windows paid-expiry scheduler
- Accounts/password changes and server-side revocable sessions; no admin password in browser storage.
- Old admin secret migrates once to a revocable admin API key while remaining OOBE encryption root.
- Business API keys support independent revocation and `service_expiry_v1` guarded by persistent startup task verification.
- Beijing time inputs for customer expiry, UTC Z protocol, SQLite lifecycle scheduler and power-off enforcement.
- Default checked Windows 10 unattended first boot in UI; provisioning status reconciled with actual clone job.

# v1.4.25
- 固定客户应用路径，短期会话凭证分离；fish-soul Bridge 原生路径兼容。
- 保留 Guest Agent v2.0.18 协议与远程升级机制。

# VM Orchestrator 版本记录（精简）

旧版 1.4.4–1.4.23 逐版说明已从运行包移除。以前的完整压缩包可作为历史归档，不要在生产目录反复覆盖旧文档。

- **README-V1.4.10**：VM Orchestrator v1.4.10 · 前台 CMD 工作目录与引号修复 根因
- **README-V1.4.11**：VM Orchestrator v1.4.11 — 上传、应用同源代理和动态工作目录 本次定位的实际故障
- **README-V1.4.12**：VM Orchestrator v1.4.12 — 普通用户免提权应用上报 诊断结论
- **README-V1.4.13**：VM Orchestrator v1.4.13 — 受管软件更新（测试版） 本版在 v1.4.12 基础上新增三种需要**客户机本地显式启用**的更新适配器。首次只在 VM `001` 验证；未完成 Windows 10 / SnowLuma / DSH 实机验收之前，不建议批量更新客户机。
- **README-V1.4.14**：VM Orchestrator v1.4.14：简化软件更新 + 可复制授权码 > 发布说明：2026-11-09（北京时间，UTC+08:00）。此包通过 Linux 离线模拟、单元测试与静态验证，但**尚未在实际 Windows 10 / VMware Workstation Pro 上执行软件停止、启动及更新验收**；先用于 001 测试虚拟机。
- **README-V1.4.15**：VM Orchestrator v1.4.15 — Host 8765 LAN listener and one-click Guest registration Host: 8765 all IPv4 NICs
- **README-V1.4.16**：VM Orchestrator v1.4.16 · 8765 管理员登录闪屏修复 问题原因
- **README-V1.4.17**：VM Orchestrator v1.4.17 · 应用授权码保留、上报时间与转发修复 目标
- **README-V1.4.18**：VM Orchestrator v1.4.18 · SnowLuma / DSH 应用网关 429 修复 为什么会白屏
- **README-V1.4.19**：VM Orchestrator v1.4.19 — DSH 登录 404 和应用并发调优 适用的现场报错
- **README-V1.4.20**：VM Orchestrator v1.4.20 — 应用登录代理兼容修复 修复范围
- **README-V1.4.21**：v1.4.21 — DSH 根路径导航与 SnowLuma API 保存分块回传 修复
- **README-V1.4.22**：VM Orchestrator v1.4.22 — Web Console 公网登录和宿主机一键升级 Guest Agent 1. `https://host.tmd.pet` 登录出现 `Cross-origin login prohibited`
- **README-V1.4.23**：VM Orchestrator v1.4.23 — host.tmd.pet 单域名兼容补丁 本版**只使用 `host.tmd.pet`，不增加通配符 DNS、其他域名或端口**。管理入口继续是
- **README-V1.4.4**：VM Orchestrator V1.4.4 - Safe Guest Service Upgrade This is a maintenance release on top of V1.4.3 System Python. Host Agent still listens on 127.0.0.1:8765 and Guest Gateway uses 
- **README-V1.4.5**：VM Orchestrator V1.4.5 - Task Recovery & Remote Diagnostics **Ports unchanged:** Host Agent `127.0.0.1:8765`, Guest Gateway HTTPS/WSS `192.168.2.99:8766`.
- **README-V1.4.6**：VM Orchestrator v1.4.6 · 北京时间 / WSS 在线诊断 / 授权修复 **基线：用户提供的 v1.4.5。版本是阶段性补丁，不代表六项需求全部上线。** 先在测试 VM 部署并验证，再升级其他客户机。代码离线测试通过，不代表 Windows 服务、VMware Workstation、iKuai 路由及真实 WSS 网络已验收。
- **README-V1.4.7**：VM Orchestrator V1.4.7 · 远程命令、分区刷新与应用转发 版本定位与已完成的范围
- **README-V1.4.8**：VM Orchestrator v1.4.8 · 客户页 HTTP 500 与应用上报模板 确认修复
- **README-V1.4.9**：VM Orchestrator v1.4.9 · WSS 分帧修复与前台交互模式 根因与修复

## v1.4.26
- Coordinated API write timeouts across 8766/Guest to prevent premature 502 during 30s fish-soul operations.
- Preserve the compiled native SnowLuma router adapter JS from host regex rewriting.
- Guest Agent 2.0.19; no customer identity format changes.
- Bounded application request body raised from 256 KiB to 512 KiB while retaining 1 MiB WSS message limits.

---

## v1.4.29 完整发布说明（归档）

# v1.4.29 客户自助操作部署与使用

## 管理员设置

1. 使用现有管理员账号登录 `https://host.tmd.pet`，进入 **「思博客户服务」→「客户自助操作配置」**。
2. 选择已有的思博商品；填写**客户可见名称、说明**，选择操作类型。新操作默认**不启用**。
3. **管理员预设命令**：选择 CMD/PowerShell、等待结果/后台启动/已登录桌面、工作目录和最大等待秒数，填写要执行的固定命令。客户请求只传操作 ID，不传命令内容或参数。若要在 Windows 用户 Profile 下操作 DSH 插件，应选择交互用户模式并单独确认实际版本。
4. **软件更新**：先在「软件更新」上传审核过的 ZIP，配置一个与商品绑定的操作，并选择**固定 package_id**。SnowLuma 可填写在 `C:\dsh\` 下的安装目录；软件更新仍复用 Guest 已有的备份/停止/安装/健康检查逻辑，必要时需在 Guest 配置本机更新适配器。
5. 检查操作确实适合该套餐后，勾选「允许客户执行」并保存。若不应再执行，可在后台立即禁用；已提交的任务不会因禁用自动回滚。

## 客户操作

- 思博服务端仍以现有一次性票据进入 `/client`。只显示所属商品且已启用的自助操作，客户不能输入命令、软件包 ID、目录或 VM ID。
- 点击后弹出确认，提交唯一 request_id；服务端检查业务有效状态、VM 运行状态、WSS 在线、注册 Agent 和套餐权限。
- 操作在现有 WSS 命令队列或 Guest 软件更新任务里排队执行。执行状态单独刷新，已打开的 SnowLuma / fish-soul iframe 不重新加载。
- 操作超时或执行结果未知，客户菜单后续执行会被阻止。管理员先核实 Guest 进程、版本及日志，再使用后台的「已核对现场，解除阻塞」；该动作**仅解除记录阻塞，不重新执行任务**。

## 管理 API（管理员账号或管理员 API Key，修改类请求需 CSRF）

- `GET /api/business/admin/self-actions`：查看自助操作目录（含后台保存的命令，仅管理员可读）。
- `POST /api/business/admin/self-actions`：新建/编辑固定目录项，包含 `product_ref,label,description,kind,enabled` 与命令或包配置。
- `PUT /api/business/admin/self-actions/{id}/enabled`：开启/禁用；`DELETE` 同路径删除目录项（保留任务历史）。
- `GET /api/business/admin/self-executions`：查看任务审核记录；`POST /api/business/admin/self-executions/{request_id}/acknowledge` 人工核对未知/待核验任务。

## 客户 API（独立业务票据会话，修改请求需 `X-Client-CSRF`）

- `GET /client/api/self-actions`：只返回当前套餐可见的 `id,label,description,kind,updated_at`，不返回命令/路径/版本包等内部数据。
- `POST /client/api/self-actions/{id}/execute`：提交 `{ "request_id": "<随机 UUID>", "confirm": true }`，返回 202。重复 UUID 只返回已有任务，不会再次执行。
- `GET /client/api/self-actions/history`：仅自己的执行状态与北京时间可格式化时间戳；不显示 stdout 或命令。
- 旧 `/client/api/packages` 保留兼容但返回空数组；旧 `/client/api/action` 不再接受客户直接指定 `software_update`，防止绕过目录。

## 安全和限制

- 预设命令由 Guest Agent SYSTEM 执行，交互模式则在 Windows 已登录用户桌面执行。**不要发布可删除客户数据、修改系统权限或包含密钥的高风险脚本**。不支持客户自由编辑参数。
- 远程命令返回 `SUCCEEDED` 代表执行器返回成功；对于 `detached` 模式，它只代表进程启动请求被接受，**不代表软件一直存活**。
- 软件更新 `VERIFY_REQUIRED` 表示版本未确认，管理员必须人工核实，不能解释为成功，也不会自动重试。
- 客户有效期与到期关机沿用 v1.4.28：时间在页面显示为北京时间，SQLite/接口仍以 UTC 存储。宿主机不自启，Host 不运行时无法实时到期关机。
- 宿主机建议只运行一个 8765 Worker，安装前备份 `D:\VMware\VM-Orchestrator\data`。不要将 `data`、Agent Key、.env、备份数据库写进发布包。

## 升级

1. 备份宿主机原目录的 `data` 文件夹及客户 VM 配置，给 001 测试机创建快照。
2. 停止 8765 与 8766；将 v1.4.29 解压到**独立文件夹**，运行 `Install-All.cmd`，再启动平台。
3. 首次部署新目录会以**新增表**方式迁移 SQLite，不覆盖旧业务表，也不会删除既有业务实例。
4. 在管理员端先建立一个**无害的测试命令**（例如 PowerShell `Get-Date`），对 001 所属套餐启用。通过思博客户页面执行，验证队列、状态和禁止重复点击。
5. 然后试用 SnowLuma 更新操作，核实 VM 目标版本及回滚。确认后再为真实客户套餐启用。

## 测试边界

已通过离线 API 集成与源代码回归，不代表 Windows 实际进程、VMware Tools 或 Guest WSS 已在宿主机验收。使用真实客户前必须在 001 VM 验证，尤其是命令运行身份、软件更新适配器、超时后人工解锁。
