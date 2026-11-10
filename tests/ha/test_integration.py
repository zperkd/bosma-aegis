"""Native entity setup/control, options migration, verification and unloading in real HA."""
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant.common import MockConfigEntry
from custom_components.bosma_aegis.api import BosmaAuthError, BosmaConnectionError, BosmaVerificationRequired
from custom_components.bosma_aegis.const import DOMAIN
from .conftest import DATA, DEVICE

pytestmark=pytest.mark.asyncio

async def setup(hass, options=None):
    entry=MockConfigEntry(domain=DOMAIN,data=DATA.copy(),options=options or {},unique_id=DATA['account'])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry

async def test_upgrade_native_entities_and_unload(hass,mock_api):
    entry=await setup(hass)
    assert hass.states.get('lock.example_lock').state=='locked'
    assert hass.states.get('sensor.example_lock_battery').state=='88'
    assert hass.data[DOMAIN][entry.entry_id].devices[0]['remotePid']=='synthetic-gateway'
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    state=hass.states.get('lock.example_lock')
    assert state is None or (state.state=='unavailable' and state.attributes.get('restored'))
    assert entry.entry_id not in hass.data[DOMAIN]

async def test_native_unlock_and_relock_confirmed(hass,mock_api):
    await setup(hass)
    async def control(device,*,unlock):
        assert device['remotePid']=='synthetic-gateway'
        mock_api.async_get_shadow.return_value={'lockStatus':0 if unlock else 1,'doorStatus':1,'battery':88}
    mock_api.async_control.side_effect=control
    for action,expected in [('unlock','unlocked'),('lock','locked')]:
        await hass.services.async_call('lock',action,{'entity_id':'lock.example_lock'},blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get('lock.example_lock').state==expected
    assert mock_api.async_control.await_count==2

async def test_read_only_retains_sensors(hass,mock_api):
    await setup(hass,{'control_mode':'read_only'})
    state=hass.states.get('lock.example_lock')
    assert state is None or (state.state=='unavailable' and state.attributes.get('restored'))
    assert hass.states.get('sensor.example_lock_status').state=='locked'

async def test_native_default_preserves_legacy_settings(hass,mock_api):
    entry=await setup(hass,{'ifttt_key':'synthetic-legacy-key','control_events':{DEVICE['devicePid']:{'lock':'lock_event','unlock':'unlock_event'}}})
    assert hass.states.get('lock.example_lock') is not None
    assert entry.options['ifttt_key']=='synthetic-legacy-key'
    assert hass.data[DOMAIN][entry.entry_id].api is mock_api

async def test_control_options_switch_to_read_only(hass,mock_api):
    entry=await setup(hass)
    flow=await hass.config_entries.options.async_init(entry.entry_id)
    assert flow['type']=='menu'
    form=await hass.config_entries.options.async_configure(flow['flow_id'],{'next_step_id':'control'})
    assert form['step_id']=='control'
    result=await hass.config_entries.options.async_configure(form['flow_id'],{'control_mode':'read_only'})
    assert result['type']=='create_entry'
    await hass.async_block_till_done()
    assert entry.options['control_mode']=='read_only'
    state=hass.states.get('lock.example_lock')
    assert state is None or (state.state=='unavailable' and state.attributes.get('restored'))

async def test_legacy_options_have_separate_form(hass,mock_api):
    entry=await setup(hass)
    flow=await hass.config_entries.options.async_init(entry.entry_id)
    form=await hass.config_entries.options.async_configure(flow['flow_id'],{'next_step_id':'control'})
    result=await hass.config_entries.options.async_configure(form['flow_id'],{'control_mode':'ifttt'})
    assert result['step_id']=='ifttt'

async def test_user_setup_discovers_locks(hass,mock_api):
    flow=await hass.config_entries.flow.async_init(DOMAIN,context={'source':SOURCE_USER})
    assert flow['step_id']=='user'
    result=await hass.config_entries.flow.async_configure(flow['flow_id'],{'account':DATA['account'],'password':DATA['password']})
    assert result['type']=='create_entry'
    assert result['data']['devices'][0]['remotePid']=='synthetic-gateway'
    await hass.async_block_till_done()

async def test_email_verification(hass,mock_api):
    mock_api.async_login.side_effect=[BosmaVerificationRequired(), 'synthetic-token']
    flow=await hass.config_entries.flow.async_init(DOMAIN,context={'source':SOURCE_USER})
    result=await hass.config_entries.flow.async_configure(flow['flow_id'],{'account':DATA['account'],'password':DATA['password']})
    assert result['step_id']=='verify'
    mock_api.async_send_verify_code.assert_awaited_once()
    result=await hass.config_entries.flow.async_configure(result['flow_id'],{'verification_code':'123456'})
    assert result['type']=='create_entry'
    await hass.async_block_till_done()

@pytest.mark.parametrize(('error','expected'),[(BosmaAuthError(),'invalid_auth'),(BosmaConnectionError(),'cannot_connect')])
async def test_setup_errors(hass,mock_api,error,expected):
    mock_api.async_login.side_effect=error
    flow=await hass.config_entries.flow.async_init(DOMAIN,context={'source':SOURCE_USER})
    result=await hass.config_entries.flow.async_configure(flow['flow_id'],{'account':DATA['account'],'password':DATA['password']})
    assert result['errors']=={'base':expected}
