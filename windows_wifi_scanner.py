"""Read signal strength and reception time with the Windows Native Wi-Fi API."""
import ctypes as native
from contextlib import contextmanager
import sys
import threading
import time


WLAN_CLIENT_VERSION = 2
WLAN_NOTIFICATION_SOURCE_ACM = 8
ACM_SCAN_COMPLETE = 7
ACM_SCAN_FAIL = 8
DOT11_BSS_TYPE_ANY = 3
SCAN_TIMEOUT_SECONDS = 6
FILETIME_UNIX_EPOCH = 116444736000000000
FILETIME_TICKS_PER_SECOND = 10000000

DWORD = native.c_uint32
GUID = native.c_ubyte * 16
BUFFER = native.c_void_p


class WifiScanError(RuntimeError):
    pass


class Interface(native.Structure):
    _fields_ = [('guid', GUID), ('description', native.c_wchar * 256), ('state', DWORD)]


class Notification(native.Structure):
    _fields_ = [('source', DWORD), ('code', DWORD), ('guid', GUID),
                ('size', DWORD), ('data', BUFFER)]


class BssEntry(native.Structure):
    _fields_ = [
        ('ssid_length', DWORD), ('ssid', native.c_ubyte * 32), ('phy_id', DWORD),
        ('bssid', native.c_ubyte * 6), ('bss_type', DWORD), ('phy_type', DWORD),
        ('rssi', native.c_int32), ('link_quality', DWORD), ('in_reg_domain', native.c_ubyte),
        ('beacon_period', native.c_uint16), ('timestamp', native.c_uint64),
        ('host_timestamp', native.c_uint64), ('capabilities', native.c_uint16),
        ('frequency', DWORD), ('rate_set_length', DWORD), ('rate_set', native.c_uint16 * 126),
        ('ie_offset', DWORD), ('ie_size', DWORD),
    ]


def check_result(operation, code):
    if code:
        reasons = {
            5: 'Windows denied Wi-Fi access. Allow location access for desktop apps in Windows Location settings.',
            1062: 'The WLAN AutoConfig service is stopped. Start the service to scan.',
            0x80342002: 'The Wi-Fi radio is off. Turn on Wi-Fi and check airplane mode.',
        }
        raise WifiScanError(reasons.get(code) or f'{operation} failed ({code}): {native.FormatError(code).strip()}')


@contextmanager
def wlan_buffer(api, operation, *arguments):
    buffer = BUFFER()
    check_result(operation, getattr(api, operation)(*arguments, native.byref(buffer)))
    try:
        yield buffer.value
    finally:
        api.WlanFreeMemory(buffer)


def scan_interface(api, handle, guid):
    completed = threading.Event()
    failure_reasons = []
    callback_type = native.WINFUNCTYPE(None, native.POINTER(Notification), BUFFER)

    @callback_type
    def on_notification(pointer, context):
        notice = pointer.contents
        if notice.source != WLAN_NOTIFICATION_SOURCE_ACM or bytes(notice.guid) != bytes(guid):
            return
        if notice.code == ACM_SCAN_FAIL:
            reason = DWORD.from_address(notice.data).value if notice.data and notice.size >= native.sizeof(DWORD) else 0
            failure_reasons.append(reason)
        if notice.code in (ACM_SCAN_COMPLETE, ACM_SCAN_FAIL):
            completed.set()

    check_result('Registering for scan results', api.WlanRegisterNotification(
        handle, WLAN_NOTIFICATION_SOURCE_ACM, False, on_notification, None, None, None))
    try:
        started = time.time()
        check_result('Starting the Wi-Fi scan', api.WlanScan(handle, native.byref(guid), None, None, None))
        if not completed.wait(SCAN_TIMEOUT_SECONDS):
            raise WifiScanError('The Wi-Fi adapter did not report scan completion in time.')
        if failure_reasons:
            reason = native.create_unicode_buffer(512)
            code = failure_reasons[0]
            if code:
                api.WlanReasonCodeToString(code, len(reason), reason, None)
            raise WifiScanError(reason.value.strip() or f'The Wi-Fi adapter reported scan failure ({code}).')
        with wlan_buffer(api, 'WlanGetNetworkBssList', handle, native.byref(guid), None,
                         DOT11_BSS_TYPE_ANY, False, None) as address:
            size, count = (DWORD * 2).from_address(address)
            header_size = native.sizeof(DWORD) * 2
            if size < header_size + count * native.sizeof(BssEntry):
                raise WifiScanError('Windows returned an incomplete Wi-Fi scan result.')
            readings = []
            for entry in (BssEntry * count).from_address(address + header_size):
                seen = (entry.host_timestamp - FILETIME_UNIX_EPOCH) / FILETIME_TICKS_PER_SECOND
                if seen >= started and entry.ssid_length <= len(entry.ssid):
                    name = bytes(entry.ssid[:entry.ssid_length]).decode('utf-8', 'replace')
                    readings.append((bytes(entry.bssid).hex(':').upper(), name, entry.rssi, seen))
            return readings
    finally:
        check_result('Unregistering scan notifications', api.WlanRegisterNotification(
            handle, 0, False, None, None, None, None))


def scan_wifi():
    if sys.platform != 'win32':
        raise WifiScanError('Wi-Fi scanning requires Windows.')
    api = native.WinDLL('wlanapi.dll')
    # WLAN functions return unsigned DWORDs, including radio-off errors with the high bit set.
    for operation in ('WlanOpenHandle', 'WlanEnumInterfaces', 'WlanRegisterNotification',
                      'WlanScan', 'WlanGetNetworkBssList', 'WlanCloseHandle'):
        getattr(api, operation).restype = DWORD
    handle = BUFFER()
    version = DWORD()
    check_result('Opening Wi-Fi', api.WlanOpenHandle(WLAN_CLIENT_VERSION, None, native.byref(version), native.byref(handle)))
    try:
        with wlan_buffer(api, 'WlanEnumInterfaces', handle, None) as address:
            count = DWORD.from_address(address).value
            if not count:
                raise WifiScanError('Windows found no enabled Wi-Fi adapter. Enable or connect a Wi-Fi adapter.')
            interfaces = (Interface * count).from_address(address + 2 * native.sizeof(DWORD))
            failures = []
            for interface in interfaces:
                try:
                    return scan_interface(api, handle, interface.guid)
                except WifiScanError as error:
                    failures.append(str(error))
            raise WifiScanError(' '.join(dict.fromkeys(failures)))
    finally:
        check_result('Closing Wi-Fi', api.WlanCloseHandle(handle, None))
