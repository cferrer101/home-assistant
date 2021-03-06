"""
homeassistant.components.device_tracker.luci
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Device tracker platform that supports scanning a OpenWRT router for device
presence.


It's required that the luci RPC package is installed on the OpenWRT router:
# opkg install luci-mod-rpc

Configuration:

To use the Luci tracker you will need to add something like the following
to your config/configuration.yaml

device_tracker:
  platform: luci
  host: YOUR_ROUTER_IP
  username: YOUR_ADMIN_USERNAME
  password: YOUR_ADMIN_PASSWORD

Variables:

host
*Required
The IP address of your router, e.g. 192.168.1.1.

username
*Required
The username of an user with administrative privileges, usually 'admin'.

password
*Required
The password for your given admin account.
"""
import logging
import json
from datetime import timedelta
import re
import threading
import requests

from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers import validate_config
from homeassistant.util import Throttle
from homeassistant.components.device_tracker import DOMAIN

# Return cached results if last scan was less then this time ago
MIN_TIME_BETWEEN_SCANS = timedelta(seconds=5)

_LOGGER = logging.getLogger(__name__)


def get_scanner(hass, config):
    """ Validates config and returns a Luci scanner. """
    if not validate_config(config,
                           {DOMAIN: [CONF_HOST, CONF_USERNAME, CONF_PASSWORD]},
                           _LOGGER):
        return None

    scanner = LuciDeviceScanner(config[DOMAIN])

    return scanner if scanner.success_init else None


# pylint: disable=too-many-instance-attributes
class LuciDeviceScanner(object):
    """ This class queries a wireless router running OpenWrt firmware
    for connected devices. Adapted from Tomato scanner.

    # opkg install luci-mod-rpc
    for this to work on the router.

    The API is described here:
    http://luci.subsignal.org/trac/wiki/Documentation/JsonRpcHowTo

    (Currently, we do only wifi iwscan, and no DHCP lease access.)
    """

    def __init__(self, config):
        host = config[CONF_HOST]
        username, password = config[CONF_USERNAME], config[CONF_PASSWORD]

        self.parse_api_pattern = re.compile(r"(?P<param>\w*) = (?P<value>.*);")

        self.lock = threading.Lock()

        self.last_results = {}

        self.token = _get_token(host, username, password)
        self.host = host

        self.mac2name = None
        self.success_init = self.token is not None

    def scan_devices(self):
        """ Scans for new devices and return a
            list containing found device ids. """

        self._update_info()

        return self.last_results

    def get_device_name(self, device):
        """ Returns the name of the given device or None if we don't know. """

        with self.lock:
            if self.mac2name is None:
                url = 'http://{}/cgi-bin/luci/rpc/uci'.format(self.host)
                result = _req_json_rpc(url, 'get_all', 'dhcp',
                                       params={'auth': self.token})
                if result:
                    hosts = [x for x in result.values()
                             if x['.type'] == 'host' and
                             'mac' in x and 'name' in x]
                    mac2name_list = [
                        (x['mac'].upper(), x['name']) for x in hosts]
                    self.mac2name = dict(mac2name_list)
                else:
                    # Error, handled in the _req_json_rpc
                    return
            return self.mac2name.get(device.upper(), None)

    @Throttle(MIN_TIME_BETWEEN_SCANS)
    def _update_info(self):
        """ Ensures the information from the Luci router is up to date.
            Returns boolean if scanning successful. """
        if not self.success_init:
            return False

        with self.lock:
            _LOGGER.info("Checking ARP")

            url = 'http://{}/cgi-bin/luci/rpc/sys'.format(self.host)
            result = _req_json_rpc(url, 'net.arptable',
                                   params={'auth': self.token})
            if result:
                self.last_results = []
                for device_entry in result:
                    # Check if the Flags for each device contain
                    # NUD_REACHABLE and if so, add it to last_results
                    if int(device_entry['Flags'], 16) & 0x2:
                        self.last_results.append(device_entry['HW address'])

                return True

            return False


def _req_json_rpc(url, method, *args, **kwargs):
    """ Perform one JSON RPC operation. """
    data = json.dumps({'method': method, 'params': args})
    try:
        res = requests.post(url, data=data, timeout=5, **kwargs)
    except requests.exceptions.Timeout:
        _LOGGER.exception("Connection to the router timed out")
        return
    if res.status_code == 200:
        try:
            result = res.json()
        except ValueError:
            # If json decoder could not parse the response
            _LOGGER.exception("Failed to parse response from luci")
            return
        try:
            return result['result']
        except KeyError:
            _LOGGER.exception("No result in response from luci")
            return
    elif res.status_code == 401:
        # Authentication error
        _LOGGER.exception(
            "Failed to authenticate, "
            "please check your username and password")
        return
    else:
        _LOGGER.error("Invalid response from luci: %s", res)


def _get_token(host, username, password):
    """ Get authentication token for the given host+username+password """
    url = 'http://{}/cgi-bin/luci/rpc/auth'.format(host)
    return _req_json_rpc(url, 'login', username, password)
