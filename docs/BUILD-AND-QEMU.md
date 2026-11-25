# GitHub Actions 构建与 QEMU 虚拟机内核验证

本文记录 thyme（小米 10S，SM8250）这颗 4.19 内核**怎么在 GitHub 上编译**、**怎么在 QEMU 虚拟机里先跑一遍**，以及这两条流水线在 P5→P8 期间真实踩过的坑和留下的证据。

- 目标设备：Xiaomi Mi 10S（`thyme`），MIUI `V14.0.6.0.TGACNXM`，Android 13（API 33）
- 构建仓库：`jingbohi/mi10s-droidspaces-kernel`（构建时为 **Private**，P8 交付后改为 **Public**）
- QEMU 探针：`tools/qemu_probe.sh`
- 证据日志：`docs/qemu-logs/`

> 路径约定：本文所有路径都相对**本仓库根目录**。
> 下载产物、重打包、刷机脚本（`fetch_artifact.ps1`、`finalize_boot.ps1`、`patch_apatch.ps1`、
> `flash_test.ps1`、`verify_*.ps1`、`tools/sym.py`）以及交付镜像保存在构建仓库之外的本地项目中，
> 不在本仓库里；文中出现这类路径时都会单独标注。

---

## 1. 为什么要两条流水线

| 约束 | 实际情况 | 结果 |
|---|---|---|
| 本机内存 | 整机 3.9 GB，WSL2 可用约 2.9 GB | 本地 `-j4` 编译内核会 OOM；只能 `-j2` + 大 swap，4–8 小时且可能中途失败 |
| 刷机成本 | 内核卡死需 **音量减 + 电源约 15 秒** 强制重启，再进 fastboot 回退 | 一次失败的刷机 ≈ 10 分钟 + 一次变砖风险 |
| 故障可见性 | 卡死发生在控制台注册之前时，**串口无输出、pstore 为空** | 真机上完全拿不到现场，只能盲试配置 |
| 迭代频率 | P5/P6 期间一天要改十几次配置 | 必须把「编译」和「上机」拆开，中间加一层可重复的验证 |

所以最终固定为：

1. **GitHub Actions 负责编译**：固定源码 commit + 固定 Clang + 固定配置门禁，产出 `Image` 和 `System.map`；
2. **QEMU 负责早期启动冒烟**：在本机 WSL 里启动同一个 `Image`，用 GDB 抓现场，专门排查「控制台注册前静默卡死」；
3. **真机负责最终验收**：QEMU 通过不代表能用，硬件、DTB、vendor 模块、FCM 都必须在手机上验。

---

## 2. 全景流程

```text
本地修改本仓库并 push
        │
        ▼
GitHub Actions（workflow_dispatch，ubuntu-22.04）
  checkout 构建脚本
  → apt 依赖
  → clone 内核源码（固定 commit，depth=1）
  → 安装 AOSP Clang r416183b
  → configure.sh（基线 config + Droidspaces 层 + 断言 + FCM 门禁）
  → build.sh（栈保护预检 + make Image + 产物校验 + AnyKernel3）
  → 上传 artifact（thyme-droidspaces-<run_number>）
        │
        ▼
本地 gh run download（或本地脚本 fetch_artifact.ps1）
        │
        ├─ finalize_boot.ps1 -Profile layer        → boot-P8-stock.img
        │
        └─ patch_apatch.ps1（设备端 kptools + kpimg）
              → finalize_boot.ps1 -Profile layer    → boot-P8-folkpatch.img
                        │
                        ▼
              flash_test.ps1 → verify_device.ps1 / verify_droidspaces.ps1
```

> 图里 `finalize_boot.ps1` 之后的步骤都在**本地项目**里，不在本仓库。

QEMU 探针是这条链上的**旁路**：它用同一份源码额外编译一个「带 PL011 串口」的诊断镜像，只在本机跑，绝不刷机。

---

## 3. GitHub Actions 构建

### 3.1 触发方式

workflow 只有 `workflow_dispatch`（手动触发），没有 push 自动构建，避免改文档也烧构建时间。

```yaml
runs-on: ubuntu-22.04
timeout-minutes: 180
concurrency:
  group: thyme-<所有输入的组合>
  cancel-in-progress: true
```

`concurrency` 的 group 把**构建相关输入**全部拼进去，所以：

- 同参数重复触发会取消旧的；
- 不同参数（例如 `qemu_probe=0` 和 `qemu_probe=1`）可以并行，不会互相覆盖产物。

### 3.2 十二个输入（必须全部显式传）

> 这是本项目最容易踩的坑：workflow 的默认值仍指向**已废弃的 pipa / moon_sm8250 树**。
> 用网页点 “Run workflow” 而不改默认值，会直接编译错误的源码，十几分钟后才发现。

| 输入 | P8 实际值 | 说明 |
|---|---|---|
| `kernel_repo` | `https://github.com/PixelExperience-Devices/kernel_xiaomi_thyme` | 真 thyme 树 |
| `kernel_branch` | `bc9f46dca6f2d608578180543cc2ff9ee5ee8a82` | 固定 commit，不用分支名 |
| `defconfig` | `thyme_defconfig` | 4.19.157，原厂配置转储 |
| `allow_crc_mismatch` | `1` | 原厂 vendor `.ko` 的符号 CRC 不符时只告警 |
| `droidspaces_layer` | `1` | 叠加容器所需配置层 |
| `disable_relr` | `1` | 与原厂一致（GNU ld 2.27 不支持 RELR） |
| `disable_kaslr` | `1` | 与本树作者自己的 thyme 配置一致 |
| `allow_config_drift` | `1` | 仓库自带的原厂 config 来自另一棵树，保真门禁对本基线无效 |
| `disable_arm64_hardening` | `0` | 保留原厂早期加固 |
| `qemu_probe` | `0` | **刷机镜像必须为 0**；只有本地 QEMU 诊断才设 1 |
| `publish_release` | `0` | `1` = 重打包 `boot-mi10s-droidspaces.img` 并发布 release（见 5.1） |
| `version` | `v1.0.0` | release 标签，格式 `vX.Y.Z`；每发一版必须递增 |

命令行等价写法：

```powershell
gh workflow run build.yml `
  -f kernel_repo=https://github.com/PixelExperience-Devices/kernel_xiaomi_thyme `
  -f kernel_branch=bc9f46dca6f2d608578180543cc2ff9ee5ee8a82 `
  -f defconfig=thyme_defconfig `
  -f allow_crc_mismatch=1 -f droidspaces_layer=1 `
  -f disable_relr=1 -f disable_kaslr=1 `
  -f allow_config_drift=1 -f disable_arm64_hardening=0 -f qemu_probe=0 `
  -f publish_release=0
```

漏任何一个 `-f`，GitHub 会用默认值补齐，且**不会报错**。

### 3.3 每个 step 做了什么

| Step | 内容 | 关键点 |
|---|---|---|
| Checkout build scripts | 拉本仓库 | 只包含 workflow / 脚本 / 配置 / 补丁 / 模板 |
| Install build deps | `bc bison cpio flex libelf-dev libssl-dev lz4 python3 rsync zip unzip wget dtc perl` 等 | Ubuntu 22.04 自带基础工具链 |
| Clone kernel source | `git init` + `remote add` + `git fetch --depth=1 origin <SHA>` + `checkout FETCH_HEAD` | 日志打印实际 HEAD 和 `Makefile` 的 VERSION/PATCHLEVEL/SUBLEVEL |
| Setup AOSP Clang | 下载 `clang-r416183b.tar.gz`（android.googlesource.com），解到 `$HOME/aosp-clang` | Clang 12.0.5 + LLD 12.0.5；这棵树 CI 验证过的版本 |
| Configure | `bash scripts/configure.sh` | 基线 config、Droidspaces 层、断言、FCM 门禁，见 3.4 |
| Build | `bash scripts/build.sh` | 预检、编译、产物校验、打包，见 3.4 |
| Repack boot.img | `tools/pack_boot.py` + `tools/verify_boot.py`，仅 `publish_release=1` | 17 项结构校验全过才继续；`qemu_probe=1` 直接失败，见 5.1 |
| Upload artifacts | `actions/upload-artifact@v4`，`if: always()` | 构建失败也能保留已生成的文件；configure 阶段失败则看 Actions 步骤日志；保留 **30 天** |
| Publish release | `gh release create`，仅 `publish_release=1` 且前面全部成功 | job 需要 `permissions: contents: write`；标签取输入 `version`（`vX.Y.Z`） |

补充：

- `PATCH_OPTIONAL` 由仓库 URL 决定：`moon_sm8250`（pipa 树）必须应用全部补丁；其它树允许跳过不兼容的可选补丁。
- 构建日志里会输出 `kernel HEAD: <sha>`，用于确认没有拉错源码。

### 3.4 构建脚本内部

#### `scripts/configure.sh`

| 阶段 | 动作 |
|---|---|
| 基线 | `cp <defconfig> $OUT/.config`，再 `olddefconfig`；`stock/*` 必须是完整转储（> 3500 个符号），PE 基线走 `*_defconfig` 分支（> 200 个符号即可） |
| RELR | 默认 `-d RELR`，与原厂一致 |
| KASLR | 默认 `-d RANDOMIZE_BASE`，避免 LLD 重定位差异导致 head.S 阶段静默卡死 |
| ARM64 早期加固 | `disable_arm64_hardening=1` 时关闭 `SW_TTBR0_PAN` / `HARDEN_BRANCH_PREDICTOR` / `HARDEN_EL2_VECTORS`（默认保留） |
| QEMU 探针 | `qemu_probe=1` 时打开 PL011 串口、跳过高通 SCM 的 `init_random_pool()`（见第 4 节） |
| Droidspaces 层 | 打开 namespace / cgroup / devtmpfs / overlayfs / veth / bridge / macvlan / netfilter 等非 GKI 必需项 |
| 原厂对齐 | `SYSVIPC=n`、`IP6_NF_NAT=n`、`NF_NAT_IPV6=n`、`IP6_NF_TARGET_MASQUERADE=n`（FCM 要求） |
| 配置断言 | `USER_NS` / `CFS_BANDWIDTH` / `CGROUP_NET_PRIO` 等必须存活；关键驱动不能被 `olddefconfig` 裁掉 |
| 保真门禁 | 与仓库自带原厂 config 逐项比对；`allow_config_drift=1` 时跳过 |
| FCM 门禁 | `check_fcm.py` 对 `config/fcm-4.19.110-level5.txt` 的 **251 项**逐项比对，不满足直接 fail |

FCM 门禁细节见根目录 `README.md` 第 3.2 节；这里只强调一点：**它不受 `allow_config_drift` 影响**，始终生效。

#### `scripts/build.sh`

| 阶段 | 检查 | 失败后果 |
|---|---|---|
| 0. 栈保护预检 | 用 `-fstack-protector-all` 编译 `sp_probe.c`，反汇编必须看到 `__stack_chk_guard`，且不能出现 `tpidr_el0` | 3 分钟内 fail，避免白编译 15 分钟 |
| 1. 编译 | `make Image`，`KCFLAGS=-mstack-protector-guard=global` | 日志落 `dist/build.log` |
| 2. 产物校验 | `Image` 大小必须在 20–100 MB | 防止配置裁剪/膨胀 |
| 2. FCM 复核 | 对 `$OUT/.config` 再跑一次 `check_fcm.py` | 防止 `syncconfig` 把项重新 `select` 成 `y` |
| 2. 版本串 | 必须以 `Linux version 4.19.157-perf-g9d90dd04aa7c ` 开头 | 否则原厂 vendor 模块的 vermagic 对不上 |
| 2. root 混入 | `Image` 里不能出现 `KernelPatch` / `KernelSU` / `kpatch` 字样 | 防止设备端补丁产物被误当源码产物 |
| 2. 栈保护端到端 | 反汇编真正链接的 `vmlinux` 的 `start_kernel`，不能出现 `tpidr_el0` | 比预检更严格，直接看最终产物 |
| 2. vendor 符号 | 必须导出 `__dynamic_pr_debug` / `__dynamic_dev_dbg` / `xlogchar_kwrite` | 缺一个，原厂 41 个 `.ko` 会整组加载失败 |
| 3. 打包 | AnyKernel3 zip，只放 `Image` | 不碰 dtb / dtbo / vendor_boot |

`Image` 校验通过后会复制到 `dist/Image`，同时保存 `config-*.txt`、`Module.symvers`、`System.map`、`start_kernel.dis`、`crc-mode.txt`。

### 3.5 产物

artifact 名称是 **`thyme-droidspaces-<run_number>`**（不是 run_id）。P8 那次 run_id `45025799106`，artifact 是 `thyme-droidspaces-37`。

| 文件 | 用途 |
|---|---|
| `Image` | 未打 root 补丁的内核 |
| `boot-mi10s-droidspaces.img` | **仅 `publish_release=1` 时生成**：重打包好的 128 MiB 可刷镜像 |
| `AnyKernel3-thyme-droidspaces.zip` | 手机端 AnyKernel3 刷入包（本项目最终不用，保留备用） |
| `config-final.txt` / `config-base.txt` / `config-diff.txt` | 最终配置 / 基线 / 差异 |
| `Module.symvers` | 导出符号 + CRC |
| `System.map` | 符号表；QEMU 卡死时用它把 PC 翻译成函数名 |
| `start_kernel.dis` | `start_kernel` 反汇编，用于确认栈保护守卫 |
| `crc-mode.txt` | `allow` 或 `strict` |
| `build.log` | 完整编译日志（含 FCM 门禁输出） |

`stock-check.txt` 在 workflow 的清单里，但当前基线不生成，`if-no-files-found: warn` 会跳过。

### 3.6 实测构建记录

以下为与 P7/P8 和 QEMU 排查直接相关的 run（时间是 run 总时长，含启动和上传）：

| run_id | commit | 结果 | 时长 | 说明 |
|---|---|---|---|---|
| `44906270218` | `9f8d4b0` | success | 12.7 min | 第一次 QEMU 探针构建；此时还没上传 `System.map` |
| `44970692468` | `b9358b1` | success | 12.3 min | 增加 `System.map` 上传，为 GDB 符号化做准备 |
| `44973181442` | `6fa85db` | **failure** | 3.3 min | 栈保护预检误报：`预检没有看到 __stack_chk_guard` |
| `44979730099` | `06af24b` | success | 14.6 min | 修正预检（`-fstack-protector-all`），产出 pq4 镜像 |
| `44986735674` | `e063564` | success | 12.3 min | 探针版跳过 SCM，产出 pq5 镜像 |
| `44992341831` | `38d60cd` | success | 12.2 min | 探针版复跑；`thyme-droidspaces-30`，本文复现用 |
| `45025799106` | `a58803b` | success | 16.6 min | **P7/P8 最终内核**，artifact `thyme-droidspaces-37` |

P8 的内核代码与 P7 完全相同（同一份 `Image`，MD5 `AC660034815E558F926689F4FBB349D2`），
只是用 115404 的 `kpimg` 重打了 root 补丁，所以 `boot-P8-stock.img` 与 P7 stock 的 MD5 相同。

### 3.7 构建期真实故障

前四行来自早期的 HyperSU 4.19.325 基线（已废弃），保留下来是为了说明“为什么最后换树”；
后两行是最终 PE 基线真实遇到的。换树后，`stock/*` 的 defconfig 命名、同源 fts 驱动冲突、
小米私有驱动的目录门控都不再出现。

| 现象 | 根因 | 处理 |
|---|---|---|
| `No rule to make target 'stock/thyme_stock_defconfig'` | 该文件是完整 `.config` 转储，kbuild 的 `%_defconfig` 匹配不到 | `cp` 到 `$OUT/.config` + `olddefconfig` |
| `include/trace/hooks/memory.h: expected identifier` | 树里 `bpf_jit_comp.c` 等无条件调用 `trace_android_vh_*` | `CONFIG_ANDROID_VENDOR_HOOKS=y` |
| `duplicate symbol: updateGestureMask`（20+ 个） | `TOUCHSCREEN_ST` 与 `TOUCHSCREEN_ST_FTS_V521` 是同源 fts 驱动 | 只留后者（FOD/手势依赖它） |
| `undefined symbol: idtp_op_ble_flag` | 小米私有驱动被 `CONFIG_MACH_XIAOMI` 目录门控 | 开 `MACH_XIAOMI_THYME=y`，并加 12 项断言 |
| 栈保护预检误报（run `44973181442`） | `-fstack-protector-strong` + `-O2` 把探针函数优化成一条 `ldrb`，canary 根本没生成 | 改用 `-fstack-protector-all`，并加 `start_kernel` 端到端反汇编检查 |
| 原厂 vendor 模块加载失败 | 新内核符号 CRC 与原厂 `.ko` 不一致 | `patches/03` 把 CRC 不匹配降级为警告；`crc-mode.txt` 记录 |

---

## 4. QEMU 虚拟机验证

### 4.1 为什么这颗 SM8250 内核能在 QEMU `virt` 上启动

- arm64 **没有** `machine_desc` 表（`arch/arm64/include/asm/mach/arch.h` 不存在），
  `setup_machine_fdt()` 只打印 `Machine model: ...`，然后走通用启动路径。
- 所以 QEMU 默认的 `linux,dummy-virt` 设备树可以直接用，**不需要伪造 `compatible`**，
  也不需要改 DTB（早期版本曾尝试重写 DTB，后来删掉了）。
- QEMU `virt` 提供 GICv2、arch timer、PSCI 0.2、2 GB RAM、2 个 vCPU，足够走到 `start_kernel` 中段。

限制：`virt` 没有 SM8250 的外设，也没有 rootfs。理想情况下内核最后会
`VFS: Unable to mount root fs` 或 panic；**能走到那里就说明早期启动路径已经通了**。
本项目的探针版目前还会在 QEMU 环境特有的一处早期异常处停下（见 4.5 / 4.8），
但已经越过真机卡死的位置，判定逻辑同样只要求「早期路径走通」。

### 4.2 探针版与真机版的差别

只有 `qemu_probe=1` 时才会发生以下改动，**真机镜像永远用 0**：

| 改动 | 位置 | 原因 |
|---|---|---|
| `ARM_AMBA`、`SERIAL_AMBA_PL011`、`SERIAL_AMBA_PL011_CONSOLE`、`SERIAL_EARLYCON`、`POWER_RESET`、`POWER_RESET_SYSCON` 打开 | `configure.sh` | QEMU `virt` 的串口是 PL011（MMIO `0x09000000`），原厂配置是关的；不开就一个字节都看不到 |
| 注释掉 `init_random_pool();` | `arch/arm64/kernel/setup.c` | QEMU 没有高通 SCM，`scm_call2` 会触发 undefined instruction；这个调用只影响随机池 seed，真机路径保持原样 |

判断某次 artifact 是不是探针版：

```bash
grep -E 'CONFIG_SERIAL_AMBA_PL011=y|CONFIG_SERIAL_EARLYCON=y' config-final.txt
```

或看 Actions 的 job 名，末尾会带 `qemu=1`。

### 4.3 本机运行环境

| 项 | 值 |
|---|---|
| 宿主 | Windows 11 + WSL2，发行版 `cepheus-build`（Ubuntu 20.04） |
| QEMU | `qemu-system-aarch64` **4.2.1**（Debian `1:4.2-3ubuntu6.30`） |
| 安装包 | `qemu-system-arm`（含 aarch64），51 个依赖，阿里云镜像 |
| 调试器 | `gdb-multiarch` **9.2** |
| 安装命令 | `sudo apt-get install -y qemu-system-arm gdb-multiarch` |

QEMU 是 WSL 里原生安装的，不需要嵌套虚拟化（TCG 软件模拟即可；跑内核冒烟足够，速度约几秒到十几秒）。

### 4.4 启动命令

`tools/qemu_probe.sh` 的核心就是这一条：

```bash
timeout 45 qemu-system-aarch64 \
    -M virt -cpu cortex-a72 -m 2048 -smp 2 \
    -nic none \
    -kernel Image \
    -append "console=ttyAMA0 earlycon=pl011,0x09000000 keep_bootcon panic=1 ignore_loglevel loglevel=8" \
    -display none -monitor none -serial "file:/tmp/qemu-serial.log" -no-reboot
```

逐项解释：

| 参数 | 作用 / 踩过的坑 |
|---|---|
| `-M virt -cpu cortex-a72` | 通用 arm64 机器；不需要匹配 kona |
| `-m 2048 -smp 2` | 2 GB / 2 核，足够走到 SLUB 和 RCU；`-smp 1` 会掩盖 SMP 相关问题 |
| `-nic none` | **必须**。QEMU `virt` 默认挂 `virtio-net-pci`，而 WSL 的 `qemu-system-arm` 包缺 `efi-virtio.rom`，QEMU 会在加载内核前直接退出，日志 0 字节 |
| `-append ...` | `earlycon=pl011,0x09000000` 是 QEMU virt 的串口地址；`keep_bootcon` 防止 earlycon 被注销；`panic=1` + `-no-reboot` 防止 panic 后无限重启刷屏 |
| `-serial file:...` | 串口写文件，便于 grep；也可以 `-serial mon:stdio` 直接看 |
| `-display none -monitor none` | 无头运行，避免 WSL 里没有显示服务 |
| `timeout 45` | 防止卡死；退出码 `124` 表示超时 |

脚本的判定门槛（4 个标记）：

```text
Booting Linux
Linux version
Kernel command line
Memory:
```

四项都出现才算「早期启动路径正常」。它**不要求**走到用户态，也不要求 `console [ttyAMA0] enabled`。

### 4.5 2026-11-22 复跑结果

用 run `44992341831`（commit `38d60cd`，`qemu=1`）的 artifact `thyme-droidspaces-30` 重新跑：

```text
Image : .../thyme-droidspaces-30/Image (49317904 字节)
qemu 退出码: 0（124 = 超时/卡住；0 或 1 = 正常结束）
串口输出  : 6685 字节
--- 关键标记 ---
  Booting Linux                    1
  Linux version                    1
  Machine model                    1
  Memory:                          1
  smp:                             0
  console [ttyAMA                  0
  Freeing unused kernel memory     0
  Run /init                        0
  Kernel panic                     1
  Unable to handle kernel          0
  Internal error                   1
结果: 通过（早期启动路径正常）
```

对应的串口尾部：

```text
[    0.163061] rcu: Hierarchical SRCU implementation.
[    0.182251] Internal error: undefined instruction: 0 [#1] PREEMPT SMP
[    0.183317] Process swapper/0 (pid: 1, stack limit = ...)
[    0.184329] pc :           (null)
[    0.184516] sp : 0000000000000000
...
[    0.194304] Kernel panic - not syncing: Attempted to kill init! exitcode=0x0000000b
```

**怎么读这个结果：**

- 这不是「完整启动成功」，而是「没有在控制台注册前静默卡死」。
- 该探针版已经越过了此前导致真机卡死的栈保护缺陷，走到了 SLUB / RCU / SRCU 之后。
- 之后的 undefined instruction（全 0 现场、`pc = null`）是 **QEMU 环境特有**的，真机上不存在；
  当时的目标（定位静默卡死根因）已经达成，这部分没有继续深挖。
- `smp: 0` / `console [ttyAMA 0` 说明还没走到 SMP 和正式控制台注册，符合上述判断。

完整日志：`docs/qemu-logs/probe-run30.log`。

### 4.6 用 GDB 定位「无串口输出」的卡死

当 QEMU 也一个字节都不输出时，唯一能拿到的就是 CPU 现场：

```bash
# 1) 启动 QEMU，开放 gdbstub（-s = :1234，-S = 启动时暂停）
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 2 \
    -nic none -kernel Image \
    -append "console=ttyAMA0 earlycon=pl011,0x09000000 keep_bootcon panic=1 ignore_loglevel loglevel=8" \
    -display none -monitor none -serial file:/tmp/serial.log -no-reboot -s -S

# 2) 另一个终端
gdb-multiarch -q
(gdb) set architecture aarch64
(gdb) target remote :1234
(gdb) info threads
(gdb) info registers pc lr sp x0 x1 x8 x29 x30
(gdb) x/24i $pc
(gdb) continue
```

拿到 PC 后，用 `tools/sym.py`（在本地项目里，未入库；只是一个 `System.map` 二分查找脚本）反查符号：

```bash
python tools/sym.py System.map 0xffffff800813dfc4 0xffffff800acf414c
```

#### 实测现场（镜像来自 run `44906270218`，commit `9f8d4b0`）

```text
* 1  Thread 1.1 (CPU#0 [running]) 0xffffff800813dfc4 in ?? ()
  2  Thread 1.2 (CPU#1 [halted ]) 0x0000000000000000 in ?? ()

Thread 1 (CPU#0):
pc   0xffffff800813dfc4
lr   0xffffff800813dfac
x8   0xffffff800acf414c
x30  0xffffff800813dfac
```

符号化结果（用 `b9358b1` 的 `System.map`；该 commit 只改了 CI 上传逻辑，内核配置与 `9f8d4b0` 相同，
`Image` MD5 因构建时间不同而不同，但符号地址一致，已用反汇编交叉验证；KASLR 已关闭，地址固定）：

```text
0xffffff800813dfac  queued_spin_lock_slowpath + 0x50
0xffffff800813dfc4  queued_spin_lock_slowpath + 0x68
0xffffff800acf414c  die_lock + 0x0
```

即：**CPU0 在 `die_lock` 上自旋，CPU1 从未启动**。这就是「没有任何输出」的直接原因。

另一次运行里还在 `handle_bad_stack` 下断点，抓到异常帧：

```text
Breakpoint 1 at 0xffffff8008091898
Thread 1 hit Breakpoint 1, 0xffffff8008091898 in ?? ()
=== handle_bad_stack 命中 ===
x0  0xffffff800a923f40      <- pt_regs
x1  0x96000005              <- ESR
--- pt_regs ---
0xffffff800a923f40: 0x0000000000000028 0x0000000096000005
...
0xffffff800a924030: 0xffffff80080833c4 0xffffff800a9fffc0
0xffffff800a924040: 0xffffff80080816b0 0x00000000604001c5
```

符号化：

```text
0x0000000000000028  <故障地址>
0xffffff8008082894  __bad_stack + 0xc0
0xffffff80080833c4  el1_ia + 0x1c
0xffffff80080816b0  do_mem_abort + 0x2c
```

故障地址是 **`0x28`**，ESR `0x96000005` = 数据 abort / translation fault。完整证据：
`docs/qemu-logs/gdb-queued-spinlock.txt`、`docs/qemu-logs/badstack-far-0x28.txt`。

### 4.7 根因链条：AOSP Clang 的 TLS 栈保护守卫

AOSP Clang 在 `aarch64-linux-android` 目标下默认使用 **TLS/sysreg** 栈保护守卫：

```asm
mrs xN, tpidr_el0            ; 内核态恒为 0
ldr xM, [xN, #0x28]          ; 读 canary
```

这是 bionic 用户态的约定。但内核态 `tpidr_el0` 恒为 0，于是函数序言第一条 canary 读取就访问地址 **`0x28`**：

```text
canary 读取 [tpidr_el0 + 0x28]
        │  tpidr_el0 = 0
        ▼
地址 0x28 缺页
        │  发生在 SP 还不在任务栈上时
        ▼
arm64 判定为 kernel stack overflow → __bad_stack → die()
        │  die() 再拿 die_lock
        ▼
die_lock 已被同一 CPU 持有 → queued_spin_lock_slowpath 自旋
        ▼
永远不返回 → 串口无输出、pstore 为空、卡 logo
```

修复分三层：

| 层 | 做法 |
|---|---|
| 编译参数 | `KCFLAGS="-mstack-protector-guard=global"`，canary 改用全局 `__stack_chk_guard`（4.19 arm64 的约定） |
| 构建前预检 | 编译一个带栈数组的小函数，反汇编必须出现 `__stack_chk_guard`，且不能出现 `tpidr_el0`；不满足 3 分钟内 fail |
| 构建后复核 | 反汇编真正链接的 `vmlinux` 的 `start_kernel`，再确认一次没有 `tpidr_el0` |

预检本身也踩过坑：第一版用 `-fstack-protector-strong -O2`，编译器把函数体优化成一条 `ldrb`，
canary 根本没生成，导致健康工具链被误判（run `44973181442` 报
`ERROR: 预检没有看到 __stack_chk_guard`）。后来改用 `-fstack-protector-all` 才可靠。

### 4.8 另外两个 QEMU 阶段

| 阶段 | 日志现象 | 根因 | 处理 |
|---|---|---|---|
| SCM oops（pq4） | `Unable to handle kernel read ... 0x18`，`pc: kmem_cache_alloc+0x4c`；call trace `scm_call2 → init_random_pool → setup_arch` | QEMU 无高通 SCM，`scm_call2` 触发 undefined instruction；此时 `sigqueue` 缓存还没建好，undef 处理自己在 `kmem_cache_alloc` 里空指针 oops，循环 #1…#9 | 探针版注释 `init_random_pool()`（`e063564`） |
| 残留 undef（pq5 / run30） | `rcu: Hierarchical SRCU implementation.` 后 `Internal error: undefined instruction: 0`，全 0 现场，`pc = null` | QEMU 环境特有的另一处早期异常，不再是栈保护问题 | 未继续追；真机验证已通过，探针目标已达成 |

pq4 的完整调用链（`docs/qemu-logs/probe-pq4-scm-oops.log`）：

```text
pc : kmem_cache_alloc+0x4c/0x324
Call trace:
 kmem_cache_alloc+0x4c/0x324
 __sigqueue_alloc+0x84/0x124
 __send_signal+0xd0/0x3ec
 force_sig_info+0x160/0x18c
 arm64_force_sig_info+0x70/0xfc
 force_signal_inject+0xf8/0x154
 do_undefinstr+0x444/0x490
 el1_undef+0x10/0xa0
 is_scm_armv8+0x50/0xe8
 scm_call2+0x10/0x18
 init_random_pool+0x74/0xec
 setup_arch+0x1b8/0x1d8
 start_kernel+0x64/0x378
```

### 4.9 QEMU 能证明什么、不能证明什么

| 能证明 | 不能证明 |
|---|---|
| head.S / 重定位 / RELR / KASLR 是否正常 | SM8250 的 DRM、KGSL、相机、WiFi、音频、UFS、充电 |
| alternatives / 早期加固补丁是否踩错点位 | `vendor_boot` 里的 kona DTB 是否匹配 |
| 栈保护 canary 是否用对（global vs TLS） | 原厂 vendor `.ko` 能否加载（CRC / vermagic） |
| MMU、页表、内存初始化、SLUB | Android 13 的 FCM / VINTF 校验（`VintfObject.verifyWithoutAvb()`） |
| GIC / arch timer / PSCI / SMP 早期路径 | SELinux 策略、MIUI 服务、指纹、NFC |
| `start_kernel` 到 console / RCU 的通用代码 | Droidspaces 容器能否真正启动、NAT 是否通 |

结论：**QEMU 是“必要的早期冒烟”，不是“充分的验收”**。最终门槛仍是真机 `verify_device.ps1`（31 PASS）
和 `verify_droidspaces.ps1 -TestShell`。

---

## 5. 从 CI 产物到刷机

> 这一节的脚本（`fetch_artifact.ps1`、`finalize_boot.ps1`、`patch_apatch.ps1`、`flash_test.ps1`）
> **不在本仓库**，它们在构建仓库之外的本地项目里，只负责“下载 → 重打包 → 刷机 → 体检”。
> 本仓库的职责到产出 `Image` 为止。

```powershell
# 1) 下载 artifact（P8：run 45025799106 → thyme-droidspaces-37）
powershell -ExecutionPolicy Bypass -File .\tools\fetch_artifact.ps1 -Run 45025799106 -Out .\dist\runP8

# 2) 无 root 镜像
powershell -ExecutionPolicy Bypass -File .\tools\finalize_boot.ps1 `
  -Artifact .\dist\runP8 -Out .\boot-P8-stock.img -Profile layer

# 3) 打 folkpatch（用设备上管理器自带的 kptools + kpimg）
powershell -ExecutionPolicy Bypass -File .\tools\patch_apatch.ps1 `
  -Image .\dist\runP8\Image `
  -Out .\dist\runP8\Image-folkpatch `
  -Kpimg "/data/data/me.yuki.folk/check/kpimg" `
  -Kptools "/data/data/me.yuki.folk/check/kptools"

# 4) 用打好补丁的 Image 重打包（finalize_boot.ps1 固定读 <Artifact>\Image）
New-Item -ItemType Directory -Force .\dist\runP8fp | Out-Null
Copy-Item .\dist\runP8\* .\dist\runP8fp\ -Force
Copy-Item .\dist\runP8\Image-folkpatch .\dist\runP8fp\Image -Force
powershell -ExecutionPolicy Bypass -File .\tools\finalize_boot.ps1 `
  -Artifact .\dist\runP8fp -Out .\boot-P8-folkpatch.img -Profile layer

# 5) 刷入 + 体检
powershell -ExecutionPolicy Bypass -File .\tools\flash_test.ps1 `
  -Image .\boot-P8-folkpatch.img -Profile layer
```

关键约束：

- `finalize_boot.ps1 -Profile layer` 会强制检查 `SYSVIPC` / `IP6_NF_NAT` / `NF_NAT_IPV6` /
  `IP6_NF_TARGET_MASQUERADE` 为 `n`，并检查容器所需项为 `y`；
- 只替换 `boot.img` 的 kernel 段，ramdisk / vbmeta / cmdline / 分区大小逐字节不变（15 项结构校验）；
- **`qemu_probe=1` 的镜像绝对不能刷**：它没有原厂串口配置，还改过 `setup.c`。

### 5.1 CI 直接出可刷镜像（`publish_release=1`）

上面的 `finalize_boot.ps1` 需要本地那份 128 MiB 原厂 `boot.img`。为了让 Actions 自己也能出
可刷镜像，仓库里存了一份**布局模板**而不是整只 boot.img：

| 文件 | 大小 | 内容 |
|---|---|---|
| `bootimg/thyme-boot-template.bin` | 19.7 MB | 4096 B v3 header + 对齐后的原厂 ramdisk + AVB vbmeta(896 B) + footer(64 B)，**不含原厂 kernel** |
| `bootimg/thyme-boot-template.json` | 1 KB | `total_size`、`ramdisk_md5`、`vbmeta_offset/sha256`、`cmdline`、`header_sha256`、`footer_sha256(_masked)` |

为什么不是 128 MB：kernel 段占 52.7 MB、尾部到 footer 之间 61.8 MB 全是零填充，都可以从
`total_size` / `vbmeta_offset` 推出来，没必要进仓库。

`publish_release=1` 时多出的两步：

```bash
python3 tools/pack_boot.py  --template bootimg/thyme-boot-template.bin \
    --manifest bootimg/thyme-boot-template.json --image dist/Image \
    --out dist/boot-mi10s-droidspaces.img

python3 tools/verify_boot.py --boot dist/boot-mi10s-droidspaces.img \
    --manifest bootimg/thyme-boot-template.json --image dist/Image
```

`verify_boot.py` 的 17 项里，关键几条：

| 检查 | 含义 |
|---|---|
| `kernel bytes == Image` | 写入的确实是这次构建的 `Image` |
| `kernel padding zeroed` | kernel 尾部按页对齐补零，没有残留旧数据 |
| `ramdisk MD5` / `os_version` / `cmdline` | 三样都和原厂完全一致 |
| `vbmeta bytes unchanged` / `footer body unchanged` | AVB 段没被改动（kernel 变大时只修正偏移字段） |
| `no root patch payload` | 出现 `KernelPatch` / `KernelSU` / `kpatch` 就拒绝发布 |

这 17 项全过才会 `gh release create`。发布内容：`boot-mi10s-droidspaces.img`、`Image`、
`AnyKernel3` zip、`System.map`、`config-final.txt`、`SHA256SUMS`；标签取输入 `version`（`vX.Y.Z`）。

两个安全闸：

- `qemu_probe=1` + `publish_release=1` → 重打包步骤第一行就 `exit 1`；
- `publish_release=1` 但构建失败 → 发布步骤 `success()` 不成立，不会创建空 release。

**与已验证路径的等价性**：拿本项目已刷入验证的 `Image` 走一遍 `pack_boot.py`，输出与真机验证过的
无 root 镜像 **MD5 完全相同**（`67F5E7337DB2F153951035FB69B8C2BB`）；用打过 root 补丁的
`Image` 再跑一次，也复现了当时刷入的 `F7E2743CAC8D57D9489BFB7938FD7FB8`（并被
`no root patch payload` 正确拦下）。所以 CI 这条路径不是另一套实现。

模板可用自己的原厂 boot.img 重新生成（需要 header v3 + AVB footer）：

```bash
python3 tools/make_boot_template.py --boot stock_boot.img \
    --out-dir bootimg --name thyme-boot-template --firmware V14.0.6.0.TGACNXM
```

**端到端实跑记录**（2026-11-23，run `45169780558`，14 分 9 秒）：

| 步骤 | 结果 |
|---|---|
| Configure + Build | success（`Image` 49,317,904 B） |
| Repack boot.img | 17 项全 PASS，`boot-mi10s-droidspaces.img` 134,217,728 B |
| Upload artifacts | `thyme-droidspaces-38`，87.7 MB（含重打包后的 `boot.img`） |
| Publish release | 当时发布的 `release-38` 已撤回，由 `v1.0.0` 取代 |

当前正式版 [`v1.0.0`](https://github.com/jingbohi/mi10s-droidspaces-kernel/releases/tag/v1.0.0)
同时提供两份 boot 镜像，其余附件只有一套：

| 附件 | 来源 | 验证状态 |
|---|---|---|
| `boot-mi10s-droidspaces.img` | 本地构建（`Image` MD5 `AC660034…`） | **真机刷入验证，31 PASS / 0 FAIL** |
| `boot-mi10s-droidspaces-ci.img` | run `45169780558` 的 CI 构建 | 未刷机验证，与本地版仅构建时间戳不同 |

注意：每次构建的内核里带编译时间戳，所以 `Image` / `boot.img` 的 MD5 每次不同（体积可以一样）；
跨版本比对请认 release 里的 `SHA256SUMS`，不要拿旧 MD5 判断新构建是否有问题。

---

## 6. 时间线（2026-11-21 ~ 11-22）

| 时间（UTC） | 事件 |
|---|---|
| 11-21 12:55 | `9f8d4b0` 构建成功，第一次拿 QEMU 探针镜像 |
| 11-21 14:14 | WSL 里 QEMU + GDB：CPU0 卡在 `queued_spin_lock_slowpath`，x8 = `die_lock` |
| 11-22 01:16 | `b9358b1` 增加 `System.map` 上传 |
| 11-22 01:48 | `6fa85db` 预检误报，3.3 分钟失败 |
| 11-22 03:14 | `06af24b` 修正预检（`-fstack-protector-all`），pq4 复现 SCM oops |
| 11-22 04:49 | `e063564` 探针跳过 SCM，pq5 越过 SCM |
| 11-22 06:07 | `38d60cd` 探针复跑（本文 4.5 节），残留 QEMU-only undef |
| 11-22 13:07 | `a58803b` 最终内核（P7/P8）构建，12–17 分钟 |
| 11-22 22:xx | 用 115404 kpimg 生成 P8、刷入，31 PASS / 0 FAIL |

---

## 7. 经验清单

1. **所有 `-f` 都要传**：漏传会静默使用 pipa 树的默认值，编译 15 分钟后才发现源码不对。
2. **固定 commit，不用分支名**：`thirteen` 分支会动，固定 `bc9f46d...` 才能复现。
3. **`allow_config_drift=1` 是这台机器的必需项**：仓库自带原厂 config 来自另一棵树；FCM 门禁仍然生效。
4. **看到 `Image` 里出现 `KernelPatch` / `KernelSU` 就停**：说明拿到的是设备端补丁产物，不是 CI 产物。
5. **QEMU 必须开 PL011**，否则没有串口；`-nic none` 必须加，否则 WSL 的 QEMU 直接退出。
6. **`grep -c "console [ttyAMA"` 是非法正则**（未闭合的 `[`），脚本里要用 `grep -F`。
7. **无输出卡死优先怀疑栈保护守卫**：`tpidr_el0 + 0x28`，不是配置丢项。
8. **QEMU 通过 ≠ 能刷**：它只覆盖通用启动路径；最终必须真机 31 PASS + 容器 200。
9. **artifact 只保留 30 天**：过期需重新构建；关键产物（P8 镜像、日志）已复制到本地项目。
10. **Private 仓库消耗 Actions 免费额度**（2000 分钟/月），本构建约 12–17 分钟/次；改 Public 后不计费。

---

## 8. 证据文件

| 文件 | 内容 |
|---|---|
| `docs/qemu-logs/gdb-queued-spinlock.txt` | GDB 现场：CPU0 `queued_spin_lock_slowpath`，x8 = `die_lock`，CPU1 halted |
| `docs/qemu-logs/badstack-far-0x28.txt` | `handle_bad_stack` 断点 + pt_regs：故障地址 `0x28`，ESR `0x96000005` |
| `docs/qemu-logs/probe-pq4-scm-oops.log` | SCM oops 完整调用链（`scm_call2 → init_random_pool`） |
| `docs/qemu-logs/probe-pq5-undef.log` | 跳过 SCM 后仍出现的 QEMU-only undefined instruction |
| `docs/qemu-logs/probe-run30.log` | 2026-11-22 用归档 artifact 复跑的完整串口日志 |
| `dist/runP8/System.map` / `start_kernel.dis`（本地项目） | 最终内核符号表 / `start_kernel` 反汇编 |
| `crashlog/verify-20261122-222915.txt`（本地项目） | P8 真机体检：31 PASS / 0 FAIL |
| `crashlog/droidspaces-20261122-223038.txt`（本地项目） | P8 容器验证日志 |

---

## 9. 复现手册

### 9.1 重新构建探针镜像

```powershell
cd droidspaces-kernel
gh workflow run build.yml `
  -f kernel_repo=https://github.com/PixelExperience-Devices/kernel_xiaomi_thyme `
  -f kernel_branch=bc9f46dca6f2d608578180543cc2ff9ee5ee8a82 `
  -f defconfig=thyme_defconfig `
  -f allow_crc_mismatch=1 -f droidspaces_layer=1 `
  -f disable_relr=1 -f disable_kaslr=1 `
  -f allow_config_drift=1 -f disable_arm64_hardening=0 -f qemu_probe=1
```

等 Actions 绿勾，下载 artifact：

```powershell
gh run list --workflow build.yml --limit 3
gh run download <run_id> -D $env:TEMP\probe
```

下载目录结构是 `<out>\thyme-droidspaces-<run_number>\Image`；WSL 里对应
`/mnt/c/Users/<用户名>/AppData/Local/Temp/probe/thyme-droidspaces-<run_number>/Image`。

### 9.2 在 WSL 里跑

```bash
sudo apt-get install -y qemu-system-arm gdb-multiarch   # 只需一次
bash tools/qemu_probe.sh /tmp/probe/thyme-droidspaces-XX/Image /tmp/qemu.log 45
```

看输出：

- 四项标记齐全 → 早期路径通过；
- 0 字节日志 → 先确认 `-nic none`、镜像是否 `qemu_probe=1`、PL011 是否 `y`；
- 有日志但卡住 → 用 4.6 节的 GDB 方法抓 PC，再用 `System.map` 反查。

### 9.3 需要抓现场时

```bash
# 终端 1
qemu-system-aarch64 -M virt -cpu cortex-a72 -m 2048 -smp 2 -nic none \
  -kernel Image \
  -append "console=ttyAMA0 earlycon=pl011,0x09000000 keep_bootcon panic=1 ignore_loglevel loglevel=8" \
  -display none -monitor none -serial file:/tmp/serial.log -no-reboot -s -S

# 终端 2
gdb-multiarch -q -ex "set architecture aarch64" -ex "target remote :1234" \
  -ex "info threads" -ex "info registers pc lr sp x8" -ex "x/24i \$pc"

# 反查（tools/sym.py 在本地项目里，不在本仓库）
python tools/sym.py System.map 0xffffff800813dfc4
```

### 9.4 最后提醒

- 探针镜像只用于本机 QEMU，**不要刷机**；
- 刷机镜像必须重新构建，`qemu_probe=0`；
- 真机验收用 `tools/flash_test.ps1`，它会自动跑 pstore 检查和 `verify_device.ps1`；
- 卡死恢复：音量减 + 电源约 15 秒 → fastboot → 刷 `boot-rollback-apatch.img`。
