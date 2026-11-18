# Oracle Always Free AMD Micro Hunter 部署指引

本文说明如何部署本 fork。代码只允许创建
`VM.Standard.E2.1.Micro`，不接受 ARM shape、Windows 镜像或 Marketplace
商业化镜像。

## 1. 部署前检查

确认以下条件成立：

- 已有 ARM 实例不受影响，本脚本不会启动、停止或修改 ARM。
- OCI API 配置必须位于 tenancy 的 home region。
- 已准备一个 regional subnet。
- 已确认目标 Availability Domain。
- 计划最多创建两台 Always Free Micro。
- 当前 tenancy 的实例、启动卷和数据卷合计不超过 Always Free 额度。

脚本会在 Launch 前检查租户 compartment 层级中的资源和块存储占用。如果
API 用户无法读取这些资源，脚本会失败停止，不会在额度状态不明确时继续抢机。

## 2. 准备服务器

支持 Linux、macOS。Python 要求 3.10 或更高版本。

Ubuntu / Debian 示例：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv pip tmux
```

克隆 fork 分支：

```bash
git clone --branch fix/micro-hunter-safety \
  https://github.com/praxis-works/oracle-freetier-instance-creation.git

cd oracle-freetier-instance-creation
```

## 3. 创建 OCI API 配置

在 OCI Console 中为用户创建 API Key，并记录：

- user OCID
- tenancy OCID
- fingerprint
- home region
- API private key 文件路径

参考仓库中的 `sample_oci_config`，创建 `oci_config`：

```ini
[DEFAULT]
user=ocid1.user.oc1..替换为你的用户OCID
fingerprint=替换为APIKey指纹
tenancy=ocid1.tenancy.oc1..替换为你的租户OCID
region=替换为你的home region
key_file=/absolute/path/to/oci_api_private_key.pem
```

权限必须收紧：

```bash
chmod 600 oci_config
chmod 600 /absolute/path/to/oci_api_private_key.pem
```

API 用户至少需要能够：

- 读取 tenancy 内的 compartment。
- 读取实例、启动卷和数据卷。
- 读取 availability domain、subnet 和 image。
- 在目标 compartment 创建实例。
- 在目标 regional subnet 上创建 VNIC。

建议使用最小权限策略，不要使用不必要的 tenancy 管理员权限。

## 4. 确认目标资源

### Home Region

`oci_config` 中的 `region` 必须与 tenancy 的 home region 一致。脚本会通过
Identity API 验证，不一致时直接退出。

### Availability Domain

在 OCI Console 的实例创建页面查看 Free Tier 可用的 Availability Domain。
`MICRO_AD` 可以使用后缀：

```bash
MICRO_AD=PHX-AD-1
```

如果匹配到多个 AD，脚本会停止。此时填写完整 AD 名称，例如：

```bash
MICRO_AD=Uocm:PHX-AD-1
```

### Regional Subnet

在 Networking 中选择目标 VCN 的 regional subnet，复制 subnet OCID：

```bash
OCI_SUBNET_ID=ocid1.subnet...
```

不能使用绑定到其他 AD 的 AD-specific subnet。

如果需要公网 IP，subnet 必须允许 public IP，并具备正确的 route table 和
Internet Gateway。

### Image

默认使用：

```bash
OPERATING_SYSTEM="Canonical Ubuntu"
OS_VERSION=22.04
```

也可以使用 Oracle Linux。显式指定 `OCI_IMAGE_ID` 时，脚本会校验：

- image 可用。
- image 支持 Micro shape。
- image 不是 Marketplace listing。
- image 操作系统是 Ubuntu 或 Oracle Linux。
- image 系统与 `OPERATING_SYSTEM` 一致。

## 5. 创建环境配置

从模板创建本地配置：

```bash
cp oci.env.example oci.env
chmod 600 oci.env
```

编辑 `oci.env`：

```bash
OCI_CONFIG=/absolute/path/to/oci_config
OCI_COMPARTMENT_ID=

MICRO_AD=PHX-AD-1
OCI_SUBNET_ID=ocid1.subnet...
OCI_IMAGE_ID=

OCI_COMPUTE_SHAPE=VM.Standard.E2.1.Micro
OPERATING_SYSTEM="Canonical Ubuntu"
OS_VERSION=22.04

MICRO_NAME_PREFIX=micro
MICRO_SSH_PUBLIC_KEY=/absolute/path/to/id_rsa.pub

REQUEST_WAIT_TIME_SECS=120
MAX_RUNTIME_HOURS=6
MAX_LAUNCH_ATTEMPTS=180
MAX_BACKOFF_SECS=900
MAX_RATE_LIMIT_FAILURES=5
RECONCILE_GRACE_SECS=180

BOOT_VOLUME_SIZE=50
MAX_TOTAL_BLOCK_VOLUME_GB=200
ASSIGN_PUBLIC_IP=true
```

说明：

- `OCI_COMPARTMENT_ID` 为空时使用 tenancy OCID；填写后仅在该 compartment
  创建实例，但额度检查仍覆盖 tenancy compartment 层级。
- `OCI_COMPUTE_SHAPE` 只能为空或 `VM.Standard.E2.1.Micro`。
- `BOOT_VOLUME_SIZE` 最小 50 GB。
- `MAX_TOTAL_BLOCK_VOLUME_GB` 不能超过 200 GB。
- `ASSIGN_PUBLIC_IP` 不属于计算 shape 本身。若要求所有网络资源也绝对零费用，
  应先确认 OCI 当前的 public IPv4 计费政策，必要时设为 `false`。

## 6. 安装依赖并进行只读预检

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

执行只读检查：

```bash
.venv/bin/python micro_hunter.py --dry-run
```

预期输出包含：

- home-region AD
- subnet OCID
- image OCID
- 当前非终止状态 Micro 数量
- 当前块存储占用
- 下一次准备创建的实例名称

`--dry-run` 不会调用 LaunchInstance。

## 7. 方式 A：tmux 临时部署

适合首次验证或短时间抢机。

```bash
tmux new -s micro-hunter
./setup_init.sh micro
```

按 `Ctrl+B`，再按 `D` 退出 tmux，任务继续运行。

重新连接：

```bash
tmux attach -t micro-hunter
```

停止：

- 连接 tmux 后按 `Ctrl+C`。
- 或终止对应 Python 进程。

查看日志：

```bash
tail -f micro_hunter.log
```

后续重跑：

```bash
./setup_init.sh micro-rerun
```

## 8. 方式 B：systemd 部署

适合固定主机。建议部署到 `/opt`：

```bash
sudo mkdir -p /opt/oracle-freetier-instance-creation
sudo chown -R "$USER":"$USER" /opt/oracle-freetier-instance-creation
```

将仓库部署到 `/opt/oracle-freetier-instance-creation`，完成 `oci.env` 和
`.venv` 配置后，编辑：

```bash
deploy/oracle-micro-hunter.service
```

需要修改：

- `User=`
- `Group=`
- `WorkingDirectory=`
- `ExecStart=`
- `ReadWritePaths=`

安装服务：

```bash
sudo cp deploy/oracle-micro-hunter.service \
  /etc/systemd/system/oracle-micro-hunter.service
sudo systemctl daemon-reload
sudo systemctl enable --now oracle-micro-hunter
```

状态与日志：

```bash
sudo systemctl status oracle-micro-hunter
journalctl -u oracle-micro-hunter -f
tail -f /opt/oracle-freetier-instance-creation/micro_hunter.log
```

停止并取消下次启动：

```bash
sudo systemctl disable --now oracle-micro-hunter
```

服务模板故意设置为 `Restart=no`。配置错误、429 或额度异常时不要快速重启，
应先查看日志并修复原因。

## 9. 运行状态文件

Launch 前会生成：

```text
.micro_hunter_state.json
```

文件权限为 `0600`，用于保存当前 Launch 的幂等 token。网络超时后不要手工删除，
脚本会先按实例名称对账。只有确认 OCI 中不存在该实例且超过 reconciliation
窗口后，脚本才会复用同一 token 重试。

## 10. 常见问题

### `OCI_COMPUTE_SHAPE` 配置错误

错误示例：

```text
This fork only permits VM.Standard.E2.1.Micro
```

处理：

```bash
OCI_COMPUTE_SHAPE=VM.Standard.E2.1.Micro
```

也可以留空，但不能填写 ARM 或任何其他 shape。

### AD 匹配数量不为 1

填写 OCI Console 中显示的完整 AD 名称。

### Boot volume preflight failed

租户块存储占用加新增启动卷会超过 200 GB。删除不需要的启动卷、数据卷或确认
是否有其他 compartment 的资源占用额度。

### Existing Micro instance is STOPPED

脚本不会创建替代实例。请在 OCI Console 中手动启动现有 Micro，或确认是否应
终止旧实例。

### `LimitExceeded`

立即停机。检查 Compute、VNIC、Block Volume 和 Always Free 配额。不要在未知
额度状态下反复重启。

### `TooManyRequests`

脚本会指数退避，连续超过阈值后退出。等待一段时间再运行，不要缩短
`REQUEST_WAIT_TIME_SECS`。

### API 无法读取 compartment 或卷

这是 fail-closed 行为。补齐只读权限后再运行，不要绕过额度检查。

## 11. 上线检查清单

- [ ] 分支为 `fix/micro-hunter-safety`
- [ ] `oci.env` 权限为 `0600`
- [ ] API private key 权限为 `0600`
- [ ] `oci_config` region 是 home region
- [ ] `MICRO_AD` 唯一匹配
- [ ] `OCI_SUBNET_ID` 是 regional subnet
- [ ] `OCI_COMPUTE_SHAPE` 为空或 `VM.Standard.E2.1.Micro`
- [ ] `OPERATING_SYSTEM` 是 Ubuntu 或 Oracle Linux
- [ ] `BOOT_VOLUME_SIZE` 至少 50
- [ ] `MAX_TOTAL_BLOCK_VOLUME_GB` 不超过 200
- [ ] `--dry-run` 成功
- [ ] 日志和 systemd 输出中没有 API private key 或用户密码
- [ ] 两台 Micro 达到 `RUNNING` 后停止任务

## 12. 升级

```bash
git fetch origin
git switch fix/micro-hunter-safety
git pull --ff-only
.venv/bin/pip install -r requirements.txt
.venv/bin/python micro_hunter.py --dry-run
```

升级后先 dry-run，再重新启动服务或 tmux 任务。
