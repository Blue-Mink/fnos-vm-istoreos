# iStoreOS for fnOS

在飞牛 fnOS 上以虚拟机方式运行 [iStoreOS](https://www.istoreos.com/)（基于 OpenWrt）的 FPK 打包工程。
向导里选好版本、CPU、内存就全自动完成：下载官方镜像 → SHA256 校验 → 网络预置 → 建虚拟机 → 桌面图标直达。
镜像由 iStoreOS 官方 CDN 原样获取，本仓库不修改 iStoreOS 本体。

**当前版本 1.1.10** · 下载 [Releases](../../releases/latest) · SHA256 `5993af3490f5bcd15d7b59a7bfd955472df69899963a8b623359f6faed00123b`

## 安装

1. 下载 FPK，应用中心 → 手动安装
2. 向导选 iStoreOS 版本 / CPU / 内存 / 是否随 NAS 自启（提交后立即返回，重活在后台）
3. 等后台完成：首次全新安装 25.12.5、24.10.8 约 4 分钟，22.03.7 约 11 分钟；磁盘已存在时约 10~40 秒
4. 用应用中心「打开」或桌面图标进固定入口 `http://<NAS 地址>:36125/`，自动跟随虚拟机 IP 变化

要求：fnOS x86_64 且已装飞牛「虚拟机」应用（需 `/dev/kvm`），安装卷剩余 ≥ 2GB，主路由 DHCP 可用。
卡住时看进度：`cat /tmp/istoreos-install.state`

## 特性

- **固定入口 36125**：多级寻踪（MAC/ARP → mDNS → domifaddr → 局域网扫描 → HTTP 指纹），DHCP 地址漂移无感；页面按「未开机 / 正在启动 / 还在获取地址 / Web 未就绪 / 已就绪」如实显示状态
- **串口网络修复**：虚拟机拿不到 IPv4 时，可在入口页经 libvirt 串口重新获取 IP、设静态地址、重启网络或手动指定跳转地址——不依赖虚拟机有网络
- **停用/启用与虚拟机联动**：停用即 ACPI 优雅关机，入口页开机也会把应用中心状态同步回来
- **数据不丢**：磁盘常驻 `/vol1/vm/pool/istoreos.qcow2`，重装原样复用；改选别的版本会先把旧盘整块留档到 `/vol1/vm/backup`；重装沿用网卡 MAC，路由器绑定不失效
- **安装秒回 + 断点续跑**：规避 fnOS 安装回调约 190 秒看门狗；中断后点「启动」按原参数续跑

## 支持的版本

| 向导选项 | 官方镜像（`https://fw.koolcenter.com/iStoreOS/x86_64_efi/`） | SHA256 前 12 位 |
|---|---|---|
| 25.12.5（默认） | istoreos-25.12.5-2026091113-x86-64-squashfs-combined-efi.img.gz | `a89206e3238c` |
| 24.10.8 | istoreos-24.10.8-2026073111-x86-64-squashfs-combined-efi.img.gz | `8825143bc8e4` |
| 22.03.7 | istoreos-22.03.7-2025050912-x86-64-squashfs-combined-efi.img.gz | `bda277850a2c` |

24.10.8 / 22.03.7 官方 gz 尾部带杂散字节，`gunzip` 会报 `trailing garbage ignored`——内容等价且校验通过，安装器已容忍。

## 卸载

应用中心卸载只删虚拟机定义与寻踪服务，**qcow2 磁盘和你的配置不会被删**，重装即复用。
要彻底清掉系统数据：先停用应用，再手动删 `/vol1/vm/pool/istoreos.qcow2`（建议先备份）。

## 构建

```bash
fnpack build -d .      # 产出 com.istoreos.vm.fpk，文件名加版本号后上传
```

目录：`manifest` 元数据 · `cmd/` 生命周期回调 · `app/bin/` 安装 worker、离线预置、寻踪入口服务 · `wizard/` 向导定义

iStoreOS 版权归 koolcenter 团队：https://www.istoreos.com/
