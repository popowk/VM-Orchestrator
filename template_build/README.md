# VMO 黄金模板制作（程序内独立工作区）

## 唯一宿主机制作目录

D:\VMware\VM-Orchestrator\template_build

此目录同时保存工具文件、BUILD 制作虚拟机和导出的 Guest 工具包。
旧的 D:\VMware\Template-Build 不再被程序创建或依赖；由管理员自行删除。
正式模板库 D:\VMware\Template、客户机 D:\VMware\Customers、数据库和旧模板保持原样。
安装在其他位置时，制作目录自动跟随实际 VMO 程序根目录下的 template_build。
为避免误将 BUILD 放进旧目录，不再使用 VMO_TEMPLATE_BUILD_ROOT 环境变量更改位置。

## 创建 BUILD Windows（两种方式）

管理员后台「黄金模板 → 独立黄金模板制作区」：
- 使用 Windows ISO 创建 EFI 虚拟机和新的独立稀疏 VMDK，默认无网络；
- 从现有 SEALED_ATTESTED 正式黄金模板执行 VMware full clone，源模板不修改。

每个 BUILD 使用 template_build\<BUILD名称>\<BUILD名称>.vmx，
不得覆盖已经存在的目录。通过 VMware Workstation 安装 Windows、VMware Tools、
系统级 Python 3.11+ 和必要的通用软件，按网络隔离策略配置网络。
已执行过 Sysprep 的旧模板再次启动后可能不能安全重复 generalize，
因此优先从 Windows ISO 创建新的 BUILD。

## 在 BUILD Windows 内：安装 OOBE 引导与 Guest Agent V2

先在宿主机运行：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\VMware\VM-Orchestrator\template_build\Export-GuestToolkit.ps1"

它生成 template_build\_GuestToolkit-v[VERSION]。同版本已存在即拒绝覆盖。
工具包仅包含当前 OOBE、Guest Agent V2 和 Sysprep 所需代码，不含客户身份。

将整个工具包复制进 BUILD Windows，以管理员 PowerShell 在工具包根目录执行：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\guest\Prepare-TemplateGuest.ps1" -ConfirmBuildClone

这一步先配置 specialize/oobeSystem、SetupComplete 和 FirstLogon 引导，
再通过 guest_agent\Install-GuestAgentV2.ps1 -TemplateMode 安装自动运行的
VMO-GuestAgentV2 SYSTEM 服务。此时不能包含 agent.json、machine-binding.json
或任何客户 Token，服务运行但等待以后每台客户机唯一的 VMO_INIT ISO。
这与「在 OOBE 阶段首次注册」不同：**Agent 通用程序在封装前安装，独立身份在克隆后首次 OOBE 注册。**

## 在 BUILD Windows 内：人工检查后执行 Sysprep

人工检查 Windows、浏览器、软件账户和相关凭据，不执行旧版 DSH 数据检测脚本。
随后在管理员 PowerShell 执行：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\guest\Seal-Win10Template.ps1" -IAmOnDisposableClone -IReviewedAllAppAndWindowsCredentials -IUnderstandTheVmWillShutDown -UnattendedBootstrap

脚本核验 OOBE 引导文件、Agent V2 自动 SYSTEM 服务且无已注册身份，
调用 sysprep /generalize /oobe /shutdown /mode:vm。
Sysprep 失败则检查 Sysprep\Panther 下日志；成功关机后不再启动该封存源。

## 发布与首次客户部署

在 VMware 中卸载 Windows 安装 ISO，确认 VMX 不再引用 ISO、没有挂起文件/锁。
在后台点击「发布到正式模板候选库」，使用另一次 full clone 生成
D:\VMware\Template\<BUILD名称>\<BUILD名称>.vmx；不会迁移或覆盖原始 BUILD、
已登记正式模板、客户机或备份。状态初始为 CANDIDATE，需再人工封存确认。

从正式模板创建一台测试客户机验证 OOBE。VMO Host 会按客户机生成独立
随机初始密码、Agent 注册 Token、TLS CA 和 VMO_INIT ISO。
首次启动 Windows 的 specialize、oobeSystem 和首次登录流程应当完成，
预装的 Agent V2 会从 VMO_INIT ISO 自动读取该客户机注册配置。
它注册并上报心跳后，只有已启用「自动推送全部客户机」的当前便携软件版本才会派发。
全新 C:\dsh 软件首次安装默认不启动；升级已安装软件按原运行状态恢复。

## 安全约束

- 旧 D:\VMware\Template-Build 由管理员自行删除，程序绝不自动清理或迁移。
- 已存在的 BUILD、Guest 导出文件或正式同名模板都拒绝覆盖。
- Release ZIP 只打包 template_build 根目录下审核过的工具文件；
  任何 BUILD 磁盘、制作文件、工具包导出、运行时身份都不能打包。
- Windows/VMware/OOBE/Sysprep 的真实成功必须在宿主机和测试客户机实测，
  代码检查与模拟测试不能替代。
