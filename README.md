# VM Orchestrator v1.4.38（公开仓库审查修复）

本项目用于 Windows + VMware Workstation Pro 宿主机，集成思博客户业务、独立 Guest Agent、客户门户与远程运维。

## 目录结构

```text
VM-Orchestrator-v1.4.38/
├─ Install-All.cmd           # 一键安装 / 原地升级（请先停止 8765/8766）
├─ Install-All.ps1           # 升级备份与部署逻辑
├─ Start-VM-Orchestrator.cmd # 唯一服务启动入口：1 个 Python PID、2 个隔离监听端口
├─ Manage.cmd                # 账户、密钥、网络、防火墙、开机启动等统一管理菜单
├─ app/                      # 宿主机后端 API + 管理/客户 UI
│  └─ ui/admin.js            # 管理后台单一 JS 文件（已合并五个旧脚本）
├─ guest_agent/              # 客户 Windows 内运行的 Agent 原始分发源
├─ integrations/sibhosting/  # 思博服务端 PHP 插件
├─ integrations/app-report/  # 软件上报的接入示例
├─ scripts/host/             # 管理菜单及启动器调用的内部实现
├─ scripts/guest/            # Sysprep/OOBE/Guest 的原始实现，供单独模板工具使用
└─ docs/                     # 集成说明与版本历史
```

**Windows 10 模板准备脚本独立分发**：下载 `VMO-Win10-Template-Kit-v1.4.36.zip（Agent 未变）`，仅在未封存的 BUILD 克隆机上使用，不要在客户机上运行 Sysprep。

## 安装、运行、维护

1. 停止旧版 8765 管理端与 8766 Guest 网关；备份 `D:\VMware\VM-Orchestrator` 和客户 VM。
2. 从 ZIP 解压到不同于正式安装路径的文件夹，运行 `Install-All.cmd`。安装器备份历史文件，**不会删除已有数据库/DPAPI 密钥/客户虚拟机**。
3. 用 VMware Workstation 所属的 Windows 登录用户运行 `Start-VM-Orchestrator.cmd`，管理端默认 `http://127.0.0.1:8765`。
4. 首次初始化管理员账户执行 `Manage.cmd account`；其他维护项目直接双击 `Manage.cmd` 查看菜单。
5. 管理页面无法切换时，用 Ctrl+Shift+R 强制刷新，检查 `/ui/admin.js?v=1.4.38` 返回 HTTP 200（需要登录）。

## 已修复的页面无法点击问题

旧版页面引用 `/ui/biz-workspace.js`，但后端**没有为它注册路由**，因此点击左侧分组及「调整额度」不能可靠切换。新版把 `ui.js`、`business-admin.js`、`biz-workspace.js`、`self-service-admin.js`、`remote-admin.js` 合并成一份 `admin.js`，由单一已鉴权路由供应；也为该 JS/CSS 加上 `no-store`，避免升级后浏览器加载旧文件。客户额度按钮会进入「公告与额度」并自动填写实例号。

## 安全边界

- 8765 管理 API 和 8766 Guest HTTPS/WSS 仍隔离；不能向公网/客户网段直接公开 8765。
- 单进程只合并了两套 Python Web 服务；VMware vmrun 和客户 VM 内 Guest Agent 必须独立运行。
- 客户额度、公告、商品模板映射及订阅数据沿用旧数据结构，不能清空或重置已有数据库。
- 测试脚本不进生产 ZIP；必要的安全认证、运行诊断及维护脚本仍保留。

## 验证范围

源码静态检查与本地 HTTP/浏览器回归通过；尚需在真实 Windows 10 + VMware Workstation Pro、爱快 ACL、思博插件环境进行完整升级验收。建议先备份并在非生产环境验证。

## 远程运维 v1.4.36

远程命令输入限制：CMD 最多 8000 字符、PowerShell 最多 12000 字符。客户 Guest Agent **v2.0.21** 才支持超过 2000 字符的命令；旧 Agent 先通过远程运维按钮升级，等待新版本 WSS 心跳。真正极长脚本建议上传 `.ps1` 后调用，不能绕过 Windows 自身进程命令行长度限制。

「WSS 连接诊断」上方新增画面预览：默认关闭，手动开启后约每 1 秒获取 1 张缩略截图，关闭/切换 VM/离开页面/标签页隐藏后自动停止。截图使用原有仅绑定宿主机 127.0.0.1 的 VNC 通道，必须先在关机状态启用本机截图。请勿将 VMware VNC 端口暴露给客户或公网。

详细说明：`docs/v1.4.36-long-commands-preview.md`。


## v1.4.38 公开 GitHub Releases 与一键更新

1. GitHub 目标仓库是公开的 `popowk/VM-Orchestrator`，仅上传可公开审阅的源码，不要提交 `data/`、`*.sqlite3`、密钥、令牌、客户数据库、虚拟机或个人凭证。
2. Windows 安装 Git for Windows 和 GitHub CLI，执行 `gh auth login`，运行 `powershell -ExecutionPolicy Bypass -File .\tools\Publish-To-GitHub.ps1 -PublicApproved`；脚本审核待提交路径、提交源码并推送 `v1.4.38` 标签。
3. GitHub Actions 在发布标签后生成正式 Release 的 ZIP 和 SHA-256 清单。确保仓库的 Actions workflow 具有 `contents:write` 权限。
4. 管理后台进入「系统更新」即可检查公开 Release，**无需配置任何 GitHub Token**。已有私有仓库 Token 可以通过 `Manage.cmd github` 的清除令牌选项移除。
5. 必须先在 Windows 宿主机手动部署支持更新的版本，之后才能从管理后台升级到更高版本。Host 端更新不等于 Guest Agent 更新。

### 发布前审查与限制

- v1.4.38 修复公开仓库发布脚本拒绝 PUBLIC 仓库的问题、独立更新进程的状态竞争，并阻止 HTTP 重定向将私有 Token 传递到另一个下载域名。
- SHA-256 与 ZIP 位于同一个 GitHub Release，只能证明传输完整性，不能替代独立签名或可信发布者验证。
- 管理后台 8765 **不得直接暴露到公网**；使用可靠反向代理、可信源 IP 白名单和 HTTPS。Guest Gateway 8766 仍需与管理 API 隔离。
- 源码中的网络默认值仅用于兼容旧版安装，不代表任何公网开放建议。真实域名、IP、证书路径应通过部署时的配置覆盖。
- 尚未在真实 Windows + VMware 环境完成一键更新及失败回滚的完整验收，首次运行应先备份并选非生产机器测试。

详细操作见 `docs/v1.4.38-public-release.md`。
