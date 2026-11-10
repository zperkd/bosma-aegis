"""Run real Home Assistant lifecycle/config-flow tests with all Bosma I/O mocked."""
from unittest.mock import patch
import pytest


DEVICE = {'devicePid':'0123456789abcdef0123456789abcdef','deviceId':'000000ABCDEF',
          'name':'Example Lock','modelCode':'010601','remotePid':'synthetic-gateway'}
DATA = {'account':'ha@example.invalid','password':'synthetic-password','imei':'synthetic-imei',
        'devices':[{k:v for k,v in DEVICE.items() if k!='remotePid'}]}

@pytest.fixture
def mock_api():
    with patch('custom_components.bosma_aegis.BosmaApi', autospec=True) as setup_cls, \
         patch('custom_components.bosma_aegis.config_flow.BosmaApi', autospec=True) as flow_cls, \
         patch('custom_components.bosma_aegis.coordinator.BosmaCoordinator.start'):
        api=setup_cls.return_value
        flow_cls.return_value=api
        api.async_login.return_value='synthetic-token'
        api.async_discover_locks.return_value=[DEVICE.copy()]
        api.async_get_shadow.return_value={'lockStatus':1,'doorStatus':1,'battery':88}
        api.async_get_events.return_value=[]
        yield api
