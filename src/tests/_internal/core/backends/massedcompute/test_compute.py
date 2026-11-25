from datetime import datetime, timezone

import pytest
import requests

from dstack._internal.core.backends.massedcompute.api_client import API_URL
from dstack._internal.core.backends.massedcompute.compute import MassedComputeCompute
from dstack._internal.core.backends.massedcompute.models import (
    MassedComputeConfig,
    MassedComputeCreds,
)
from dstack._internal.core.errors import ComputeError, NotYetTerminated, ProvisioningError
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.backends.orphans import OwnerType
from dstack._internal.server.testing.common import (
    get_instance_configuration,
    get_instance_offer_with_availability,
    get_job_provisioning_data,
)

VM_ID = "465e97cc-77cc-47dd-bb7c-1835ee127f9f"


@pytest.fixture
def compute() -> MassedComputeCompute:
    return MassedComputeCompute(MassedComputeConfig(creds=MassedComputeCreds(api_key="test")))


class TestCreateInstance:
    def test_reconciles_the_same_launch_after_a_lost_reply(
        self, compute: MassedComputeCompute, requests_mock
    ):
        launch = requests_mock.post(f"{API_URL}/instance/launch", exc=requests.ReadTimeout)
        data = compute.create_instance(
            get_instance_offer_with_availability(backend=BackendType.MASSEDCOMPUTE),
            get_instance_configuration(),
            None,
        )
        name = launch.last_request.json()["instanceName"]
        assert data.instance_id == name
        vm = {
            "uuid": VM_ID,
            "name": name,
            "status": "Running",
            "os_booted": True,
            "ip": "192.0.2.1",
            "username": "Ubuntu",
        }
        requests_mock.get(
            f"{API_URL}/instance",
            [
                {"status_code": 503},
                {"json": {"runningInstances": [{**vm, "name": name + "-other"}]}},
                {
                    "json": {
                        "runningInstances": [
                            vm,
                            {**vm, "uuid": "1a821471-d1d7-4c27-971d-10d3951ccce7"},
                        ]
                    }
                },
                {"json": {"runningInstances": [vm]}},
            ],
        )
        requests_mock.get(f"{API_URL}/instance/{VM_ID}", json={"runningInstances": [vm]})
        with pytest.raises(ComputeError, match="503"):
            compute.update_provisioning_data(data, "", "")
        compute.update_provisioning_data(data, "", "")
        assert data.instance_id == name and data.hostname is None
        with pytest.raises(ComputeError, match="Multiple"):
            compute.update_provisioning_data(data, "", "")
        assert data.instance_id == name and data.hostname is None
        compute.update_provisioning_data(data, "", "")
        assert data.instance_id == VM_ID and data.hostname == vm["ip"]
        assert data.backend_data is None
        assert launch.call_count == 1


class TestUpdateProvisioningData:
    @pytest.mark.parametrize(("attached", "hidden_count"), [(False, 0), (True, 1), (True, 0)])
    def test_requires_exclusive_attachment_before_exposing_ssh(
        self, compute: MassedComputeCompute, requests_mock, attached, hidden_count
    ):
        jpd = get_job_provisioning_data(hostname=None)
        jpd.instance_id = VM_ID
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
                        "attachedTo": [{"uuid": jpd.instance_id.upper()}] if attached else [],
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
    @pytest.mark.parametrize("lost_reply", [False, True])
    def test_waits_until_new_vm_has_been_seen_by_uuid(
        self, compute: MassedComputeCompute, requests_mock, lost_reply
    ):
        reply = {"exc": requests.ReadTimeout} if lost_reply else {"json": {"response": VM_ID}}
        launch = requests_mock.post(f"{API_URL}/instance/launch", **reply)
        data = compute.create_instance(
            get_instance_offer_with_availability(backend=BackendType.MASSEDCOMPUTE),
            get_instance_configuration(),
            None,
        )
        vm = {
            "uuid": VM_ID,
            "name": launch.last_request.json()["instanceName"],
            "status": "Running",
        }
        if lost_reply:
            requests_mock.get(f"{API_URL}/instance", json={"runningInstances": [vm]})
        requests_mock.get(
            f"{API_URL}/instance/{VM_ID}",
            [
                {"json": {"runningInstances": []}},
                {"json": {"runningInstances": [vm]}},
            ],
        )
        with pytest.raises(ComputeError, match="has not returned VM details"):
            compute.terminate_instance(data.instance_id, "any", data.backend_data)
        with pytest.raises(NotYetTerminated) as found:
            compute.terminate_instance(data.instance_id, "any", data.backend_data)
        assert found.value.instance_id == VM_ID
        assert found.value.backend_data is None

    def test_waits_for_disappearance_and_accepts_repeated_calls(
        self, compute: MassedComputeCompute, requests_mock
    ):
        vm = {"uuid": VM_ID, "name": "vm", "status": "Stopped"}
        requests_mock.get(
            f"{API_URL}/instance/{VM_ID}",
            [
                {"json": {"runningInstances": [vm]}},
                {"json": {"runningInstances": []}},
            ],
        )
        terminate = requests_mock.post(f"{API_URL}/instance/terminate", status_code=202)

        # A stopped VM on MassedCompute is still billable.
        with pytest.raises(NotYetTerminated):
            compute.terminate_instance(VM_ID, "any")
        compute.terminate_instance(VM_ID, "any")
        compute.terminate_instance(VM_ID, "any")

        assert terminate.call_count == 1
        assert terminate.last_request.json() == {"instanceUuids": [VM_ID]}


class TestListResourceGroups:
    def test_lists_only_instances_with_a_provider_ref(
        self, compute: MassedComputeCompute, requests_mock
    ):
        vm = {
            "uuid": VM_ID,
            "name": "dstack-main-gpu-abcdefgh12345678",
            "status": "Running",
            "created": "2026-11-25T14:48:48.000Z",
        }
        requests_mock.get(
            f"{API_URL}/instance",
            json={"runningInstances": [vm, {**vm, "name": "manual"}]},
        )

        (group,) = compute.list_resource_groups().resource_groups

        assert group.owner_type == OwnerType.INSTANCE
        assert group.provider_ref == "abcdefgh1234"
        assert group.primary_id == VM_ID
        assert group.primary_name == vm["name"]
        assert group.created_at == datetime(2026, 11, 25, 14, 48, 48, tzinfo=timezone.utc)
