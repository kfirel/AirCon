import aiohttp
import base64
from getmac import get_mac_address
from http import HTTPStatus
import json
import logging
import ssl
import sys

from .app_mappings import *

_USER_AGENT = 'Dalvik/2.1.0 (Linux; U; Android 9.0; SM-G850F Build/LRX22G)'


async def _sign_in(user: str, passwd: str, user_server: str, app_id: str, app_secret: str,
                   session: aiohttp.ClientSession, ssl_context: ssl.SSLContext):
  query = {
      'user': {
          'email': user,
          'password': passwd,
          'application': {
              'app_id': app_id,
              'app_secret': app_secret
          }
      }
  }
  headers = {
      'Accept': 'application/json',
      'Connection': 'Keep-Alive',
      'Authorization': 'none',
      'Content-Type': 'application/json',
      'User-Agent': _USER_AGENT,
      'Host': user_server,
      'Accept-Encoding': 'gzip'
  }
  logging.debug('POST /users/sign_in.json, body=%r, headers=%r', json.dumps(query), headers)
  async with session.request('POST',
                             f'https://{user_server}/users/sign_in.json',
                             json=query,
                             headers=headers,
                             ssl=ssl_context) as resp:
    if resp.status != HTTPStatus.OK.value:
      logging.error('Failed to login to Hisense server:\nStatus %d: %r', resp.status, resp.reason)
      sys.exit(1)
    resp_data = await resp.text()
    try:
      tokens = json.loads(resp_data)
    except UnicodeDecodeError:
      logging.exception('Failed to parse login tokens to Hisense server:\nData: %r', resp_data)
      sys.exit(1)
    return tokens['access_token']


async def _get_devices(devices_server: str, access_token: str, headers: dict,
                       session: aiohttp.ClientSession, ssl_context: ssl.SSLContext):
  logging.debug('GET /apiv1/devices.json, headers=%r', headers)
  async with session.get(f'https://{devices_server}/apiv1/devices.json',
                         headers=headers,
                         ssl=ssl_context) as resp:
    if resp.status != HTTPStatus.OK.value:
      logging.error('Failed to get devices data from Hisense server:\nStatus %d: %r', resp.status,
                    resp.reason)
      sys.exit(1)
    resp_data = await resp.text()
    try:
      devices = json.loads(resp_data)
    except UnicodeDecodeError:
      logging.exception('Failed to parse devices data from Hisense server:\nData: %r', resp_data)
      sys.exit(1)
    if not devices:
      logging.error('No device is configured! Please configure a device first.')
      sys.exit(1)
    return devices


async def _get_lanip(devices_server: str, dsn: str, headers: dict, session: aiohttp.ClientSession,
                     ssl_context: ssl.SSLContext):
  logging.debug(f'GET /apiv1/dsns/{dsn}/lan.json, headers=%r', headers)
  async with session.get(f'https://{devices_server}/apiv1/dsns/{dsn}/lan.json',
                         headers=headers,
                         ssl=ssl_context) as resp:
    if resp.status != HTTPStatus.OK.value:
      logging.error('Failed to get device data from Hisense server: %r', resp)
      sys.exit(1)
    resp_data = await resp.text()
    return json.loads(resp_data)['lanip']


async def _get_device_properties(devices_server: str, dsn: str, headers: dict,
                                 session: aiohttp.ClientSession, ssl_context: ssl.SSLContext):
  logging.debug(f'GET /apiv1/dsns/{dsn}/properties.json, headers=%r', headers)
  async with session.get(f'https://{devices_server}/apiv1/dsns/{dsn}/properties.json',
                         headers=headers,
                         ssl=ssl_context) as resp:
    if resp.status != HTTPStatus.OK.value:
      logging.error('Failed to get properties data from Hisense server: %r', resp)
      sys.exit(1)
    resp_data = await resp.text()
    return json.loads(resp_data)


async def perform_discovery(session: aiohttp.ClientSession,
                            app: str,
                            user: str,
                            passwd: str,
                            device_filter: str = None,
                            properties_filter: bool = False) -> dict:
  if app in SECRET_ID_MAP:
    app_prefix = SECRET_ID_MAP[app]
  else:
    app_prefix = 'a-Hisense-{}-field'.format(app)

  if app in SECRET_ID_EXTRA_MAP:
    app_id = '-'.join((app_prefix, SECRET_ID_EXTRA_MAP[app], 'id'))
  else:
    app_id = '-'.join((app_prefix, 'id'))

  secret = base64.b64encode(SECRET_MAP[app]).decode('utf-8').rstrip('=').replace('+', '-').replace(
      '/', '_')
  app_secret = '-'.join((app_prefix, secret))

  # Extract the region from the app ID (and fallback to US)
  region = app[-2:]
  if region not in AYLA_USER_SERVERS:
    region = 'us'
  user_server = AYLA_USER_SERVERS[region]
  devices_server = AYLA_DEVICES_SERVERS[region]

  ssl_context = ssl.SSLContext()
  ssl_context.verify_mode = ssl.CERT_NONE
  ssl_context.check_hostname = False
  ssl_context.load_default_certs()

  access_token = await _sign_in(user, passwd, user_server, app_id, app_secret, session, ssl_context)

  result = []
  headers = {
      'Accept': 'application/json',
      'Connection': 'Keep-Alive',
      'Authorization': 'auth_token ' + access_token,
      'User-Agent': _USER_AGENT,
      'Host': devices_server,
      'Accept-Encoding': 'gzip'
  }
  devices = await _get_devices(devices_server, access_token, headers, session, ssl_context)
  logging.debug('Found devices: %r', devices)
  for device in devices:
    device_data = device['device']
    device_data['product_name'] = device_data['lan_ip']
    if device_filter and device_filter != device_data['product_name']:
      continue
    dsn = device_data['dsn']
    lanip = await _get_lanip(devices_server, dsn, headers, session, ssl_context)
    properties_text = ''
    if properties_filter:
      props = await _get_device_properties(devices_server, dsn, headers, session, ssl_context)
      device_data['properties'] = props

    device_data['lanip_key'] = lanip['lanip_key']
    device_data['lanip_key_id'] = lanip['lanip_key_id']
    device_data['temp_type'] = 'C' if app in CELSIUS_BASED_APPS else 'F'
    # If the server doesn't know the MAC address, fetch it from the local network.
    if not device_data.get('mac'):
      mac = get_mac_address(ip=device_data['lan_ip'])
      if not mac or mac == '00:00:00:00:00:00':
        logging.error(f'Failed to fetch MAC address for AC on IP address {device_data["lan_ip"]}.' +
                      '\nAre you sure it is connected? Skipping...')
        continue
      device_data['mac'] = mac.replace(':', '')
    result.append(device_data)
  return result
