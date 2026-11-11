> v1.4.15 最新提示：8765 已支持 `0.0.0.0` 绑定。它是管理员接口，必须严格限制来源 IP、走 HTTPS，不能直接公开给客户或互联网。以下旧版 localhost 说明仅供历史参考。

# V1.3 客户自助管理 / 思博业务集成

此版本以 V1.2.4 **完整源码**为基线，同一个 FastAPI Host Agent 内增加客户门户和管理员“思博客户服务”菜单，不新建第二套 VMware 控制服务。

## 安全边界（务必阅读）

- 管理后台 `/` 以及 `/api/vms`、`/api/templates` 等 **仍然只绑定 `127.0.0.1:8765`**，不要将 8765 直接暴露到公网。
- 客户页面 `/client`、`/client/api/*` 使用与管理员彻底分开的 HttpOnly Cookie，会话过期后必须重新从思博进入。
- 思博插件通过服务器端独立 `X-VMO-Business-Key` 发起开通、暂停、终止及一次性登录签发，**绝对不能使用** `VMO_ADMIN_KEY`。
- 业务 `service_ref` 映射到服务端保存的唯一 `vm_id`。客户的任何请求里都不接受客户提交的虚拟机 ID、VMX 路径、黄金模板 ID。操作前由服务端查询并检查归属。
- 一次性登录票据由思博服务器侧以 POST form 交给 `/client/exchange`，有效期 **90 秒，使用一次即销毁**；不会放在 URL 查询参数中。客户会话最长 3 小时。
- 服务暂停/终止会撤销客户票据和会话，不会自动删除正在使用的 Windows 磁盘；磁盘清理由管理员处理。终止状态不可直接重新激活。
- 强制关机、快照恢复/删除、重装系统、更新软件均需要手动输入准确虚拟机名称作为再次确认。快照增删恢复沿用后端的**完全关机且最多 3 个快照**规则。
- 只供客户使用的 HTTPS 代理必须严格限制路径为 `/client`, `/client/*`, `/ui/client.js`, `/ui/client.css`。业务 API 必须走**私有 HTTPS 通道或出站中转**；普通客户端不应能调用 `/api/business/*`。不要将 `/api/*`, `/ui/ui.js`, `/docs`, `/openapi.json`, `/` 暴露到客户域名。
- 对外客户入口需要正式 HTTPS 证书，并在 Host Agent 启动环境中设置 `VMO_CLIENT_SECURE_COOKIE=1`，确保会话 Cookie 标记 Secure。信任的代理必须正确传递/验证 HTTPS 请求，而不能直接信任来自互联网的 X-Forwarded-*。
- 仍需**二层客户间隔离、Windows 虚拟机 OOBE 实测、Guest Agent 自动注册、更新适配器、访问频率限制、审计日志备份及公网中转部署**，才能作为正式对外售卖的多租户系统使用。目前测试覆盖的是服务器授权和 VMware 模拟动作，不等于真实公网/VMware 的完整验收。

## Windows 宿主机升级

1. 在当前管理页面确保所有任务执行完毕，停止 Host Agent 和 Guest Gateway。
2. 将统一 ZIP 解压到 `D:\VMware\VM-Orchestrator-v1.3`（不能在已安装目录下解压）。
3. 双击 `Install-All.cmd`，升级现有 `D:\VMware\VM-Orchestrator`。安装器会备份旧代码和数据，但不会覆盖已有数据库、`.venv`、DPAPI 密钥、虚拟机及模板文件。
4. 双击 `Start-VM-Orchestrator.cmd` 启动单进程双监听服务，在本机打开 `http://127.0.0.1:8765/`。
5. 网页左侧出现 **「思博客户服务」**，配置“思博商品标识”到已封存模板的对应关系。例如 `win10-basic` 映射到 `Win10-Golden-OOBE`。也可以将一台 READY 测试 VM 绑定到服务实例，然后点击 **本机客户预览**。

首次启动时，`Start-Unified.ps1` 会在当前 Windows 登录用户 `%LOCALAPPDATA%\VM-Orchestrator\business-key.txt` 中生成**独立的 DPAPI 加密业务密钥**。再次启动读取同一密钥；不会每次改变。只有思博服务器端插件需要这一密钥。

若要给**可信的思博业务服务器**配置密钥，可在宿主机双击 `Copy-Business-Key.cmd`，复制后及时清空剪贴板。此密钥不可给客户、不可贴到前端 JavaScript 或提交到 Git。

## 思博新版注册式插件

本包随附：`integrations/sibhosting/public/plugins/host/vmo_vmware/`。

将整个 `vmo_vmware` 目录复制到思博服务器的 `public/plugins/host/`，按 [新版钩子文档](https://doc.0330.top/archives/43/) 绑定对应商品。

服务器配置字段：
- `vmo_api_url`：思博服务器可访问的 **私有 HTTPS 业务 API**（不是客户前端域名），需要受控中转或专用隧道。
- `vmo_business_key`：专用业务密钥，必须放在思博的服务器端安全配置中。
- `vmo_portal_url`：客户可访问的 HTTPS 门户源地址；反向代理只允许客户端入口的路径。

商品选配字段：
- `vmo_product_ref`：必须与 Host Agent 的「思博客户服务」商品映射一致，例如 `win10-basic`。

插件已实现 `create`、`suspend`、`unsuspend`、`terminate`、`status`、`clientArea`、`clientAreaOutput` 钩子。`create` 只是**异步受理克隆任务**，不能把「已排队」当作「Windows/OOBE/Guest 全部交付成功」。你使用的思博版本的钩子参数和商品状态更新流程，需要在真实思博测试环境验证。

**由于当前 VMware 宿主机是内网 `192.168.2.99` 且 Host Agent 仅本机监听，本包没有擅自开放管理员端口，也没有自动搭建公网中转。** 没有中转时，本机客户预览正常，外网思博插件暂时不能连通。

## 客户可使用的功能

- 首次购买：业务开通进入异步克隆 + 独立 VMware 身份 + 自动开机；客户页面显示克隆中/就绪和 Guest 注册状态。
- 只读：本机状态、最近任务、按需单帧屏幕截图、快照列表、已批准软件包。
- 操作：开机、软关机、强制关机、关机状态最多 3 个快照、关机状态恢复/删除快照、从套餐绑定模板重装、Guest Agent 已注册时下发批准版本的软件更新。
- 业务暂停后：客户会话和所有自助接口立即失效；现有 VMware 虚拟机不会自动被清盘。

## 对接调用与排障

需要宿主机启动 Business API；所有服务端请求均携带 `X-VMO-Business-Key`（仅服务端使用）。

`POST /api/business/services`
```json
{"service_ref":"sib-service-123","customer_ref":"sib-user-456","product_ref":"win10-basic"}
```
返回 `202`，重复相同业务受理请求只查询原业务（不额外克隆）；不同客户重用相同业务 ID 返回 `409`。

`GET /api/business/services/{service_ref}` 可查询开通任务状态。

`POST /api/business/services/{service_ref}/ticket`
```json
{"customer_ref":"sib-user-456"}
```
返回 `ticket`，有效 90 秒，只能通过服务端登录页 POST 到 `/client/exchange`，**不要拼到 URL 中**。

`POST /api/business/services/{service_ref}/state` 可将业务置为 `ACTIVE`、`SUSPENDED` 或 `TERMINATED`。`TERMINATED` 是不可恢复的逻辑终止，磁盘是否保留由管理员策略决定。

## 网络与原有未完成项

- Host Agent：`127.0.0.1:8765`，不要公网开放。
- 当前 Guest Gateway：`VMnet1 192.168.109.1:8766`，而客户 VMnet0 网卡在 `192.168.50.x`，旧网络未自动修复。要么建立到 VMnet1 的**受限**管理网卡/路由，要么重新设计独立管理网络。不能把 iKuai `192.168.50.1` 当成 Windows 宿主机的监听地址。
- Windows OOBE 此前在 specialize 阶段未正确读取 ISO；Agent 曾因 `URLError` 注册失败。V1.3 未声称自动修复这两个环境问题，生产部署之前必须用两台新 VM 通过真实验收。
