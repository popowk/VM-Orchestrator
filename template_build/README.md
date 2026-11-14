# VM Orchestrator Windows 10 黄金模板制作（v1.4.64）

## 宿主机制作目录

唯一目录：D:\VMware\VM-Orchestrator\template_build（或 VMO 实际安装根目录下的 template_build）。
旧 D:\VMware\Template-Build 不再使用，**由管理员自行删除**。
正式黄金模板仍在 D:\VMware\Template，客户机仍在 D:\VMware\Customers。
**不要在宿主机运行客户机内的 01/02/03 封装命令。**

### 先在宿主机双击 00 导出工具包

双击 template_build\00-Export-GuestToolkit.cmd。

如旧版本在线升级后该文件缺失，可先重启 VMO Host 程序，新版会只恢复缺失入口；
也可以直接双击 scripts\host\00-Export-GuestToolkit.cmd（后者不依赖
template_build 旧版本是否存在）。已有同名版本导出目录绝不覆盖。

导出结果如下：

    template_build\
      00-Export-GuestToolkit.cmd
      Export-GuestToolkit.ps1
      _GuestToolkit-v1.4.64\
        Agent\
        Core\
        01-Prepare-And-Verify.cmd
        02-Verify-And-Seal.cmd
        03-Check-Status.cmd
        README-FIRST.txt

Agent 是通用 Guest Agent V2 代码，Core 是 OOBE、状态检查和 Sysprep 工具。
**整包复制** _GuestToolkit-v1.4.64 到尚未封装的 Windows 10 BUILD 虚拟机，
可以将文件夹改名为 C:\dsh\VMO-Win10-Template-Kit。
不要将之前已经有客户身份的 C:\dsh 目录或用户会话误复制到黄金模板。

### 然后在 Windows BUILD 虚拟机内直接双击

1. **01-Prepare-And-Verify.cmd**：弹出管理员 UAC，检查并准备通用 OOBE 引导、
   安装无客户注册身份的自动 SYSTEM 服务 VMO-GuestAgentV2，完成后检查状态。
   请事先安装 Windows、VMware Tools 和系统级 Python 3.11+。
2. **03-Check-Status.cmd**：只读检查 OOBE 文件、V2 SYSTEM 服务和身份是否为空。
   如果失败，不能执行封装。此检查不能代替人工确认浏览器、应用或 Windows 凭据。
3. **02-Verify-And-Seal.cmd**：再次执行检查；确认这是可抛弃的、未封装的 BUILD
   Windows 虚拟机并审查所有账户凭据后，输入大写 SEAL，才真正调用
   Sysprep /generalize /oobe /shutdown /mode:vm。正常成功后 BUILD 自动关机。
   **关机后不要再次开机**，否则可能破坏封存状态。

**“在 OOBE 安装 Guest Agent”更准确的流程：** 在 BUILD VM 里提前安装身份为空的
Guest Agent V2；正式客户机首次 OOBE 时由该服务读取独立 VMO_INIT ISO
注册身份。任何注册 Token、agent.json、machine-binding.json 都不能预存在母版。

### 发布与保护

Host 后台「黄金模板」可从 Windows ISO 创建 BUILD 或从已人工封存的模板 full clone。
制作文件以 template_build\<BUILD名称> 为单位；同名已存在时拒绝覆盖。
成功 Sysprep 关机后，在 VMware 中卸载 Windows 安装 ISO，确认无 .vmss/.lck
或 VMX ISO 引用；从 Host 后台发布为 D:\VMware\Template\<BUILD名称> 的候选版，
由管理员人工验收封存后再用于客户机。

正式客户新建和重装时，OOBE 独立注册成功、上报新 Guest Agent V2 心跳后，
仅自动推送那些已启用自动推送策略的当前便携软件版本；全新首装默认不启动。
保留现有正式模板、VMware BUILDs、数据库、客户 VM、旧备份与业务软件。
不要把 BUILD 磁盘、导出工具包和身份状态纳入 Release ZIP。
