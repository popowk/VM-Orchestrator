# VM Orchestrator — 功能总览（v1.4.44）

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
