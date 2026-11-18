#!/usr/bin/env python3
"""Safely acquire up to two Always Free VM.Standard.E2.1.Micro instances."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import random
import re
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import oci
except ImportError:  # Tests inject a small compatibility object.
    oci = None


MICRO_SHAPE = "VM.Standard.E2.1.Micro"
TERMINAL_STATES = {"TERMINATED"}
IN_FLIGHT_STATES = {
    "PROVISIONING",
    "STARTING",
    "MOVING",
    "REBOOTING",
    "TERMINATING",
}
CAPACITY_MESSAGES = (
    "out of host capacity",
    "out of capacity",
    "capacity for shape",
    "not enough capacity",
)


class HunterError(Exception):
    """Base class for expected Micro hunter failures."""


class ConfigError(HunterError):
    """Configuration is unsafe or incomplete."""


class FatalServiceError(HunterError):
    """An OCI response means retrying would be unsafe."""


class RetryCapacityError(HunterError):
    """The launch was definitively rejected for lack of capacity."""


class RetryBackoffError(HunterError):
    """The request was rate-limited and should use exponential backoff."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in {"true", "1", "yes", "y"}:
        return True
    if raw in {"false", "0", "no", "n"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    oci_config_path: Path
    compartment_id: str
    tenancy_id: str
    micro_ad: str
    subnet_id: str
    image_id: str | None
    operating_system: str
    os_version: str
    micro_count: int
    name_prefix: str
    request_wait_secs: int
    max_backoff_secs: int
    max_runtime_secs: int
    max_launch_attempts: int
    max_rate_limit_failures: int
    reconcile_grace_secs: int
    boot_volume_size_gb: int
    max_total_block_volume_gb: int
    assign_public_ip: bool
    ssh_authorized_keys_file: Path
    state_file: Path
    log_file: Path
    discord_webhook: str

    def desired_names(self) -> list[str]:
        return [f"{self.name_prefix}-{index}" for index in range(1, self.micro_count + 1)]


def _read_oci_config(path: Path) -> tuple[str, Path | None]:
    parser = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except OSError as exc:
        raise ConfigError(f"Cannot read OCI config {path}: {exc}") from exc
    try:
        tenancy_id = parser.get("DEFAULT", "tenancy").strip()
        user_id = parser.get("DEFAULT", "user").strip()
    except (configparser.NoOptionError, configparser.NoSectionError) as exc:
        raise ConfigError(f"{path} must contain tenancy and user in [DEFAULT]") from exc
    if not tenancy_id.startswith("ocid1.tenancy.") or not user_id.startswith("ocid1.user."):
        raise ConfigError("OCI config tenancy/user values must be OCIDs")
    key_file = parser.get("DEFAULT", "key_file", fallback="").strip()
    return tenancy_id, _expand_path(key_file) if key_file else None


def _secure_file(path: Path) -> None:
    """Make a sensitive file owner-only readable and writable."""
    try:
        if not path.exists():
            return
        current = stat.S_IMODE(path.stat().st_mode)
        if current != stat.S_IRUSR | stat.S_IWUSR:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            logging.warning("Restricted permissions on %s to 0600", path)
    except OSError as exc:
        raise ConfigError(f"Cannot secure {path}: {exc}") from exc


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigError("python-dotenv is required; run ./setup_init.sh") from exc

    env_file = Path("oci.env")
    if env_file.exists():
        _secure_file(env_file)
        load_dotenv(env_file, override=False)

    oci_config_path = _expand_path(_env("OCI_CONFIG", "~/.oci/config"))
    tenancy_id, key_file = _read_oci_config(oci_config_path)
    _secure_file(oci_config_path)
    if key_file:
        if not key_file.is_file():
            raise ConfigError(f"OCI API private key does not exist: {key_file}")
        _secure_file(key_file)

    compartment_id = _env("OCI_COMPARTMENT_ID", tenancy_id)
    if not compartment_id.startswith("ocid1.compartment.") and compartment_id != tenancy_id:
        raise ConfigError("OCI_COMPARTMENT_ID must be a compartment or tenancy OCID")

    micro_ad = _env("MICRO_AD", _env("OCT_FREE_AD"))
    subnet_id = _env("OCI_SUBNET_ID")
    if not micro_ad:
        raise ConfigError("MICRO_AD is required (for example PHX-AD-1)")
    if not subnet_id.startswith("ocid1.subnet."):
        raise ConfigError("OCI_SUBNET_ID must be set to an existing regional subnet OCID")

    image_id = _env("OCI_IMAGE_ID") or None
    if image_id and not image_id.startswith("ocid1.image."):
        raise ConfigError("OCI_IMAGE_ID must be an image OCID")

    request_wait_secs = _env_int("REQUEST_WAIT_TIME_SECS", 120, minimum=60)
    max_backoff_secs = _env_int("MAX_BACKOFF_SECS", 900, minimum=request_wait_secs)
    reconcile_grace_secs = _env_int(
        "RECONCILE_GRACE_SECS", 180, minimum=request_wait_secs
    )
    boot_volume_size_gb = _env_int("BOOT_VOLUME_SIZE", 50, minimum=50)
    max_total_block_volume_gb = _env_int(
        "MAX_TOTAL_BLOCK_VOLUME_GB", 200, minimum=boot_volume_size_gb
    )
    if max_total_block_volume_gb > 200:
        raise ConfigError(
            "MAX_TOTAL_BLOCK_VOLUME_GB must not exceed the Always Free 200 GB limit"
        )

    settings = Settings(
        oci_config_path=oci_config_path,
        compartment_id=compartment_id,
        tenancy_id=tenancy_id,
        micro_ad=micro_ad,
        subnet_id=subnet_id,
        image_id=image_id,
        operating_system=_env("OPERATING_SYSTEM", "Canonical Ubuntu"),
        os_version=_env("OS_VERSION", "22.04"),
        micro_count=2,
        name_prefix=_env("MICRO_NAME_PREFIX", "micro").strip("-"),
        request_wait_secs=request_wait_secs,
        max_backoff_secs=max_backoff_secs,
        max_runtime_secs=_env_int("MAX_RUNTIME_HOURS", 6, minimum=1) * 3600,
        max_launch_attempts=_env_int("MAX_LAUNCH_ATTEMPTS", 180, minimum=1),
        max_rate_limit_failures=_env_int(
            "MAX_RATE_LIMIT_FAILURES", 5, minimum=1
        ),
        reconcile_grace_secs=reconcile_grace_secs,
        boot_volume_size_gb=boot_volume_size_gb,
        max_total_block_volume_gb=max_total_block_volume_gb,
        assign_public_ip=_env_bool("ASSIGN_PUBLIC_IP", True),
        ssh_authorized_keys_file=_expand_path(
            _env("MICRO_SSH_PUBLIC_KEY", "~/.ssh/id_rsa.pub")
        ),
        state_file=_expand_path(_env("MICRO_STATE_FILE", ".micro_hunter_state.json")),
        log_file=_expand_path(_env("MICRO_LOG_FILE", "micro_hunter.log")),
        discord_webhook=_env("DISCORD_WEBHOOK"),
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,47}", settings.name_prefix):
        raise ConfigError("MICRO_NAME_PREFIX must contain 1 to 48 safe characters")
    return settings


def _response_data(response: Any) -> Any:
    return response.data if hasattr(response, "data") else response


def _page_items(client: Any, method_name: str, **kwargs: Any) -> list[Any]:
    response = getattr(client, method_name)(**kwargs)
    items = list(_response_data(response))
    while bool(getattr(response, "has_next_page", False)):
        response = getattr(client, method_name)(**kwargs, page=response.next_page)
        items.extend(_response_data(response))
    return items


def _exception_fields(exc: BaseException) -> tuple[int | None, str, str]:
    status = getattr(exc, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    return status, code, message


def classify_service_error(exc: BaseException) -> str:
    status, code, message = _exception_fields(exc)
    normalized = message.lower()
    if code == "TooManyRequests" or status == 429:
        return "rate_limited"
    if any(text in normalized for text in CAPACITY_MESSAGES):
        return "capacity"
    if status in {500, 502, 503, 504}:
        return "ambiguous"
    if code in {"LimitExceeded", "QuotaExceeded", "NotAuthenticated",
                "NotAuthorizedOrNotFound", "InvalidParameter"}:
        return "fatal"
    if code == "InternalError":
        return "ambiguous"
    name = type(exc).__name__
    if name.endswith(
        ("RequestException", "ConnectTimeout", "ReadTimeout", "Timeout",
         "ConnectionError", "TimeoutError")
    ) or any(
        text in normalized
        for text in (
            "read timed out",
            "connect timeout",
            "connection reset",
            "connection aborted",
            "max retries exceeded",
            "proxyerror",
            "tunnel connection failed",
        )
    ):
        return "ambiguous"
    return "unexpected"


@dataclass(frozen=True)
class Inventory:
    instances: tuple[Any, ...]
    micros: tuple[Any, ...]
    running_micros: tuple[Any, ...]
    in_flight_micros: tuple[Any, ...]
    stopped_micros: tuple[Any, ...]


@dataclass(frozen=True)
class StepResult:
    kind: str
    message: str
    wait_secs: float = 0


@dataclass(frozen=True)
class PendingLaunch:
    display_name: str
    opc_retry_token: str
    created_at: float


class MicroHunter:
    def __init__(
        self,
        settings: Settings,
        compute_client: Any,
        blockstorage_client: Any,
        network_client: Any,
        identity_client: Any,
        models: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.compute = compute_client
        self.blockstorage = blockstorage_client
        self.network = network_client
        self.identity = identity_client
        self.models = models
        self.sleep = sleep
        self.clock = clock
        self.monotonic = monotonic
        self.rng = rng or random.SystemRandom()
        self.availability_domain = ""
        self.image_id = ""
        self.subnet: Any = None
        self.compartment_ids: list[str] = []
        self.transient_failures = 0
        self.rate_limit_failures = 0
        self.launch_attempts = 0

    def prepare(self) -> None:
        self._verify_home_region()
        self.compartment_ids = self._resolve_compartments()
        self.availability_domain = self._resolve_ad()
        self.subnet = self._resolve_subnet()
        self.image_id = self._resolve_image()
        self._ensure_ssh_key()
        logging.info("Availability domain: %s", self.availability_domain)
        logging.info("Subnet: %s", self.settings.subnet_id)
        logging.info("Image: %s", self.image_id)

    def _resolve_compartments(self) -> list[str]:
        compartments = _page_items(
            self.identity,
            "list_compartments",
            compartment_id=self.settings.tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        )
        resource_compartments = [self.settings.tenancy_id]
        resource_compartments.extend(
            item.id
            for item in compartments
            if getattr(item, "lifecycle_state", "ACTIVE") == "ACTIVE"
        )
        if self.settings.compartment_id not in resource_compartments:
            resource_compartments.append(self.settings.compartment_id)
        logging.info(
            "Checking resource limits across %d compartment(s)",
            len(resource_compartments),
        )
        return list(dict.fromkeys(resource_compartments))

    def _verify_home_region(self) -> None:
        subscriptions = _page_items(
            self.identity,
            "list_region_subscriptions",
            tenancy_id=self.settings.tenancy_id,
        )
        homes = [item for item in subscriptions if bool(getattr(item, "is_home_region", False))]
        if not homes:
            raise FatalServiceError("OCI did not report a home region for this tenancy")
        configured_region = str(getattr(self.identity, "region", "") or "")
        home_region = str(getattr(homes[0], "region_name", "") or "")
        if configured_region and home_region and configured_region != home_region:
            raise ConfigError(
                f"OCI config region is {configured_region}, but Always Free must launch "
                f"in home region {home_region}"
            )

    def _resolve_ad(self) -> str:
        ads = _page_items(
            self.identity,
            "list_availability_domains",
            compartment_id=self.settings.tenancy_id,
        )
        wanted = self.settings.micro_ad
        matches = [
            item.name
            for item in ads
            if item.name == wanted or item.name.endswith(f"-{wanted.lstrip('-')}")
        ]
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1:
            raise ConfigError(
                f"MICRO_AD {wanted!r} matched {len(matches)} availability domains; "
                "set the full AD name so the choice is unambiguous"
            )
        return matches[0]

    def _resolve_subnet(self) -> Any:
        subnet = _response_data(self.network.get_subnet(self.settings.subnet_id))
        if subnet.lifecycle_state != "AVAILABLE":
            raise ConfigError(f"Subnet is {subnet.lifecycle_state}, not AVAILABLE")
        if getattr(subnet, "availability_domain", None):
            raise ConfigError(
                "OCI_SUBNET_ID must be a regional subnet; AD-specific subnets cannot "
                "launch in the selected Micro AD"
            )
        if self.settings.assign_public_ip and bool(
            getattr(subnet, "prohibit_public_ip_on_vnic", False)
        ):
            raise ConfigError(
                "The subnet prohibits public IPs; set ASSIGN_PUBLIC_IP=false or use "
                "another subnet"
            )
        return subnet

    def _resolve_image(self) -> str:
        if self.settings.image_id:
            image = _response_data(self.compute.get_image(self.settings.image_id))
            if image.lifecycle_state != "AVAILABLE":
                raise ConfigError(f"Image is {image.lifecycle_state}, not AVAILABLE")
            compatible = getattr(image, "compatible_shapes", None)
            if compatible and MICRO_SHAPE not in compatible:
                raise ConfigError(f"Image is not compatible with {MICRO_SHAPE}")
            return image.id

        images = _page_items(
            self.compute,
            "list_images",
            compartment_id=self.settings.compartment_id,
            shape=MICRO_SHAPE,
        )
        candidates = [
            image
            for image in images
            if image.lifecycle_state == "AVAILABLE"
            and image.operating_system == self.settings.operating_system
            and image.operating_system_version == self.settings.os_version
        ]
        if not candidates:
            raise ConfigError(
                "No available image matches OPERATING_SYSTEM/OS_VERSION "
                f"({self.settings.operating_system} {self.settings.os_version})"
            )
        candidates.sort(key=lambda image: image.time_created, reverse=True)
        return candidates[0].id

    def _ensure_ssh_key(self) -> None:
        public_key_path = self.settings.ssh_authorized_keys_file
        private_key_path = public_key_path.with_name(f"{public_key_path.stem}_private")
        if public_key_path.exists():
            _secure_file(private_key_path)
            return
        try:
            import paramiko
        except ImportError as exc:
            raise ConfigError(
                "paramiko is required to generate an SSH key; run ./setup_init.sh"
            ) from exc
        public_key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = paramiko.RSAKey.generate(3072)
        previous_umask = os.umask(0o077)
        try:
            key.write_private_key_file(str(private_key_path))
            private_key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            public_key_path.write_text(
                f"ssh-rsa {key.get_base64()} {public_key_path.stem}_auto_generated\n",
                encoding="ascii",
            )
            public_key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        finally:
            os.umask(previous_umask)
        logging.info("Generated SSH key pair at %s and %s", public_key_path, private_key_path)

    def _ssh_public_key(self) -> str:
        path = self.settings.ssh_authorized_keys_file
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ConfigError(f"Cannot read SSH public key {path}: {exc}") from exc
        if not value.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-")):
            raise ConfigError(f"{path} does not look like an SSH public key")
        return value

    def inventory(self) -> Inventory:
        instances_list: list[Any] = []
        seen_instance_ids: set[str] = set()
        for compartment_id in self.compartment_ids:
            compartment_instances = _page_items(
                self.compute,
                "list_instances",
                compartment_id=compartment_id,
            )
            for item in compartment_instances:
                resource_id = str(getattr(item, "id", "") or "")
                if resource_id and resource_id in seen_instance_ids:
                    continue
                if resource_id:
                    seen_instance_ids.add(resource_id)
                instances_list.append(item)
        instances = tuple(instances_list)
        micros = tuple(
            instance
            for instance in instances
            if instance.shape == MICRO_SHAPE
            and instance.lifecycle_state not in TERMINAL_STATES
        )
        running = tuple(
            instance for instance in micros if instance.lifecycle_state == "RUNNING"
        )
        in_flight = tuple(
            instance
            for instance in micros
            if instance.lifecycle_state in IN_FLIGHT_STATES
        )
        stopped = tuple(
            instance for instance in micros if instance.lifecycle_state == "STOPPED"
        )
        return Inventory(instances, micros, running, in_flight, stopped)

    def _block_volume_usage_gb(self) -> float:
        resource_volumes: list[Any] = []
        seen_volume_ids: set[str] = set()
        for compartment_id in self.compartment_ids:
            compartment_boot_volumes = _page_items(
                self.blockstorage,
                "list_boot_volumes",
                compartment_id=compartment_id,
            )
            compartment_volumes = _page_items(
                self.blockstorage,
                "list_volumes",
                compartment_id=compartment_id,
            )
            for volume in (*compartment_boot_volumes, *compartment_volumes):
                resource_id = str(getattr(volume, "id", "") or "")
                if resource_id and resource_id in seen_volume_ids:
                    continue
                if resource_id:
                    seen_volume_ids.add(resource_id)
                resource_volumes.append(volume)
        total = 0.0
        for volume in resource_volumes:
            if getattr(volume, "lifecycle_state", "") in TERMINAL_STATES:
                continue
            total += float(getattr(volume, "size_in_gbs", 0) or 0)
        return total

    def _ensure_storage_budget(self) -> None:
        used = self._block_volume_usage_gb()
        requested = self.settings.boot_volume_size_gb
        limit = self.settings.max_total_block_volume_gb
        if used + requested > limit:
            raise FatalServiceError(
                f"Boot volume preflight failed: {used:g} GB in use + {requested} GB "
                f"requested exceeds the {limit} GB allowance"
            )
        logging.info("Block volume preflight: %.0f/%d GB in use", used, limit)

    def _load_pending(self) -> PendingLaunch | None:
        path = self.settings.state_file
        if not path.exists():
            return None
        _secure_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pending = PendingLaunch(
                display_name=str(payload["display_name"]),
                opc_retry_token=str(payload["opc_retry_token"]),
                created_at=float(payload["created_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid state file {path}: {exc}") from exc
        uuid.UUID(pending.opc_retry_token)
        return pending

    def _write_pending(self, pending: PendingLaunch) -> None:
        path = self.settings.state_file
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": 1,
            "display_name": pending.display_name,
            "opc_retry_token": pending.opc_retry_token,
            "created_at": pending.created_at,
            "availability_domain": self.availability_domain,
            "subnet_id": self.settings.subnet_id,
            "image_id": self.image_id,
        }
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(payload, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, path)
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                raise

    def _clear_pending(self) -> None:
        self.settings.state_file.unlink(missing_ok=True)

    def _pending_exists(self, inventory: Inventory, pending: PendingLaunch) -> bool:
        return any(
            instance.display_name == pending.display_name for instance in inventory.micros
        )

    def _choose_name(self, inventory: Inventory) -> str | None:
        used_names = {instance.display_name for instance in inventory.micros}
        for name in self.settings.desired_names():
            if name not in used_names:
                return name
        return None

    def _launch_details(self, display_name: str) -> Any:
        create_vnic = self.models.CreateVnicDetails(
            assign_public_ip=self.settings.assign_public_ip,
            assign_private_dns_record=True,
            display_name=display_name,
            subnet_id=self.settings.subnet_id,
        )
        source_details = self.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=self.image_id,
            boot_volume_size_in_gbs=self.settings.boot_volume_size_gb,
        )
        instance_options = self.models.InstanceOptions(
            are_legacy_imds_endpoints_disabled=True
        )
        return self.models.LaunchInstanceDetails(
            availability_domain=self.availability_domain,
            compartment_id=self.settings.compartment_id,
            create_vnic_details=create_vnic,
            display_name=display_name,
            shape=MICRO_SHAPE,
            instance_options=instance_options,
            source_details=source_details,
            metadata={"ssh_authorized_keys": self._ssh_public_key()},
        )

    def _launch(
        self, display_name: str, pending: PendingLaunch | None = None
    ) -> StepResult:
        self._ensure_storage_budget()
        if pending is None:
            pending = PendingLaunch(
                display_name=display_name,
                opc_retry_token=str(uuid.uuid4()),
                created_at=self.clock(),
            )
            self._write_pending(pending)
        self.launch_attempts += 1
        logging.info(
            "Launch attempt %d for %s (retry token %s)",
            self.launch_attempts,
            display_name,
            pending.opc_retry_token,
        )
        try:
            response = self.compute.launch_instance(
                launch_instance_details=self._launch_details(display_name),
                opc_retry_token=pending.opc_retry_token,
            )
        except Exception as exc:
            category = classify_service_error(exc)
            if category == "capacity":
                self._clear_pending()
                self.transient_failures = 0
                return StepResult(
                    "wait",
                    "No host capacity; waiting before the next launch",
                    self._capacity_wait(),
                )
            if category == "rate_limited":
                self._clear_pending()
                self.rate_limit_failures += 1
                if self.rate_limit_failures > self.settings.max_rate_limit_failures:
                    raise FatalServiceError(
                        "OCI kept returning TooManyRequests; stopping to avoid account risk"
                    ) from exc
                wait_secs = min(
                    self.settings.max_backoff_secs,
                    self.settings.request_wait_secs
                    * (2 ** (self.rate_limit_failures - 1)),
                )
                return StepResult(
                    "wait", f"OCI rate-limited the launch ({wait_secs}s backoff)", wait_secs
                )
            if category == "transient":
                self._clear_pending()
                self.transient_failures += 1
                wait_secs = min(
                    self.settings.max_backoff_secs,
                    self.settings.request_wait_secs
                    * (2 ** min(self.transient_failures - 1, 4)),
                )
                return StepResult(
                    "wait", f"Transient OCI error ({wait_secs}s backoff)", wait_secs
                )
            if category == "ambiguous":
                return StepResult(
                    "wait",
                    "Launch result is ambiguous; reconciling by instance name before retry",
                    self.settings.reconcile_grace_secs,
                )
            self._clear_pending()
            status, code, message = _exception_fields(exc)
            raise FatalServiceError(
                f"Launch failed and must not be retried: status={status}, "
                f"code={code or '-'}, message={message}"
            ) from exc

        status = getattr(response, "status", 200)
        if status not in {200, 201, 202}:
            self._clear_pending()
            raise FatalServiceError(f"Unexpected LaunchInstance HTTP status: {status}")
        self.rate_limit_failures = 0
        self.transient_failures = 0
        logging.info("OCI accepted launch for %s (HTTP %s)", display_name, status)
        return StepResult(
            "wait",
            f"Launch accepted for {display_name}; waiting for RUNNING",
            self.settings.request_wait_secs,
        )

    def _capacity_wait(self) -> float:
        jitter = self.rng.uniform(0.9, 1.2)
        return max(60.0, self.settings.request_wait_secs * jitter)

    def step(self) -> StepResult:
        inventory = self.inventory()
        running_names = {instance.display_name for instance in inventory.running_micros}
        desired_running = all(
            name in running_names for name in self.settings.desired_names()
        )
        if len(inventory.running_micros) >= self.settings.micro_count and (
            desired_running or len(inventory.running_micros) >= self.settings.micro_count
        ):
            self._clear_pending()
            return StepResult(
                "success",
                f"{len(inventory.running_micros)} Always Free Micro instance(s) are RUNNING",
            )
        if inventory.stopped_micros:
            names = ", ".join(item.display_name for item in inventory.stopped_micros)
            raise FatalServiceError(
                f"Existing Micro instance(s) are STOPPED: {names}. Start them in OCI "
                "Console instead of launching replacements."
            )
        if inventory.in_flight_micros:
            names = ", ".join(item.display_name for item in inventory.in_flight_micros)
            return StepResult(
                "wait",
                f"Micro instance(s) already in flight: {names}",
                self.settings.request_wait_secs,
            )
        if len(inventory.micros) >= self.settings.micro_count:
            names = ", ".join(item.display_name for item in inventory.micros)
            raise FatalServiceError(
                f"{len(inventory.micros)} Micro instance(s) already exist but are not "
                f"RUNNING: {names}. Inspect them instead of creating duplicates."
            )

        pending = self._load_pending()
        if pending:
            if self._pending_exists(inventory, pending):
                logging.info("Reconciled ambiguous launch %s", pending.display_name)
                self._clear_pending()
                return StepResult(
                    "wait",
                    f"Found {pending.display_name}; waiting for it to reach RUNNING",
                    self.settings.request_wait_secs,
                )
            age = max(0.0, self.clock() - pending.created_at)
            if age < self.settings.reconcile_grace_secs:
                remaining = self.settings.reconcile_grace_secs - age
                return StepResult(
                    "wait",
                    f"Reconciling pending launch {pending.display_name} ({remaining:.0f}s left)",
                    remaining,
                )
            logging.warning(
                "Pending launch %s was not found after %ss; retrying with the same token",
                pending.display_name,
                self.settings.reconcile_grace_secs,
            )
            return self._launch(pending.display_name, pending)

        name = self._choose_name(inventory)
        if not name:
            raise FatalServiceError(
                "All configured Micro display names are already used; refusing to guess"
            )
        return self._launch(name)

    def run(self) -> None:
        self.prepare()
        deadline = self.monotonic() + self.settings.max_runtime_secs
        while True:
            result = self.step()
            logging.info("%s: %s", result.kind, result.message)
            if result.kind == "success":
                return
            if self.monotonic() >= deadline:
                raise FatalServiceError(
                    "MAX_RUNTIME_HOURS reached; rerun later instead of polling continuously"
                )
            if self.launch_attempts >= self.settings.max_launch_attempts:
                raise FatalServiceError(
                    "MAX_LAUNCH_ATTEMPTS reached; rerun later instead of polling continuously"
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                continue
            self.sleep(min(result.wait_secs, remaining))


def configure_logging(path: Path, verbose: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ]
    _secure_file(path)
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def notify(webhook: str, message: str) -> None:
    if not webhook:
        return
    import urllib.request

    request = urllib.request.Request(
        webhook,
        data=json.dumps({"content": message[:1900]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as exc:  # Notification failures must not change provisioning state.
        logging.warning("Discord notification failed: %s", exc)


def build_clients(settings: Settings) -> tuple[Any, Any, Any, Any, Any]:
    if oci is None:
        raise ConfigError("oci is required; run ./setup_init.sh")
    config = oci.config.from_file(str(settings.oci_config_path))
    compute = oci.core.ComputeClient(config)
    blockstorage = oci.core.BlockstorageClient(config)
    network = oci.core.VirtualNetworkClient(config)
    identity = oci.identity.IdentityClient(config)
    return compute, blockstorage, network, identity, oci.core.models


def print_plan(settings: Settings, hunter: MicroHunter) -> None:
    hunter.prepare()
    inventory = hunter.inventory()
    used = hunter._block_volume_usage_gb()
    print(f"Home-region AD: {hunter.availability_domain}")
    print(f"Subnet: {settings.subnet_id}")
    print(f"Image: {hunter.image_id}")
    print(f"Existing non-terminal Micro instances: {len(inventory.micros)}")
    for instance in inventory.micros:
        print(
            f"  - {instance.display_name}: {instance.lifecycle_state} "
            f"({instance.availability_domain})"
        )
    print(f"Block volume usage: {used:g}/{settings.max_total_block_volume_gb} GB")
    print(
        f"Next safe action: prepare {settings.boot_volume_size_gb} GB boot volume for "
        f"{hunter._choose_name(inventory) or 'no available slot'}"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely acquire up to two Always Free E2.1.Micro instances"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and show the next action without launching",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="also write logs to stderr"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings: Settings | None = None
    try:
        settings = load_settings()
        configure_logging(settings.log_file, verbose=args.verbose)
        compute, blockstorage, network, identity, models = build_clients(settings)
        hunter = MicroHunter(
            settings, compute, blockstorage, network, identity, models
        )
        if args.dry_run:
            print_plan(settings, hunter)
            return 0
        hunter.run()
        logging.info("Micro hunter completed successfully")
        notify(
            settings.discord_webhook,
            f"OCI Micro hunter finished at {datetime.now(timezone.utc).isoformat()}",
        )
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        return 130
    except HunterError as exc:
        logging.error("%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        if settings:
            notify(settings.discord_webhook, f"OCI Micro hunter stopped: {exc}")
        return 2


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    raise SystemExit(main())
