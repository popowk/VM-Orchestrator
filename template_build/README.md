# VMO 独立黄金模板制作工作区

本目录包含 **宿主机黄金模板制作工具**。正式发行包必须包含本目录，与原有 scripts/guest 及现有模板磁盘相互独立。

## 目录约定

- D:\VMware\Template-Build：制作、测试 Windows 黄金模板；不会存放客户实例。
- D:\VMware\Template：原正式黄金模板库，原 VMX/VMDK 原样保留。
- D:\VMware\Customers：客户虚拟机和重装备份，制作工具不能覆盖。
- D:\VMware\VM-Orchestrator\template_build：宿主机模板制作工具和本说明。

可以用 VMO_TEMPLATE_BUILD_ROOT 环境变量另设制作目录。不能与正式模板根目录或客户机根目录重叠。

## 方式一：从 Windows ISO 创建

进入管理后台“黄金模板 → 独立黄金模板制作区”，填新 BUILD 名称、Windows ISO 的宿主机绝对路径，以及内存、CPU、系统盘。Host 使用 VMware Workstation 的 vmware-vdiskmanager.exe 创建新的独立稀疏 VMDK，并写入 EFI Win10 VMX。

默认没有网络连接。需要安装 Windows、VMware Tools 或业务程序时，先按爱快/VMnet 规则配置独立网络，再通过 VMware 手动启动 BUILD Windows。不要在模板里保存任何客户机身份、授权密钥、浏览器登录状态。

## 方式二：从现有黄金模板完整克隆

从正式模板库选择已经人工封存的来源模板，必须完全关机，没有 .lck 或 .vmss。通过 vmrun full clone 复制到一个新的 Template-Build 子目录。源模板保持原样。已执行 Sysprep 的旧模板再次启动后会进入 OOBE，可能不能再次安全 generalize，需要在测试机验证。

## 独立 Guest 工具包

在宿主机执行：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\VMware\VM-Orchestrator\template_build\Export-GuestToolkit.ps1"

输出目录位于 D:\VMware\Template-Build\_GuestToolkit-v[VERSION]，同版本已存在时拒绝覆盖。该工具会从正式程序包中的 scripts/guest 与 guest_agent 复制必要脚本和 Agent V2 安装代码，不会导入 DSH 账户、客户身份、浏览器会话或数据库。

将独立工具包复制进 BUILD Windows，使用管理员 PowerShell 执行 guest\Install-OOBE-Bootstrap.ps1 -ConfirmBuildClone。完成 Windows 配置和凭据检查后，按脚本要求附带明确人工确认开关及 -UnattendedBootstrap 运行 guest\Seal-Win10Template.ps1，等待 Sysprep 正常关机。

## 发布正式候选

关机后不要再启动封存的 BUILD 系统。先在 VMware 中移除安装 ISO 引用；检查 VMX、VMSS 和 LCK。随后在管理后台模板制作列表点击“发布到正式模板候选库”。

发布使用另一次 full clone，创建新的正式模板目录 D:\VMware\Template\[BUILD名称]\[BUILD名称].vmx，且绝不覆盖现有同名目录；BUILD 原文件不移动不删除。

正式模板新副本只登记为 CANDIDATE。管理员必须独立审核和人工封存后，才可以用于正式客户克隆与重装。首次要用测试客户机验证 OOBE、独立 Guest 注册和软件下载。

如果建立、封存或发布过程中发生错误，保留部分文件以供人工调查；不能悄悄清空重试。

## 新系统软件自动推送

新客户机或重装后的 Windows 均会生成新的 Guest Agent 注册身份。自动推送按虚拟机 ID、当前 Agent 注册 ID 和当前软件包 ID 去重，而不是永久只看 VM ID。

新系统注册成功并首次上报 2.0.27+ 心跳后，开启自动推送的软件即使在 C:\dsh 下尚未安装，也会进行首次下载校验、解压和安装。首次安装成功仍默认不启动。旧系统的软件任务继续保留审计，不会作为新系统的去重依据。
