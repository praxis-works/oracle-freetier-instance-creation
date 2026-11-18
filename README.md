# Oracle Always Free AMD Micro Hunter

This fork is restricted to the Always Free AMD shape
`VM.Standard.E2.1.Micro`. It is intended for a tenancy that already has ARM
capacity and only needs the remaining two Micro instances.

## Hard restrictions

The code refuses to proceed unless all of these conditions hold:

- The launch shape is exactly `VM.Standard.E2.1.Micro`.
- The maximum number of non-terminated Micro instances is two.
- The API config points to the tenancy home region.
- The subnet is regional and available.
- The selected availability domain resolves uniquely.
- The image is available and compatible with the Micro shape.
- The operating system is Ubuntu or Oracle Linux, never Windows.
- Marketplace images with listing identifiers are rejected.
- Existing block volumes plus the proposed boot volume do not exceed 200 GB.
- Existing stopped or in-flight Micro instances are inspected instead of
  creating replacements.

`main.py` is only a compatibility alias for `micro_hunter.py`. The original
generic ARM launch implementation has been removed. `OCI_COMPUTE_SHAPE` is
accepted only when absent or exactly `VM.Standard.E2.1.Micro`; any other value
causes a configuration error.

## What it does

- Counts non-terminated Micro instances across accessible compartments.
- Launches sequentially as `micro-1`, then `micro-2`.
- Treats only an exact display name with Micro shape and lifecycle state
  `RUNNING` as a completed launch.
- Omits `shape_config` for the fixed 1 OCPU / 1 GB shape.
- Persists an `opc_retry_token` before LaunchInstance.
- Reconciles ambiguous timeouts by display name before retrying.
- Uses at least 120 seconds between capacity attempts by default.
- Stops on `LimitExceeded`, authentication, authorization, invalid parameters,
  repeated `TooManyRequests`, runtime limits, or launch-attempt limits.
- Writes credentials and state files with mode `0600`, and rotates logs.

No automation can guarantee that an OCI tenancy will never be reviewed or have
Always Free limits reduced. Keep the conservative intervals and stop after the
two intended instances are running.

## Requirements

- Python 3.10 or newer.
- An OCI API key and config in the tenancy home region.
- A regional subnet that can launch `VM.Standard.E2.1.Micro`.
- The exact availability domain eligible for your Free Tier resources.
- An SSH public key, or permission for the script to generate one.

## Setup

```bash
git clone https://github.com/praxis-works/oracle-freetier-instance-creation.git
cd oracle-freetier-instance-creation
cp oci.env.example oci.env
chmod 600 oci.env
```

Edit `oci.env`:

```bash
OCI_CONFIG=/absolute/path/to/oci_config
MICRO_AD=PHX-AD-1
OCI_SUBNET_ID=ocid1.subnet...
MICRO_NAME_PREFIX=micro
MICRO_SSH_PUBLIC_KEY=/absolute/path/to/id_rsa.pub
REQUEST_WAIT_TIME_SECS=120
MAX_RUNTIME_HOURS=6
MAX_LAUNCH_ATTEMPTS=180
BOOT_VOLUME_SIZE=50
MAX_TOTAL_BLOCK_VOLUME_GB=200
ASSIGN_PUBLIC_IP=true
```

`OCI_COMPARTMENT_ID` is optional and defaults to the tenancy OCID. When set,
the instance is launched in that compartment, while quota checks still traverse
the tenancy compartment hierarchy.

If `MICRO_AD` matches more than one availability domain, use the full AD name
shown in the OCI Console.

## Run

Validate configuration without launching:

```bash
python3 micro_hunter.py --dry-run
```

Install dependencies and start:

```bash
./setup_init.sh micro
```

Skipping dependency installation on later runs:

```bash
./setup_init.sh micro-rerun
```

For compatibility, `python3 main.py` invokes the same Micro-only entry point.

## Deployment guide

For API key setup, OCI resource discovery, tmux deployment, systemd deployment,
logging, stopping, and troubleshooting, see
[DEPLOYMENT_zh-CN.md](DEPLOYMENT_zh-CN.md).

## Cost boundary

The compute request is hard-coded to `VM.Standard.E2.1.Micro`; no ARM or paid
compute shape can be selected through configuration. The script also refuses
to launch when the tenant-wide block volume inventory would exceed the 200 GB
Always Free allocation.

This guard covers compute shape, instance count, boot-volume storage, marketplace
image metadata, and home-region placement. Network data transfer, public IPv4
policy, account-wide limits, or other tenancy resources are controlled by OCI
and are outside what a client-side script can fully guarantee.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile main.py micro_hunter.py tests/test_micro_hunter.py
bash -n setup_init.sh setup_env.sh
```

The unit tests verify that only Micro shape requests are generated, ARM
instances do not block Micro acquisition, quotas stop unsafe launches, and
ambiguous timeouts do not duplicate instances.

## Credits

Based on
[mohankumarpaluru/oracle-freetier-instance-creation](https://github.com/mohankumarpaluru/oracle-freetier-instance-creation).
See [LICENSE](LICENSE) for the upstream license.
