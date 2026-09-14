# iStoreOS for fnOS（飞牛 NAS 虚拟机应用）

在飞牛 fnOS 上以虚拟机形式运行 [iStoreOS](https://www.istoreos.com/)（基于 OpenWrt 的路由/NAS 系统）的 FPK 应用包。安装向导选版本后全自动：下载官方镜像 → 校验 → 网络预置 → 创建虚拟机 → 应用中心/桌面图标直达。

> 本仓库为 fnOS 打包适配工程，不修改 iStoreOS 本体；镜像从 iStoreOS 官方 CDN 原样下载并做 SHA256 校验。

## 当前版本

| | |
|---|---|
| 包版本 | **1.1.10** |
| FPK 下载 | [Releases](../../releases/latest) |
| FPK SHA256 | `5993af3490f5bcd15d7b59a7bfd955472df69899963a8b623359f6faed00123b` |

## 特性

- **秒回安装 + 后台进度**：安装回调秒级返回（规避 fnOS 约 190 秒回调看门狗），下载/预置/建机全部异步进行，状态机可查：`cat /tmp/istoreos-install.state`
- **三版本向导可选**：25.12.5 / 24.10.8 / 22.03.7（x86-64 EFI 官方 combined 镜像，安装时从官方 CDN 下载并强校验）
- **网络预置（A1 离线注入）**：安装期内存快照 + 一次性 qemu 起机，把 LAN 口改为 DHCP 客户端、关闭 iStoreOS 自带 DHCP/RA —— 接入任意主路由网段自动拿 IP，无需要求 192.168.100.x 网段
- **IP 寻踪入口（36125 端口）**：mac/arp → VNC 横幅 → mDNS → domifaddr → ping sweep 多级寻踪，应用中心「打开」按钮与桌面图标自动 302 到 VM 当前 IP（DHCP 地址漂移无感）
- **官方风格图标**：应用中心/桌面图标采用飞牛官方 squircle（连续曲率超椭圆）圆角曲线
- **断点续跑**：安装失败/中断后在应用中心点「启动」会按原参数自动重试；已有预置磁盘时秒级复用
- **入口页分状态 + 串口自救**：固定入口按「未开机 / 正在启动 / 还在获取地址 / Web 服务未就绪 / 已就绪」如实显示，开关机态每次都跟虚拟机实际状态核对（关机后按钮立刻出现）；拿不到 IPv4 时提供「网络修复」页，全部经 libvirt 串口在虚拟机里执行（重新获取 IP、设静态地址、重启网络、手动指定跳转地址），**不需要虚拟机有网络**
- **停用/启用与虚拟机真联动**：停用即 ACPI 优雅关机（实测 4~14 秒，超时转强制断电），入口页一键开机也会把应用中心状态同步回来
- **重装/换版本保数据**：磁盘常驻 `/vol1/vm/pool/istoreos.qcow2`，重装原样复用；向导里改选别的版本会先把旧盘整块留档到 `/vol1/vm/backup` 再装所选版本
- **网卡身份稳定**：重装沿用上一次的 UUID 与 MAC（另记 `/vol1/vm/istoreos.vm-mac`），不再因为换 MAC 让路由器绑定和租约作废

## 系统要求

| 项目 | 要求 |
|---|---|
| 系统 | fnOS x86_64（需 KVM，`/dev/kvm`） |
| 虚拟化 | 飞牛「虚拟机」应用已安装（提供 libvirt/OVMF 固件/virsh） |
| 内存 | 默认 1024MB，向导可设 512MB ~ 物理内存-1.5GB |
| 磁盘 | 安装卷剩余 ≥ 2GB（qcow2 虚拟磁盘 2GB 薄分配） |
| 网络 | 主路由 DHCP 可用（VM 桥接在 NAS 网口上取 IP） |

## 安装

1. [Releases](../../releases/latest) 下载 `com.istoreos.vm-1.1.10-fnos-amd64.fpk`
2. 飞牛 应用中心 → 手动安装 → 选择 FPK
3. 向导中选择版本/CPU/内存/是否随 NAS 自启
4. 等待后台完成（首次下载约 240MB。实测全新安装：25.12.5 / 24.10.8 约 4 分钟，22.03.7 约 11 分钟；磁盘已存在时重装约 10~40 秒，不重新下载）
5. 完成后应用中心「启用/停用」与虚拟机开机关机**联动**（停用 = ACPI 优雅关机，实测约 10 秒，60 秒超时转强制断电）；也可随时在「虚拟机」应用内独立操作

进度排查：

```bash
cat /tmp/istoreos-install.state        # queued/downloading/verifying/extracting/provisioning/defining-vm/ready/failed:xxx
tail -f /tmp/istoreos-install.log      # 状态轨迹
journalctl -u isto-install.service     # 后台 worker 详细日志
```

## 支持的 iStoreOS 版本

| 向导选项 | 官方镜像文件 | SHA256 |
|---|---|---|
| 25.12.5（默认） | istoreos-25.12.5-2026091113-x86-64-squashfs-combined-efi.img.gz | `a89206e3238cc0421561e4f3d6ffb17296d700bd13d7e77fd93ed1f86afdfc30` |
| 24.10.8 | istoreos-24.10.8-2026073111-x86-64-squashfs-combined-efi.img.gz | `8825143bc8e4ae45f6f828c7e40e1c9a605ba455ce4c7abf691a4e2b3d2e005e` |
| 22.03.7 | istoreos-22.03.7-2025050912-x86-64-squashfs-combined-efi.img.gz | `bda277850a2ccb37a67e69974d6b43733057d96dfb5a70914228375879b7d2ba` |

下载源：`https://fw.koolcenter.com/iStoreOS/x86_64_efi/`（iStoreOS 官方 CDN）

> 注：24.10.8 / 22.03.7 官方 gz 文件尾部带杂散字节，`gunzip` 会报 `trailing garbage ignored`（rc≠0）——镜像内容等价且 SHA256 校验通过，安装器已容忍，属正常现象。

## 卸载说明

- 应用中心卸载只删除虚拟机定义、UEFI 变量与寻踪服务；**qcow2 磁盘不会被删**（它在 libvirt 存储池 `/vol1/vm/pool/istoreos.qcow2`，不在应用数据目录里），重装即原样复用你的配置
- 换版本同样不丢数据：选别的版本时旧盘会整块留档到 `/vol1/vm/backup/istoreos.qcow2.swap-<版本>-<时间戳>`
- 想彻底清掉系统数据：先停用应用，再手动删除 `/vol1/vm/pool/istoreos.qcow2`（删前建议先备份一份）

## 仓库结构

```
manifest            FPK 元数据
cmd/                平台生命周期回调（install/main/uninstall/config/upgrade）
app/bin/            后台安装 worker、离线预置脚本、IP 寻踪入口服务、DB 守护
app/ui/             桌面图标与入口配置
config/             权限与资源声明
wizard/             安装/配置向导定义
```

## 从源码构建

依赖飞牛 `fnpack` 打包工具：

```bash
fnpack build -d .
# 产出 com.istoreos.vm.fpk（文件名取 manifest 的 appname，重命名加版本号即可）
```

## 致谢与上游

- iStoreOS：https://www.istoreos.com/ （下载站 https://fw.koolcenter.com/ ）
- iStoreOS 本体版权归 koolcenter 团队所有；本项目仅为 fnOS 侧的 FPK 封装与运维脚本
