"""Test application module."""

import asyncio
import logging

import pytest
import zigpy.config
import zigpy.device
from zigpy.types import EUI64
import zigpy.zdo.types as zdo_t

from zigpy_deconz import types as t
import zigpy_deconz.api as deconz_api
import zigpy_deconz.exception
import zigpy_deconz.zigbee.application as application

from .async_mock import AsyncMock, MagicMock, patch, sentinel

ZIGPY_NWK_CONFIG = {
    zigpy.config.CONF_NWK: {
        zigpy.config.CONF_NWK_PAN_ID: 0x4567,
        zigpy.config.CONF_NWK_EXTENDED_PAN_ID: "11:22:33:44:55:66:77:88",
        zigpy.config.CONF_NWK_UPDATE_ID: 22,
        zigpy.config.CONF_NWK_KEY: [0xAA] * 16,
    },
}


@pytest.fixture
def device_path():
    return "/dev/null"


@pytest.fixture
def api():
    """Return API fixture."""
    api = MagicMock(spec_set=zigpy_deconz.api.Deconz(None, None))
    api.device_state = AsyncMock(
        return_value=(deconz_api.DeviceState(deconz_api.NetworkState.CONNECTED), 0, 0)
    )
    api.write_parameter = AsyncMock()

    # So the protocol version is effectively infinite
    api._proto_ver.__ge__.return_value = True
    api._proto_ver.__lt__.return_value = False

    api.protocol_version.__ge__.return_value = True
    api.protocol_version.__lt__.return_value = False

    return api


@pytest.fixture
def app(device_path, api):
    config = application.ControllerApplication.SCHEMA(
        {
            **ZIGPY_NWK_CONFIG,
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: device_path},
        }
    )

    app = application.ControllerApplication(config)

    api.change_network_state = AsyncMock()

    device_state = MagicMock()
    device_state.network_state.__eq__.return_value = True
    api.device_state = AsyncMock(return_value=(device_state, 0, 0))

    p1 = patch.object(app, "_api", api)
    p2 = patch.object(app, "_delayed_neighbour_scan")
    p3 = patch.object(app, "_change_network_state", wraps=app._change_network_state)

    with p1, p2, p3:
        yield app


@pytest.fixture
def ieee():
    return EUI64.deserialize(b"\x00\x01\x02\x03\x04\x05\x06\x07")[0]


@pytest.fixture
def nwk():
    return t.uint16_t(0x0100)


@pytest.fixture
def addr_ieee(ieee):
    addr = t.DeconzAddress()
    addr.address_mode = t.AddressMode.IEEE
    addr.address = ieee
    return addr


@pytest.fixture
def addr_nwk(nwk):
    addr = t.DeconzAddress()
    addr.address_mode = t.AddressMode.NWK
    addr.address = nwk
    return addr


@pytest.fixture
def addr_nwk_and_ieee(nwk, ieee):
    addr = t.DeconzAddress()
    addr.address_mode = t.AddressMode.NWK_AND_IEEE
    addr.address = nwk
    addr.ieee = ieee
    return addr


@pytest.mark.parametrize(
    "proto_ver, nwk_state, error",
    [
        (0x0107, deconz_api.NetworkState.CONNECTED, None),
        (0x0106, deconz_api.NetworkState.CONNECTED, None),
        (0x0107, deconz_api.NetworkState.OFFLINE, None),
        (0x0107, deconz_api.NetworkState.OFFLINE, asyncio.TimeoutError()),
    ],
)
async def test_start_network(app, proto_ver, nwk_state, error):
    app.load_network_info = AsyncMock()
    app.restore_neighbours = AsyncMock()
    app.add_endpoint = AsyncMock()
    app._change_network_state = AsyncMock(side_effect=error)

    app._api.device_state = AsyncMock(
        return_value=(deconz_api.DeviceState(nwk_state), 0, 0)
    )
    app._api._proto_ver = proto_ver
    app._api.protocol_version = proto_ver

    if nwk_state != deconz_api.NetworkState.CONNECTED and error is not None:
        with pytest.raises(zigpy.exceptions.FormationFailure):
            await app.start_network()

        return

    with patch.object(application.DeconzDevice, "initialize", AsyncMock()):
        await app.start_network()
        assert app.load_network_info.await_count == 1
        assert app._change_network_state.await_count == 1

        assert (
            app._change_network_state.await_args_list[0][0][0]
            == deconz_api.NetworkState.CONNECTED
        )

        if proto_ver >= application.PROTO_VER_NEIGBOURS:
            assert app.restore_neighbours.await_count == 1
        else:
            assert app.restore_neighbours.await_count == 0


async def test_permit(app, nwk):
    app._api.write_parameter = AsyncMock()
    time_s = 30
    await app.permit_ncp(time_s)
    assert app._api.write_parameter.call_count == 1
    assert app._api.write_parameter.call_args_list[0][0][1] == time_s


async def test_connect(app):
    def new_api(*args):
        api = MagicMock()
        api.connect = AsyncMock()
        api.version = AsyncMock(return_value=sentinel.version)

        return api

    with patch.object(application, "Deconz", new=new_api):
        app._api = None
        await app.connect()
        assert app._api is not None

        assert app._api.connect.await_count == 1
        assert app._api.version.await_count == 1
        assert app.version is sentinel.version


async def test_connect_failure(app):
    with patch.object(application, "Deconz") as api_mock:
        api = api_mock.return_value = MagicMock()
        api.connect = AsyncMock()
        api.version = AsyncMock(side_effect=RuntimeError("Broken"))

        app._api = None

        with pytest.raises(RuntimeError):
            await app.connect()

        assert app._api is None
        api.connect.assert_called_once()
        api.version.assert_called_once()
        api.close.assert_called_once()


async def test_disconnect(app):
    reset_watchdog_task = app._reset_watchdog_task = MagicMock()
    api_close = app._api.close = MagicMock()

    await app.disconnect()

    assert app._api is None
    assert app._reset_watchdog_task is None

    assert api_close.call_count == 1
    assert reset_watchdog_task.cancel.call_count == 1


async def test_disconnect_no_api(app):
    app._api = None
    await app.disconnect()


async def test_disconnect_close_error(app):
    app._api.write_parameter = MagicMock(
        side_effect=zigpy_deconz.exception.CommandError(1, "Error")
    )
    await app.disconnect()


async def test_permit_with_key_not_implemented(app):
    with pytest.raises(NotImplementedError):
        await app.permit_with_key(node=MagicMock(), code=b"abcdef")


async def test_deconz_dev_add_to_group(app, nwk, device_path):
    group = MagicMock()
    app._groups = MagicMock()
    app._groups.add_group.return_value = group

    deconz = application.DeconzDevice(0, device_path, app, sentinel.ieee, nwk)
    deconz.endpoints = {
        0: sentinel.zdo,
        1: sentinel.ep1,
        2: sentinel.ep2,
    }

    await deconz.add_to_group(sentinel.grp_id, sentinel.grp_name)
    assert group.add_member.call_count == 2

    assert app.groups.add_group.call_count == 1
    assert app.groups.add_group.call_args[0][0] is sentinel.grp_id
    assert app.groups.add_group.call_args[0][1] is sentinel.grp_name


async def test_deconz_dev_remove_from_group(app, nwk, device_path):
    group = MagicMock()
    app.groups[sentinel.grp_id] = group
    deconz = application.DeconzDevice(0, device_path, app, sentinel.ieee, nwk)
    deconz.endpoints = {
        0: sentinel.zdo,
        1: sentinel.ep1,
        2: sentinel.ep2,
    }

    await deconz.remove_from_group(sentinel.grp_id)
    assert group.remove_member.call_count == 2


def test_deconz_props(nwk, device_path):
    deconz = application.DeconzDevice(0, device_path, app, sentinel.ieee, nwk)
    assert deconz.manufacturer is not None
    assert deconz.model is not None


@pytest.mark.parametrize(
    "name, firmware_version, device_path",
    [
        ("ConBee", 0x00000500, "/dev/ttyUSB0"),
        ("ConBee II", 0x00000700, "/dev/ttyUSB0"),
        ("RaspBee", 0x00000500, "/dev/ttyS0"),
        ("RaspBee II", 0x00000700, "/dev/ttyS0"),
        ("RaspBee", 0x00000500, "/dev/ttyAMA0"),
        ("RaspBee II", 0x00000700, "/dev/ttyAMA0"),
    ],
)
def test_deconz_name(nwk, name, firmware_version, device_path):
    deconz = application.DeconzDevice(
        firmware_version, device_path, app, sentinel.ieee, nwk
    )
    assert deconz.model == name


async def test_deconz_new(app, nwk, device_path, monkeypatch):
    mock_init = AsyncMock()
    monkeypatch.setattr(zigpy.device.Device, "_initialize", mock_init)

    deconz = await application.DeconzDevice.new(app, sentinel.ieee, nwk, 0, device_path)
    assert isinstance(deconz, application.DeconzDevice)
    assert mock_init.call_count == 1
    mock_init.reset_mock()

    mock_dev = MagicMock()
    mock_dev.endpoints = {
        0: MagicMock(),
        1: MagicMock(),
        22: MagicMock(),
    }
    app.devices[sentinel.ieee] = mock_dev
    deconz = await application.DeconzDevice.new(app, sentinel.ieee, nwk, 0, device_path)
    assert isinstance(deconz, application.DeconzDevice)
    assert mock_init.call_count == 0


def test_tx_confirm_success(app):
    tsn = 123
    req = app._pending[tsn] = MagicMock()
    app.handle_tx_confirm(tsn, sentinel.status)
    assert req.result.set_result.call_count == 1
    assert req.result.set_result.call_args[0][0] is sentinel.status


def test_tx_confirm_dup(app, caplog):
    caplog.set_level(logging.DEBUG)
    tsn = 123
    req = app._pending[tsn] = MagicMock()
    req.result.set_result.side_effect = asyncio.InvalidStateError
    app.handle_tx_confirm(tsn, sentinel.status)
    assert req.result.set_result.call_count == 1
    assert req.result.set_result.call_args[0][0] is sentinel.status
    assert any(r.levelname == "DEBUG" for r in caplog.records)
    assert "probably duplicate response" in caplog.text


def test_tx_confirm_unexpcted(app, caplog):
    app.handle_tx_confirm(123, 0x00)
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "Unexpected transmit confirm for request id" in caplog.text


async def test_reset_watchdog(app):
    """Test watchdog."""
    with patch.object(app._api, "write_parameter") as mock_api:
        dog = asyncio.create_task(app._reset_watchdog())
        await asyncio.sleep(0.3)
        dog.cancel()
        assert mock_api.call_count == 1

    with patch.object(app._api, "write_parameter") as mock_api:
        mock_api.side_effect = zigpy_deconz.exception.CommandError
        dog = asyncio.create_task(app._reset_watchdog())
        await asyncio.sleep(0.3)
        dog.cancel()
        assert mock_api.call_count == 1


async def test_force_remove(app):
    """Test forcibly removing a device."""
    await app.force_remove(sentinel.device)


async def test_restore_neighbours(app):
    """Test neighbour restoration."""

    # FFD, Rx on when idle
    device_1 = app.add_device(nwk=0x0001, ieee=EUI64.convert("00:00:00:00:00:00:00:01"))
    device_1.node_desc = zdo_t.NodeDescriptor(1, 64, 142, 0xBEEF, 82, 82, 0, 82, 0)

    # RFD, Rx on when idle
    device_2 = app.add_device(nwk=0x0002, ieee=EUI64.convert("00:00:00:00:00:00:00:02"))
    device_2.node_desc = zdo_t.NodeDescriptor(1, 64, 142, 0xBEEF, 82, 82, 0, 82, 0)

    device_3 = app.add_device(nwk=0x0003, ieee=EUI64.convert("00:00:00:00:00:00:00:03"))
    device_3.node_desc = None

    # RFD, Rx off when idle
    device_5 = app.add_device(nwk=0x0005, ieee=EUI64.convert("00:00:00:00:00:00:00:05"))
    device_5.node_desc = zdo_t.NodeDescriptor(2, 64, 128, 0xBEEF, 82, 82, 0, 82, 0)

    coord = MagicMock()
    coord.ieee = EUI64.convert("aa:aa:aa:aa:aa:aa:aa:aa")

    app.devices[coord.ieee] = coord
    app.state.node_info.ieee = coord.ieee

    app.topology.neighbors[coord.ieee] = [
        zdo_t.Neighbor(ieee=device_1.ieee),
        zdo_t.Neighbor(ieee=device_2.ieee),
        zdo_t.Neighbor(ieee=device_3.ieee),
        zdo_t.Neighbor(ieee=EUI64.convert("00:00:00:00:00:00:00:04")),
        zdo_t.Neighbor(ieee=device_5.ieee),
    ]

    p = patch.object(app, "_api", spec_set=zigpy_deconz.api.Deconz(None, None))

    with p as api_mock:
        api_mock.add_neighbour = AsyncMock()
        await app.restore_neighbours()

    assert api_mock.add_neighbour.call_count == 1
    assert api_mock.add_neighbour.await_count == 1


@patch("zigpy_deconz.zigbee.application.DELAY_NEIGHBOUR_SCAN_S", 0)
async def test_delayed_scan():
    """Delayed scan."""

    coord = MagicMock()
    config = application.ControllerApplication.SCHEMA(
        {
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: "usb0"},
            zigpy.config.CONF_DATABASE: "tmp",
        }
    )

    app = application.ControllerApplication(config)
    with patch.object(app, "get_device", return_value=coord):
        with patch.object(app, "topology", AsyncMock()):
            await app._delayed_neighbour_scan()
            app.topology.scan.assert_called_once_with(devices=[coord])


@patch("zigpy_deconz.zigbee.application.CHANGE_NETWORK_WAIT", 0.001)
@pytest.mark.parametrize("support_watchdog", [False, True])
async def test_change_network_state(app, support_watchdog):
    app._reset_watchdog_task = MagicMock()

    app._api.device_state = AsyncMock(
        side_effect=[
            (deconz_api.DeviceState(deconz_api.NetworkState.OFFLINE), 0, 0),
            (deconz_api.DeviceState(deconz_api.NetworkState.JOINING), 0, 0),
            (deconz_api.DeviceState(deconz_api.NetworkState.CONNECTED), 0, 0),
        ]
    )

    if support_watchdog:
        app._api._proto_ver = application.PROTO_VER_WATCHDOG
        app._api.protocol_version = application.PROTO_VER_WATCHDOG
    else:
        app._api._proto_ver = application.PROTO_VER_WATCHDOG - 1
        app._api.protocol_version = application.PROTO_VER_WATCHDOG - 1

    old_watchdog_task = app._reset_watchdog_task
    cancel_mock = app._reset_watchdog_task.cancel = MagicMock()

    await app._change_network_state(deconz_api.NetworkState.CONNECTED, timeout=0.01)

    if support_watchdog:
        assert cancel_mock.call_count == 1
        assert app._reset_watchdog_task is not old_watchdog_task
    else:
        assert cancel_mock.call_count == 0
        assert app._reset_watchdog_task is old_watchdog_task


ENDPOINT = zdo_t.SimpleDescriptor(
    endpoint=None,
    profile=1,
    device_type=2,
    device_version=3,
    input_clusters=[4],
    output_clusters=[5],
)


@pytest.mark.parametrize(
    "descriptor, slots, target_slot",
    [
        (ENDPOINT.replace(endpoint=1), {0: ENDPOINT.replace(endpoint=2)}, 0),
        # Prefer the endpoint with the same ID
        (
            ENDPOINT.replace(endpoint=1),
            {
                0: ENDPOINT.replace(endpoint=2, profile=1234),
                1: ENDPOINT.replace(endpoint=1, profile=1234),
            },
            1,
        ),
    ],
)
async def test_add_endpoint(app, descriptor, slots, target_slot):
    async def read_param(param_id, index):
        assert param_id == deconz_api.NetworkParameter.configure_endpoint

        if index not in slots:
            raise zigpy_deconz.exception.CommandError(
                deconz_api.Status.UNSUPPORTED, "Unsupported"
            )
        else:
            return index, slots[index]

    app._api.read_parameter = AsyncMock(side_effect=read_param)
    app._api.write_parameter = AsyncMock()

    await app.add_endpoint(descriptor)
    app._api.write_parameter.assert_called_once_with(
        deconz_api.NetworkParameter.configure_endpoint, target_slot, descriptor
    )


async def test_add_endpoint_no_free_space(app):
    async def read_param(param_id, index):
        assert param_id == deconz_api.NetworkParameter.configure_endpoint
        assert index in (0x00, 0x01)

        raise zigpy_deconz.exception.CommandError(
            deconz_api.Status.UNSUPPORTED, "Unsupported"
        )

    app._api.read_parameter = AsyncMock(side_effect=read_param)
    app._api.write_parameter = AsyncMock()
    app._written_endpoints.add(0x00)
    app._written_endpoints.add(0x01)

    with pytest.raises(ValueError):
        await app.add_endpoint(ENDPOINT.replace(endpoint=1))

    app._api.write_parameter.assert_not_called()


async def test_add_endpoint_no_unnecessary_writes(app):
    async def read_param(param_id, index):
        assert param_id == deconz_api.NetworkParameter.configure_endpoint

        if index > 0x01:
            raise zigpy_deconz.exception.CommandError(
                deconz_api.Status.UNSUPPORTED, "Unsupported"
            )

        return index, ENDPOINT.replace(endpoint=1)

    app._api.read_parameter = AsyncMock(side_effect=read_param)
    app._api.write_parameter = AsyncMock()

    await app.add_endpoint(ENDPOINT.replace(endpoint=1))
    app._api.write_parameter.assert_not_called()

    # Writing another endpoint will cause a write
    await app.add_endpoint(ENDPOINT.replace(endpoint=2))
    app._api.write_parameter.assert_called_once_with(
        deconz_api.NetworkParameter.configure_endpoint, 1, ENDPOINT.replace(endpoint=2)
    )


@patch("zigpy_deconz.zigbee.application.asyncio.sleep", new_callable=AsyncMock)
@patch(
    "zigpy_deconz.zigbee.application.ControllerApplication.initialize",
    side_effect=[RuntimeError(), None],
)
@patch(
    "zigpy_deconz.zigbee.application.ControllerApplication.connect",
    side_effect=[RuntimeError(), None, None],
)
async def test_reconnect(mock_connect, mock_initialize, mock_sleep, app):
    assert app._reconnect_task is None
    app.connection_lost(RuntimeError())

    assert app._reconnect_task is not None
    await app._reconnect_task

    assert mock_connect.call_count == 3
    assert mock_initialize.call_count == 2


async def test_disconnect_during_reconnect(app):
    assert app._reconnect_task is None
    app.connection_lost(RuntimeError())
    await asyncio.sleep(0)
    await app.disconnect()

    assert app._reconnect_task is None


async def test_reset_network_info(app):
    app.form_network = AsyncMock()
    await app.reset_network_info()

    app.form_network.assert_called_once()
