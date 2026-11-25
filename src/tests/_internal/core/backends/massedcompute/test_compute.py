from datetime import datetime, timezone

import pytest

from dstack._internal.core.backends.massedcompute.api_client import API_URL
from dstack._internal.core.backends.massedcompute.compute import MassedComputeCompute
from dstack._internal.core.backends.massedcompute.models import (
    MassedComputeConfig,
    MassedComputeCreds,
)
from dstack._internal.core.errors import NotYetTerminated, ProvisioningError
from dstack._internal.core.models.backends.orphans import OwnerType
from dstack._internal.server.testing.common import get_job_provisioning_data


class TestUpdateProvisioningData:
    @pytest.mark.parametrize(("attached", "hidden_count"), [(False, 0), (True, 1), (True, 0)])
    def test_requires_exclusive_attachment_before_exposing_ssh(
        self, requests_mock, attached, hidden_count
    ):
        compute = MassedComputeCompute(
            MassedComputeConfig(creds=MassedComputeCreds(api_key="test"))
        )
        jpd = get_job_provisioning_data(hostname=None)
        volume_id = "78ee0ea9-4c18-4aa4-b0df-aa1212734d45"
        jpd.volume_mounts = {volume_id: "/persistent/checkpoints"}
        vm = {
            "uuid": jpd.instance_id,
            "name": "worker",
            "status": "Running",
            "os_booted": True,
            "username": "Ubuntu",
            "ip": "192.0.2.1",
        }
        requests_mock.get(f"{API_URL}/instance/{jpd.instance_id}", json={"runningInstances": [vm]})
        requests_mock.get(
            f"{API_URL}/storage-volumes",
            json={
                "storageVolumes": [
                    {
                        "id": volume_id,
                        "name": "checkpoints",
                        "status": "active",
                        "canManage": True,
                        "mountPath": "/persistent/checkpoints",
                        "hiddenAttachedCount": hidden_count,
                        "attachedTo": [{"uuid": jpd.instance_id}] if attached else [],
                    }
                ]
            },
        )
        if attached and hidden_count == 0:
            compute.update_provisioning_data(jpd, "", "")
            assert jpd.hostname == "192.0.2.1"
        else:
            with pytest.raises(ProvisioningError, match="failed to attach"):
                compute.update_provisioning_data(jpd, "", "")
            assert jpd.hostname is None


class TestTerminateInstance:
    def test_waits_for_disappearance_and_accepts_repeated_calls(self, requests_mock):
        compute = MassedComputeCompute(
            MassedComputeConfig(creds=MassedComputeCreds(api_key="test"))
        )
        vm = {"uuid": "vm-id", "name": "vm", "status": "Stopped"}
        requests_mock.get(
            f"{API_URL}/instance/vm-id",
            [
                {"json": {"runningInstances": [vm]}},
                {"json": {"runningInstances": []}},
            ],
        )
        terminate = requests_mock.post(f"{API_URL}/instance/terminate", status_code=202)

        # A stopped VM on MassedCompute is still billable.
        with pytest.raises(NotYetTerminated):
            compute.terminate_instance("vm-id", "any")
        compute.terminate_instance("vm-id", "any")
        compute.terminate_instance("vm-id", "any")

        assert terminate.call_count == 1
        assert terminate.last_request.json() == {"instanceUuids": ["vm-id"]}


class TestListResourceGroups:
    def test_lists_only_instances_with_a_provider_ref(self, requests_mock):
        compute = MassedComputeCompute(
            MassedComputeConfig(creds=MassedComputeCreds(api_key="test"))
        )
        vm = {
            "uuid": "vm-id",
            "name": "dstack-main-gpu-abcdefgh12345678",
            "status": "Running",
            "created": "2026-11-25T14:48:48.000Z",
        }
        requests_mock.get(
            f"{API_URL}/instance",
            json={"runningInstances": [vm, {**vm, "name": "manual", "uuid": "another-vm"}]},
        )

        (group,) = compute.list_resource_groups().resource_groups

        assert group.owner_type == OwnerType.INSTANCE
        assert group.provider_ref == "abcdefgh1234"
        assert group.primary_id == "vm-id"
        assert group.created_at == datetime(2026, 11, 25, 14, 48, 48, tzinfo=timezone.utc)
