"""Opt-in alarm delivery, replay suppression, fresh door timers and options in real HA."""
import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch
import pytest
from homeassistant.exceptions import HomeAssistantError
from custom_components.bosma_aegis.api import BosmaConnectionError
from custom_components.bosma_aegis.const import DOMAIN
from .conftest import DEVICE
from .test_integration import setup

pytestmark=pytest.mark.asyncio
PID=DEVICE['devicePid']

@pytest.fixture
def notifications():
    with patch('custom_components.bosma_aegis.alerts.persistent_notification.async_create') as create, \
         patch('custom_components.bosma_aegis.alerts.persistent_notification.async_dismiss') as dismiss:
        yield create,dismiss

async def make(hass,mock_api,options=None):
    entry=await setup(hass,options)
    return entry,hass.data[DOMAIN][entry.entry_id].alerts

async def flush(hass):await hass.async_block_till_done(wait_background_tasks=True)

async def test_alerts_default_off(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api)
    assert not alerts.break_in and not alerts.door_ajar
    mock_api.async_get_events.assert_not_awaited()
    notifications[0].assert_not_called()

async def test_startup_alarms_not_replayed(hass,mock_api,notifications):
    mock_api.async_get_events.return_value=[{'actType':64,'timestamp':time.time()-1}]
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    await flush(hass)
    notifications[0].assert_not_called()
    mock_api.async_get_events.return_value=[{'actType':64,'timestamp':time.time()+1}]
    await alerts.async_reconcile();await flush(hass)
    notifications[0].assert_called_once()
    assert 'impact' in notifications[0].call_args.args[1]

async def test_alarm_without_lock_transition_and_no_duplicates(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    mock_api.async_get_events.return_value=[{'actType':'64','timestamp':time.time()+1}]
    alerts.push(PID);alerts.push(PID);await flush(hass)
    await alerts.async_reconcile();await flush(hass)
    notifications[0].assert_called_once()
    assert hass.states.get('lock.example_lock').state=='locked'

async def test_impact_cooldown_and_no_jam_false_alarm(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    for act in (65,66,67,1,129):
        alerts._process_events(PID,[{'actType':act,'timestamp':time.time()+1}])
    await flush(hass);notifications[0].assert_not_called()
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+2}]);await flush(hass)
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+3}]);await flush(hass)
    assert notifications[0].call_count==1
    alerts._last_sent[(PID,'break_in')]-=61
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+4}]);await flush(hass)
    assert notifications[0].call_count==2

async def test_old_foreign_and_invalid_alarm_rows_ignored(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()-500},
        {'actType':64,'timestamp':time.time()+1,'devicePid':'another-lock'},
        {'actType':64,'timestamp':'nan'},{'actType':64,'timestamp':True},None])
    await flush(hass);notifications[0].assert_not_called()

async def test_phone_only_delivery(hass,mock_api,notifications):
    phone=AsyncMock();hass.services.async_register('notify','mobile_app_test',phone)
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True,'alert_persistent':False,'alert_notify_service':'notify.mobile_app_test'})
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+1}]);await flush(hass)
    phone.assert_awaited_once();notifications[0].assert_not_called()
    assert 'Potential break-in' in phone.call_args.args[0].data['title']

async def test_ha_and_phone_delivery(hass,mock_api,notifications):
    phone=AsyncMock();hass.services.async_register('notify','mobile_app_test',phone)
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True,'alert_persistent':True,'alert_notify_service':'notify.mobile_app_test'})
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+1}]);await flush(hass)
    phone.assert_awaited_once();notifications[0].assert_called_once()

async def test_phone_failure_keeps_ha_notification_and_redacts(hass,mock_api,notifications,caplog):
    phone=AsyncMock(side_effect=HomeAssistantError('SECRET_PHONE_DATA'))
    hass.services.async_register('notify','mobile_app_test',phone)
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True,'alert_notify_service':'notify.mobile_app_test'})
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()+1}]);await flush(hass)
    notifications[0].assert_called_once()
    assert 'SECRET_PHONE_DATA' not in caplog.text

async def test_door_timer_not_reset_and_requires_fresh_open(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True,'alert_ajar_delay':30})
    with patch('custom_components.bosma_aegis.alerts.async_call_later',return_value=Mock()) as timer:
        alerts.observe(PID,0,time.time());alerts.observe(PID,0,time.time())
        timer.assert_called_once();assert timer.call_args.args[1]==30
        mock_api.async_get_shadow.return_value={'doorStatus':0,'lockStatus':1}
        await alerts._async_confirm_ajar(PID);await flush(hass)
    notifications[0].assert_called_once()
    await alerts._async_confirm_ajar(PID)
    assert notifications[0].call_count==1
    alerts.observe(PID,1,time.time());assert PID not in alerts._ajar_sent
    assert notifications[1].called

async def test_closed_during_timer_confirmation_has_no_alert(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    alerts.observe(PID,0,time.time())
    mock_api.async_get_shadow.return_value={'doorStatus':1,'lockStatus':1}
    await alerts._async_confirm_ajar(PID);await flush(hass)
    notifications[0].assert_not_called();assert PID not in alerts._timers

async def test_unavailable_door_confirmation_has_no_false_alarm(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    alerts.observe(PID,0,time.time())
    mock_api.async_get_shadow.side_effect=BosmaConnectionError('synthetic')
    await alerts._async_confirm_ajar(PID);await flush(hass)
    notifications[0].assert_not_called()

async def test_newer_closed_push_wins_over_slow_open_response(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    alerts.observe(PID,0,time.time())
    async def shadow(_device):
        alerts.observe(PID,1,time.time()+.01)
        return {'doorStatus':0,'lockStatus':1}
    mock_api.async_get_shadow.side_effect=shadow
    await alerts._async_confirm_ajar(PID);await flush(hass)
    notifications[0].assert_not_called()

async def test_device_ajar_deduplicates_timer_and_clears_when_closed(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    alerts.observe(PID,0,time.time())
    alerts._process_events(PID,[{'actType':68,'timestamp':time.time()+1}]);await flush(hass)
    await alerts._async_confirm_ajar(PID);await flush(hass)
    notifications[0].assert_called_once()
    alerts.observe(PID,1,time.time());assert notifications[1].called

async def test_unload_cancels_timers_and_suppresses_late_alerts(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    alerts.observe(PID,0,time.time());assert alerts._timers
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert alerts._closed and not alerts._timers
    await alerts._async_emit(PID,'break_in','synthetic',source='test')
    notifications[0].assert_not_called()

async def test_options_destinations_and_preserve_existing_control(hass,mock_api):
    entry,alerts=await make(hass,mock_api,{'control_mode':'read_only'})
    flow=await hass.config_entries.options.async_init(entry.entry_id)
    form=await hass.config_entries.options.async_configure(flow['flow_id'],{'next_step_id':'alerts'})
    assert form['step_id']=='alerts'
    values={'alert_break_in':True,'alert_door_ajar':True,'alert_ajar_delay':60,'alert_persistent':False,'alert_notify_service':''}
    result=await hass.config_entries.options.async_configure(form['flow_id'],values)
    assert result['errors']=={'base':'no_alert_destination'}
    values['alert_persistent']=True
    result=await hass.config_entries.options.async_configure(form['flow_id'],values)
    assert result['type']=='create_entry'
    await flush(hass)
    assert entry.options['control_mode']=='read_only' and entry.options['alert_break_in']


async def test_alarm_timestamp_skew_is_tolerated(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    alerts._process_events(PID,[{'actType':64,'timestamp':time.time()-25}]);await flush(hass)
    notifications[0].assert_called_once()

async def test_websocket_alarm_without_shadow_transition(hass,mock_api,notifications):
    import json
    entry,alerts=await make(hass,mock_api,{'alert_break_in':True})
    mock_api.async_get_events.return_value=[{'actType':64,'timestamp':time.time()+1}]
    coordinator=hass.data[DOMAIN][entry.entry_id]
    coordinator._ws_message(None,json.dumps({'action':'notification/data','respData':{'data':{
        'devicePid':PID,'notifyType':'ALARM_NOTIFICATION','notifyData':{}}}}))
    await flush(hass)
    notifications[0].assert_called_once()
    assert hass.states.get('lock.example_lock').state=='locked'

async def test_partial_door_push_starts_timer(hass,mock_api,notifications):
    entry,alerts=await make(hass,mock_api,{'alert_door_ajar':True})
    coordinator=hass.data[DOMAIN][entry.entry_id]
    coordinator._apply(PID,{'doorStatus':0},source='push',observed=time.time())
    assert alerts._doors[PID]=='open' and PID in alerts._timers
    notifications[0].assert_not_called()
