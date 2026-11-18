from __future__ import annotations

import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import micro_hunter


class FakeModel:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeModels:
    CreateVnicDetails = FakeModel
    InstanceSourceViaImageDetails = FakeModel
    InstanceOptions = FakeModel
    LaunchInstanceDetails = FakeModel


class FakeRequestException(Exception):
    pass


class FakeServiceError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def instance(name, state="RUNNING", shape=micro_hunter.MICRO_SHAPE, ad="AD-1"):
    return SimpleNamespace(
        id=f"ocid1.instance.oc1..{name}",
        display_name=name,
        lifecycle_state=state,
        shape=shape,
        availability_domain=ad,
    )


class FakeCompute:
    def __init__(self):
        self.instances = []
        self.launch_calls = []
        self.launch_error = None
        self.image = None

    def list_instances(self, **_kwargs):
        return self.instances

    def launch_instance(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.launch_error:
            raise self.launch_error
        return SimpleNamespace(status=200)

    def get_image(self, _image_id):
        return self.image


class FakeBlockStorage:
    def __init__(self):
        self.boot_volumes = []
        self.volumes = []

    def list_boot_volumes(self, **_kwargs):
        return self.boot_volumes

    def list_volumes(self, **_kwargs):
        return self.volumes


class FakeNetwork:
    def __init__(self):
        self.subnet = SimpleNamespace(
            lifecycle_state="AVAILABLE",
            availability_domain=None,
            prohibit_public_ip_on_vnic=False,
        )

    def get_subnet(self, _subnet_id):
        return self.subnet


class FakeIdentity:
    def __init__(self):
        self.region = "us-phoenix-1"

    def list_region_subscriptions(self, **_kwargs):
        return [
            SimpleNamespace(
                is_home_region=True,
                region_name="us-phoenix-1",
            )
        ]

    def list_availability_domains(self, **_kwargs):
        return [
            SimpleNamespace(name="Uocm:PHX-AD-1"),
            SimpleNamespace(name="Uocm:PHX-AD-2"),
        ]

    def list_compartments(self, **_kwargs):
        return []


class MicroHunterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        ssh_key = root / "id_rsa.pub"
        ssh_key.write_text("ssh-rsa AAAAB3NzaC1yc2E test\n", encoding="ascii")
        self.settings = micro_hunter.Settings(
            oci_config_path=root / "config",
            compartment_id="ocid1.compartment.oc1..example",
            tenancy_id="ocid1.tenancy.oc1..example",
            micro_ad="PHX-AD-1",
            subnet_id="ocid1.subnet.oc1.phx.example",
            image_id=None,
            operating_system="Canonical Ubuntu",
            os_version="22.04",
            micro_count=2,
            name_prefix="micro",
            request_wait_secs=120,
            max_backoff_secs=900,
            max_runtime_secs=3600,
            max_launch_attempts=10,
            max_rate_limit_failures=2,
            reconcile_grace_secs=180,
            boot_volume_size_gb=50,
            max_total_block_volume_gb=200,
            assign_public_ip=True,
            ssh_authorized_keys_file=ssh_key,
            state_file=root / ".micro_hunter_state.json",
            log_file=root / "micro_hunter.log",
            discord_webhook="",
        )
        self.now = [1000.0]
        self.compute = FakeCompute()
        self.blockstorage = FakeBlockStorage()
        self.network = FakeNetwork()
        self.identity = FakeIdentity()
        self.hunter = micro_hunter.MicroHunter(
            self.settings,
            self.compute,
            self.blockstorage,
            self.network,
            self.identity,
            FakeModels,
            clock=lambda: self.now[0],
            rng=SimpleNamespace(uniform=lambda _a, _b: 1.0),
        )
        self.hunter.availability_domain = "Uocm:PHX-AD-1"
        self.hunter.image_id = "ocid1.image.oc1.phx.example"
        self.hunter.compartment_ids = [
            self.settings.tenancy_id,
            self.settings.compartment_id,
        ]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_existing_arm_does_not_block_micro_launch(self):
        self.compute.instances = [
            instance("arm-main", shape="VM.Standard.A1.Flex")
        ]
        result = self.hunter.step()
        self.assertEqual(result.kind, "wait")
        self.assertEqual(len(self.compute.launch_calls), 1)
        details = self.compute.launch_calls[0]["launch_instance_details"]
        self.assertEqual(details.display_name, "micro-1")
        self.assertEqual(details.shape, micro_hunter.MICRO_SHAPE)

    def test_existing_provisioning_micro_is_not_launched_again(self):
        self.compute.instances = [instance("micro-1", state="PROVISIONING")]
        result = self.hunter.step()
        self.assertEqual(result.kind, "wait")
        self.assertEqual(self.compute.launch_calls, [])

    def test_timeout_reconciles_by_name_without_duplicate_launch(self):
        self.compute.launch_error = FakeRequestException("read timed out")
        first = self.hunter.step()
        self.assertEqual(first.kind, "wait")
        self.assertTrue(self.settings.state_file.exists())

        self.compute.instances = [instance("micro-1", state="PROVISIONING")]
        second = self.hunter.step()
        self.assertEqual(second.kind, "wait")
        self.assertEqual(len(self.compute.launch_calls), 1)

    def test_timeout_retry_reuses_opc_retry_token(self):
        self.compute.launch_error = FakeRequestException("connection reset")
        self.hunter.step()
        first_token = self.compute.launch_calls[0]["opc_retry_token"]
        self.now[0] += self.settings.reconcile_grace_secs + 1
        self.hunter.step()
        second_token = self.compute.launch_calls[1]["opc_retry_token"]
        self.assertEqual(first_token, second_token)

    def test_limit_exceeded_is_fatal(self):
        self.compute.launch_error = FakeServiceError(
            400, "LimitExceeded", "The following service limits were exceeded"
        )
        with self.assertRaises(micro_hunter.FatalServiceError):
            self.hunter.step()
        self.assertFalse(self.settings.state_file.exists())

    def test_rate_limit_uses_exponential_backoff_then_stops(self):
        self.compute.launch_error = FakeServiceError(
            429, "TooManyRequests", "Too many requests"
        )
        waits = []
        for _ in range(2):
            result = self.hunter.step()
            waits.append(result.wait_secs)
        self.assertEqual(waits, [120, 240])
        with self.assertRaises(micro_hunter.FatalServiceError):
            self.hunter.step()

    def test_one_running_micro_continues_with_second(self):
        self.compute.instances = [instance("micro-1")]
        result = self.hunter.step()
        self.assertEqual(result.kind, "wait")
        self.assertEqual(
            self.compute.launch_calls[0]["launch_instance_details"].display_name,
            "micro-2",
        )

    def test_two_running_micros_finish(self):
        self.compute.instances = [instance("micro-1"), instance("micro-2")]
        result = self.hunter.step()
        self.assertEqual(result.kind, "success")
        self.assertEqual(self.compute.launch_calls, [])

    def test_micro_launch_details_omit_shape_config(self):
        self.hunter.step()
        details = self.compute.launch_calls[0]["launch_instance_details"]
        self.assertFalse(hasattr(details, "shape_config"))
        self.assertTrue(details.instance_options.are_legacy_imds_endpoints_disabled)

    def test_non_micro_shape_configuration_is_rejected(self):
        with self.assertRaises(micro_hunter.ConfigError):
            micro_hunter.validate_configured_shape("VM.Standard.A1.Flex")

    def test_micro_shape_configuration_is_accepted(self):
        micro_hunter.validate_configured_shape(micro_hunter.MICRO_SHAPE)

    def test_non_linux_free_tier_os_is_rejected(self):
        with self.assertRaises(micro_hunter.ConfigError):
            micro_hunter.validate_free_micro_os("Windows")

    def test_free_tier_linux_os_is_accepted(self):
        micro_hunter.validate_free_micro_os("Canonical Ubuntu")
        micro_hunter.validate_free_micro_os("Oracle Linux")

    def test_marketplace_image_is_rejected(self):
        self.compute.image = SimpleNamespace(
            id="ocid1.image.oc1.phx.marketplace",
            lifecycle_state="AVAILABLE",
            compatible_shapes=[micro_hunter.MICRO_SHAPE],
            operating_system="Canonical Ubuntu",
            listing_id="ocid1.marketplacelisting.oc1.phx.example",
            listing_resource_version="2026.01",
        )
        hunter = micro_hunter.MicroHunter(
            replace(self.settings, image_id=self.compute.image.id),
            self.compute,
            self.blockstorage,
            self.network,
            self.identity,
            FakeModels,
        )
        with self.assertRaises(micro_hunter.ConfigError):
            hunter._resolve_image()

    def test_windows_image_is_rejected(self):
        self.compute.image = SimpleNamespace(
            id="ocid1.image.oc1.phx.windows",
            lifecycle_state="AVAILABLE",
            compatible_shapes=[micro_hunter.MICRO_SHAPE],
            operating_system="Windows",
            listing_id=None,
            listing_resource_version=None,
        )
        hunter = micro_hunter.MicroHunter(
            replace(self.settings, image_id=self.compute.image.id),
            self.compute,
            self.blockstorage,
            self.network,
            self.identity,
            FakeModels,
        )
        with self.assertRaises(micro_hunter.ConfigError):
            hunter._resolve_image()

    def test_block_volume_limit_above_always_free_is_rejected_at_launch(self):
        hunter = micro_hunter.MicroHunter(
            replace(self.settings, max_total_block_volume_gb=201),
            self.compute,
            self.blockstorage,
            self.network,
            self.identity,
            FakeModels,
        )
        hunter.availability_domain = "Uocm:PHX-AD-1"
        hunter.image_id = "ocid1.image.oc1.phx.example"
        with self.assertRaises(micro_hunter.FatalServiceError):
            hunter._launch_details("micro-1")

    def test_storage_preflight_fails_before_launch(self):
        self.blockstorage.boot_volumes = [
            SimpleNamespace(size_in_gbs=180, lifecycle_state="AVAILABLE")
        ]
        with self.assertRaises(micro_hunter.FatalServiceError):
            self.hunter.step()
        self.assertEqual(self.compute.launch_calls, [])

    def test_state_file_is_owner_only(self):
        self.hunter.step()
        mode = stat.S_IMODE(self.settings.state_file.stat().st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_wait_time_below_safety_floor_is_rejected(self):
        with patch.dict("os.environ", {"REQUEST_WAIT_TIME_SECS": "0"}):
            with self.assertRaises(micro_hunter.ConfigError):
                micro_hunter._env_int("REQUEST_WAIT_TIME_SECS", 120, minimum=60)


if __name__ == "__main__":
    unittest.main()
