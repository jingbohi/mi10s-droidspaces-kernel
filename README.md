# thyme（小米 10S）Droidspaces 内核构建

在**原厂内核配置**上叠加最小的 Droidspaces 配置层，产出可直接替换 `boot.img` 中 kernel 段的 `Image`。
目标设备：Xiaomi Mi 10S（`thyme`，SM8250），MIUI `V14.0.6.0.TGACNXM`（Android 13，API 33）。

## 一、基线

| 项 | 值 |
| --- | --- |
| 源码 | `PixelExperience-Devices/kernel_xiaomi_thyme` |
| 分支 / commit | `thirteen` / `bc9f46dca6f2d608578180543cc2ff9ee5ee8a82` |
| defconfig | `thyme_defconfig`（4.19.157） |
| 编译器 | AOSP Clang r416183b（12.0.5）+ LLD 12.0.5 |
| 目标版本串 | `4.19.157-perf-g9d90dd04aa7c`（与原厂 `uname -r` 完全一致） |

> `moon_sm8250` 那棵树（`thyme_stock-defconfig`、4.19.325）**不是**这台机器的基线，构建会失败或功能缺失。

## 二、做了什么

| 类别 | 内容 |
| --- | --- |
| 配置基线 | 直接使用 `thyme_defconfig`（原厂配置转储），不换树、不改硬件驱动 |
| 容器层 | `USER_NS=y`、`CFS_BANDWIDTH=y`、`CGROUP_NET_PRIO=y`、`POSIX_MQUEUE`、各 namespace、`CGROUP_DEVICE/PIDS`、`DEVTMPFS`、`OVERLAY_FS`、`VETH/BRIDGE/MACVLAN`、`NF_TABLES`、`IP_NF_TARGET_MASQUERADE` |
| 保持关闭 | `SYSVIPC=n`、`IP6_NF_NAT=n`、`NF_NAT_IPV6=n`、`IP6_NF_TARGET_MASQUERADE=n`（见第四节） |
| 版本串 | `LOCALVERSION="-perf-g9d90dd04aa7c"` + 空 `.scmversion`，`uname -r` 与原厂一致 |
| 安全特性 | 关闭 `RELR`、`RANDOMIZE_BASE`（KASLR），与原厂构建一致 |
| CRC | 打 `03-allow-crc-mismatch.patch`，原厂 vendor `.ko` 的符号 CRC 不符时只告警、不拒绝加载 |
| 产物 | `Image`、`AnyKernel3` 包、`config-final.txt`、`config-base.txt`、`config-diff.txt`、`Module.symvers`、`System.map`、`build.log` |

补丁说明：

| 补丁 | 作用 | 适用条件 |
| --- | --- | --- |
| `01-fix-xt-qtaguid-panic` | 修复 xt_qtaguid 引用计数导致的 panic | 仅当内核树存在 `xt_qtaguid.c` 时应用，否则跳过 |
| `02-fix-cgroup-file-prefix` | 恢复 cgroup 文件前缀 | 总是应用 |
| `03-allow-crc-mismatch` | 符号 CRC 不匹配降级为警告 | `allow_crc_mismatch=1` 时应用 |
| `04-fix-netprio-cgroup-id` | 适配 5.x 风格 `cgroup_id()` | 仅当存在 `cgroup_id(struct cgroup *)` 时应用，4.19 跳过 |

## 三、跑构建

Actions → **Build thyme Droidspaces kernel** → Run workflow，**所有输入都要显式给值**（漏传会静默落回错误树的默认值）：

```
kernel_repo          = https://github.com/PixelExperience-Devices/kernel_xiaomi_thyme
kernel_branch        = bc9f46dca6f2d608578180543cc2ff9ee5ee8a82
defconfig            = thyme_defconfig
allow_crc_mismatch   = 1
droidspaces_layer    = 1
disable_relr         = 1
disable_kaslr        = 1
allow_config_drift   = 1
disable_arm64_hardening = 0
qemu_probe           = 0     # 必须为 0，探测镜像不可刷机
```

- `allow_config_drift=1`：仓库里的 `config/stock-4.19.157-thyme.config` 是另一棵树的原厂配置，
  与 PE thyme 基线不匹配，该保真门禁对本基线无效；改回 `0` 会在配置阶段失败。
- FCM 门禁不受 `allow_config_drift` 影响，始终生效（配置阶段 + 编译后各检查一次）。

本地等价命令：

```powershell
gh workflow run build.yml `
  -f kernel_repo=https://github.com/PixelExperience-Devices/kernel_xiaomi_thyme `
  -f kernel_branch=bc9f46dca6f2d608578180543cc2ff9ee5ee8a82 `
  -f defconfig=thyme_defconfig `
  -f allow_crc_mismatch=1 -f droidspaces_layer=1 `
  -f disable_relr=1 -f disable_kaslr=1 `
  -f allow_config_drift=1 -f disable_arm64_hardening=0 -f qemu_probe=0
```

## 四、为什么 SYSVIPC / IPv6 NAT 必须为 n

Android 13 的 `Build.isBuildConsistent()` 在 Treble 设备上等价于
`VintfObject.verifyWithoutAvb() == 0`，它用 `/system/etc/vintf/compatibility_matrix.5.xml`
里 `<kernel version="4.19.110" level="5">` 的 **251 项**逐项核对内核配置。
`SYSVIPC` 与 `IP6_NF_NAT` 在原厂内核里都是 `n`，早期版本为了容器打开后，开机就会弹
“There's an internal problem with your device”。

- 仓库内置 `config/fcm-4.19.110-level5.txt`（251 项要求），`scripts/check_fcm.py` 做比对，
  配置阶段和编译后都会跑；不满足直接编译失败。
- IPv6 NAT 只是 Droidspaces 的 `OPTIONAL` 项，容器 `--net=nat` 走 IPv4 MASQUERADE，不受影响。
- `SYSVIPC=n` 与原厂一致；容器里依赖 SysV 共享内存的程序（如 PostgreSQL 默认配置）会失败，
  Ubuntu 24.04 Minimal + systemd 无影响。

## 五、已验证结果

| 项 | 结果 |
| --- | --- |
| 构建 | Actions run `45025799106`，13m49s，success |
| `Image` | 49,317,904 B，MD5 `AC660034815E558F926689F4FBB349D2` |
| 导出符号 | 13,873 个；相对上一版仅移除 6 个 `nf_nat_*ipv6*`，0 新增；41 个原厂 `.ko` 均不引用 |
| FCM | 251 项全部满足（含运行时 `/proc/config.gz` 实算） |
| 开机弹窗 | 无 `VintfObject ... returns 1`、无 `Build fingerprint is not consistent` |
| 原厂功能 | WiFi / 相机 / 指纹 / 触控 / 充电 / 音频 全部 PASS |
| 容器 | systemd `running`、`--failed` 空、eth0 NAT、`curl https://www.baidu.com/` = 200 |

`Image` 打上 folkpatch（KernelPatch 0.13.9，2026-10-30 构建，空超级密钥）后重打包为
`boot-P8-folkpatch.img`（MD5 `F7E2743CAC8D57D9489BFB7938FD7FB8`），已刷入并验证；
不带 root 的 `boot-P8-stock.img` MD5 为 `67F5E7337DB2F153951035FB69B8C2BB`
（与上一版相同，因为内核代码未变，只有 root 补丁变了）。

> 交付镜像保存在构建仓库之外的本地项目中；仓库保存构建脚本、配置，以及 `docs/qemu-logs/` 里的 QEMU 探针原始日志。

## 六、发布正式版（release）

输入 `publish_release=1` 时，构建成功后会自动重打包并发布一个**不含 root** 的正式版：

1. `tools/pack_boot.py` 用 `bootimg/thyme-boot-template.bin` + `dist/Image` 重建
   `boot-mi10s-droidspaces.img`（128 MiB，与原分区同尺寸，只替换 kernel 段）；
2. `tools/verify_boot.py` 做 17 项结构校验，任一失败即中止发布
   （含 `no root patch payload`，所以打过 KernelPatch 的 Image 会被拒绝）；
3. `gh release create` 上传 `boot-mi10s-droidspaces.img`、`Image`、`AnyKernel3` zip、`System.map`、
   `config-final.txt`、`SHA256SUMS`。

| 约束 | 说明 |
| --- | --- |
| `qemu_probe=1` | 发布会**直接失败**，探针版改过 `setup.c`，不能刷机 |
| 标签 | 输入 `version`，格式 `vX.Y.Z`（默认 `v1.0.0`，每发一版必须递增） |
| 保留期 | release 永久保存；artifact 仍然只有 30 天 |
| 权限 | job 声明 `permissions: contents: write`，用 `GITHUB_TOKEN` 发布 |

CI 链路已在 run [`45169780558`](https://github.com/jingbohi/mi10s-droidspaces-kernel/actions/runs/45169780558)
（14 分 9 秒）上端到端跑通：重打包 17 项全 PASS，6 个附件的 SHA256 与 `SHA256SUMS` 逐项一致。
当时发布的 `release-38` 已撤回，现由 [`v1.0.0`](https://github.com/jingbohi/mi10s-droidspaces-kernel/releases/tag/v1.0.0) 取代。

`bootimg/` 里是什么：

| 文件 | 内容 |
| --- | --- |
| `thyme-boot-template.bin` | 4096 B v3 头 + 原厂 ramdisk + AVB vbmeta + footer（19.7 MB，**不含原厂 kernel**） |
| `thyme-boot-template.json` | 总大小、`ramdisk` MD5、vbmeta 偏移/哈希、cmdline、header 哈希等 |

模板可以用自己设备导出的原厂 boot.img 重新生成：

```bash
python3 tools/make_boot_template.py --boot stock_boot.img \
    --out-dir bootimg --name thyme-boot-template --firmware V14.0.6.0.TGACNXM
```

用本项目已刷入验证的那颗 `Image` 重打包，输出与本机验证过的无 root 镜像
**逐字节一致**（MD5 `67F5E7337DB2F153951035FB69B8C2BB`），说明 CI 的这条重打包路径与
真机验证路径等价，不是另一套实现。

## 七、构建流程与 QEMU 验证细节

[`docs/BUILD-AND-QEMU.md`](docs/BUILD-AND-QEMU.md) 记录了两条流水线的完整过程：

- **GitHub Actions 构建**：10 个输入的实际值与漏传后果、每个 step、`configure.sh` / `build.sh` 的
  12 道门禁、artifact 内容、7 次关键 run 的真实耗时、构建期踩过的坑与处理。
- **QEMU 虚拟机验证**：为什么这颗 SM8250 内核能在 `-M virt` 上启动、探针版与真机版的 3 处差异、
  WSL 安装、完整启动命令及每个参数的原因、4 项判定标记、GDB 抓现场的步骤。
- **根因案例**：AOSP Clang 默认 TLS 栈保护守卫（`tpidr_el0 + 0x28`）导致的静默卡死，
  从 GDB 现场（`queued_spin_lock_slowpath` / `die_lock`）到 `handle_bad_stack` 的 `0x28` 故障地址，
  再到 `-mstack-protector-guard=global` 修复与两层反汇编复核。
- **边界**：QEMU 能证明什么、不能证明什么，以及 CI 产物 → 重打包 → 刷机 → 体检的衔接方式。

## 八、目录

```
.github/workflows/build.yml        Actions 流程（全部输入见第三节）
bootimg/thyme-boot-template.bin    boot.img 布局模板（header + ramdisk + vbmeta + footer）
bootimg/thyme-boot-template.json   模板清单（大小、MD5、偏移、cmdline）
tools/make_boot_template.py        从原厂 boot.img 生成模板
tools/pack_boot.py                 模板 + Image → 128 MiB boot.img
tools/verify_boot.py               重打包结果的结构校验（发布前门禁）
tools/qemu_probe.sh                本地 QEMU 探针（诊断镜像，不可刷机）
scripts/configure.sh               基线 config + Droidspaces 层 + 断言 + FCM 门禁
scripts/build.sh                   编译 + 编译后 FCM 复核 + 打 AnyKernel3
scripts/check_config.py            原厂配置保真核对
scripts/check_fcm.py               FCM 251 项比对（可用 --simulate 做离线推演）
scripts/gen_fcm_bundle.py          从 compatibility_matrix XML 生成比对清单
scripts/patch_crc.py               CRC 降级补丁生成器
config/fcm-4.19.110-level5.txt     设备 FCM 要求清单
config/stock-4.19.157-thyme.config 原厂配置转储（仅核对用，不是基线）
patches/                           4 个源码补丁
anykernel/anykernel.sh             只写 Image，不写 dtb/dtbo
docs/BUILD-AND-QEMU.md             构建流程 + QEMU 验证全过程
docs/qemu-logs/                    QEMU 串口日志与 GDB 现场（证据）
```

## 九、重打 root 补丁

换内核会掉 root：KernelPatch 补丁直接嵌在 kernel 里。用管理器自带的 `kptools` + `kpimg`
对编译出来的 `Image` 重新打一遍即可，不需要改 ramdisk：

```bash
kptools -p -i Image -k <kpimg> -o Image.patched     # 空超级密钥时不传 -s
kptools -l -i Image.patched                         # 应显示 patched=true
```

再把 `Image.patched` 塞回原厂 `boot.img`（只替换 kernel 段）后刷入。
管理器更新后若提示“内核补丁有新版本可用”，说明管理器内置的 `kpimg` 构建与 boot 里的不同；
重新打补丁并刷入即可消除，root 本身不受影响。
