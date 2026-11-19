"""Local Bluetooth and Wi-Fi signal recorder with a separate simulation."""
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from transmitter_location_estimator import estimate_location, estimate_simulation
from windows_wifi_scanner import scan_wifi, WifiScanError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.vendor'))
from bleak import BleakScanner

DEVICE_ID_PATTERN = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}|[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}')
LEGACY_DEVICE_ID = 'LEGACY'
BLUETOOTH = 'bluetooth'
WIFI = 'wifi'
WIFI_ID_PREFIX = 'WIFI:'
WIFI_SCAN_INTERVAL_SECONDS = 5
FLOORS = ('0', '1', '2')
LEGACY_LEVELS = {'basement': '0', 'first': '1', 'second': '2'}
DATA = ROOT / 'data' / 'measurements.json'
TRUTH = {'floor': '2', 'x': .72, 'y': .32}
DEFAULT_LEVEL_COUNT = 3
MAX_LEVEL_COUNT = 20
RECORDABLE_FLOORS = tuple(str(level) for level in range(MAX_LEVEL_COUNT))


def source_of(identifier):
    return WIFI if identifier.startswith(WIFI_ID_PREFIX) else BLUETOOTH


def saved_device(identifier, name):
    return {'address': identifier.removeprefix(WIFI_ID_PREFIX), 'name': name, 'rssi': None, 'last_seen': None}


class Tracker:
    def __init__(self, data_path=DATA, start_threads=True):
        self.lock = threading.RLock()
        self.data_path = Path(data_path)
        self.mode = 'live'
        self.level_count = DEFAULT_LEVEL_COUNT
        self.sim_target_floor = TRUTH['floor']
        legacy_level_counts = []
        self.target = None
        self.signal_source = BLUETOOTH
        self.source_targets = {}
        self.target_since = time.time()
        self.wifi_changed = threading.Condition(self.lock)
        self.wifi_generation = 0
        self.wifi_state, self.wifi_error = 'starting', None
        self.wifi_fresh_count = 0
        self.wifi_last_seen = None
        self.devices = {}
        self.live_sessions = {}
        self.live_last_points = {}
        self.sessions = {m: {'points': [], 'active_id': None, 'observations': []} for m in ('live', 'simulation')}
        self.scanner_state, self.error = 'starting', None
        self.estimate_cache = {}
        self.sim_dimensions = (10., 10., 3.)
        self.sim_hz, self.sim_noise = 10., 3.
        self.sim_wake = threading.Event()
        self.last_points = {'live': None, 'simulation': None}
        self.timers = {'live': None, 'simulation': None}
        self.real_scan_started = time.monotonic()
        self.signal_counts = {}
        self.sim_receiver = {'floor': '0', 'x': .5, 'y': .5}
        self.test_report = None
        self.rng = random.Random()
        if self.data_path.exists():
            saved = json.loads(self.data_path.read_text(encoding='utf-8'))
            if isinstance(saved, list):
                legacy_level_counts = [DEFAULT_LEVEL_COUNT]
                self.target = LEGACY_DEVICE_ID
                self.devices[self.target] = {'address': self.target, 'name': 'Legacy recordings', 'rssi': None, 'last_seen': None}
                backup = self.data_path.with_name('measurements.legacy.json')
                if not backup.exists():
                    backup.write_text(json.dumps(saved, indent=2), encoding='utf-8')
                for point in saved:
                    point['readings'] = []
                    point['legacy_count'] = point.get('count', 0)
                self.sessions['live']['points'] = saved
            else:
                self.target = saved.get('target')
                self.signal_source = saved.get('signal_source', BLUETOOTH)
                self.source_targets = saved.get('source_targets', {})
                self.level_count = saved.get('level_count', DEFAULT_LEVEL_COUNT)
                if saved.get('version', 0) < 5:
                    former_sim_count = saved.get('sim_level_count', self.level_count if saved.get('mode') == 'simulation' else DEFAULT_LEVEL_COUNT)
                    legacy_level_counts = [self.level_count, former_sim_count, *saved.get('live_level_counts', {}).values()]
                    self.sim_target_floor = str(min(int(TRUTH['floor']), former_sim_count - 1))
                else:
                    self.sim_target_floor = saved['sim_target_floor']
                self.live_last_points = saved.get('live_last_points', {})
                for address, name in saved.get('device_names', {}).items():
                    self.devices[address] = saved_device(address, 'Unnamed' if name == 'Unnamed device' and source_of(address) == BLUETOOTH else name)
                for address, session in saved.get('other_live_sessions', {}).items():
                    self.live_sessions[address] = {'points': session.get('points', []), 'active_id': None, 'observations': []}
                self.mode = saved.get('mode', 'live')
                self.sim_dimensions = tuple(saved.get('sim_dimensions', [10, 10, 3]))
                self.sim_receiver = saved.get('sim_receiver', self.sim_receiver)
                self.sim_hz = saved.get('sim_hz', 10.)
                self.sim_noise = saved.get('sim_noise', 3.)
                self.last_points.update(saved.get('last_points', {}))
                self.test_report = saved.get('test_report')
                for mode in self.sessions:
                    self.sessions[mode]['points'] = saved.get('sessions', {}).get(mode, {}).get('points', [])
        if self.target is not None:
            self.live_sessions[self.target] = self.sessions['live']
            self.devices.setdefault(self.target, saved_device(self.target, self.target))
            self.source_targets[self.signal_source] = self.target
        # A saved open run ends at its last checkpoint, excluding offline time.
        for session in list(self.sessions.values())+list(self.live_sessions.values()):
            for point in session['points']:
                point['floor'] = LEGACY_LEVELS.get(str(point['floor']), str(point['floor']))
                for run in point.get('recording_runs', []):
                    if run.get('stopped_at') is None:
                        run['stopped_at'] = run['started_at'] + run.get('duration_seconds', 0)
                        point['last_run_seconds'] = run.get('duration_seconds', 0)
        if legacy_level_counts:
            highest_saved_floor = max((int(point['floor']) + 1 for session in self.level_datasets() for point in session['points']), default=1)
            self.level_count = max(highest_saved_floor, *legacy_level_counts)
        if start_threads:
            threading.Thread(target=lambda: asyncio.run(self.scan_forever()), daemon=True).start()
            threading.Thread(target=self.wifi_worker, daemon=True).start()
            threading.Thread(target=self.simulation_worker, daemon=True).start()

    def save(self):
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        if self.target is not None:
            self.live_sessions[self.target] = self.sessions['live']
            self.live_last_points[self.target] = self.last_points['live']
        value = {'version': 7, 'level_count': self.level_count,
                 'signal_source': self.signal_source, 'source_targets': self.source_targets,
                 'sim_target_floor': self.sim_target_floor, 'app': 'bluetooth-finder', 'target': self.target,
                 'device_names': {address: self.devices.get(address, {}).get('name', address) for address in self.live_sessions},
                 'live_last_points': self.live_last_points,
                 'other_live_sessions': {address: {'points': session['points']} for address, session in self.live_sessions.items() if address != self.target},
                 'mode': self.mode, 'sim_dimensions': self.sim_dimensions,
                 'sim_receiver': self.sim_receiver,
                 'sim_hz': self.sim_hz, 'sim_noise': self.sim_noise, 'last_points': self.last_points,
                 'test_report': self.test_report,
                 'sessions': {m: {'points': [self.point_snapshot(m, p) for p in s['points']]}
                              for m, s in self.sessions.items()}}
        temporary = self.data_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value), encoding='utf-8')
        temporary.replace(self.data_path)

    async def scan_forever(self):
        while True:
            try:
                async with BleakScanner(detection_callback=self.advertisement):
                    with self.lock:
                        self.scanner_state, self.error = 'scanning', None
                    while True:
                        await asyncio.sleep(1)
            except Exception as exc:
                with self.lock:
                    self.scanner_state, self.error = 'error', str(exc)
                await asyncio.sleep(5)

    def advertisement(self, device, advertisement):
        if not -120 <= advertisement.rssi < 0:
            return
        with self.lock:
            address = device.address.upper()
            if not DEVICE_ID_PATTERN.fullmatch(address):
                return
            previous = self.devices.get(address, {})
            self.devices[address] = {'address': address,
                'name': getattr(advertisement, 'local_name', None) or getattr(device, 'name', None) or previous.get('name') or 'Unnamed',
                'rssi': advertisement.rssi, 'last_seen': time.time()}
            if address == self.target:
                self._record('live', advertisement.rssi)

    def restart_wifi_scan(self):
        self.wifi_generation += 1
        self.wifi_state, self.wifi_error = 'starting', None
        self.wifi_changed.notify_all()

    def wifi_worker(self):
        while True:
            with self.wifi_changed:
                self.wifi_changed.wait_for(lambda: self.mode == 'live' and self.signal_source == WIFI)
                generation = self.wifi_generation
                self.wifi_state = 'scanning'
            try:
                readings = scan_wifi()
                with self.lock:
                    if generation == self.wifi_generation:
                        self.accept_wifi_scan(readings)
                        self.wifi_state, self.wifi_error = 'complete', None
            except (WifiScanError, OSError) as error:
                with self.lock:
                    if generation == self.wifi_generation:
                        self.wifi_state, self.wifi_error = 'error', str(error)
            with self.wifi_changed:
                self.wifi_changed.wait_for(lambda: generation != self.wifi_generation, WIFI_SCAN_INTERVAL_SECONDS)

    def accept_wifi_scan(self, readings):
        fresh = set()
        for address, name, rssi, seen in sorted(readings, key=lambda reading: reading[3], reverse=True):
            identifier = WIFI_ID_PREFIX + address
            previous = self.devices.get(identifier, {})
            if seen <= (previous.get('last_seen') or 0):
                continue
            fresh.add(identifier)
            self.devices[identifier] = {'address': address, 'name': name or previous.get('name') or 'Hidden network',
                                        'rssi': rssi, 'last_seen': seen}
            self.wifi_last_seen = max(self.wifi_last_seen or seen, seen)
            if identifier == self.target and seen >= self.target_since:
                self._record('live', rssi, seen)
        self.wifi_fresh_count = len(fresh)

    def select_source(self, source):
        if source not in (BLUETOOTH, WIFI):
            raise ValueError('Choose Bluetooth or Wi-Fi.')
        with self.lock:
            if source != self.signal_source:
                self.signal_source = source
                self.activate_target(self.source_targets.get(source))
                self.restart_wifi_scan()
                self.save()

    def select_device(self, identifier):
        identifier = str(identifier).upper()
        with self.lock:
            address = identifier.removeprefix(WIFI_ID_PREFIX)
            if address != LEGACY_DEVICE_ID and not DEVICE_ID_PATTERN.fullmatch(address):
                raise ValueError('Invalid device identifier.')
            if source_of(identifier) != self.signal_source or (identifier not in self.devices and identifier not in self.live_sessions):
                raise ValueError('Choose a discovered device.')
            if identifier != self.target:
                self.activate_target(identifier)
                self.source_targets[self.signal_source] = identifier
                self.save()

    def activate_target(self, identifier):
        self.stop_timer('live')
        self.sessions['live']['active_id'] = None
        if self.target is not None:
            self.live_sessions[self.target] = self.sessions['live']
            self.live_last_points[self.target] = self.last_points['live']
        self.target = identifier
        self.target_since = time.time()
        empty = {'points': [], 'active_id': None, 'observations': []}
        self.sessions['live'] = empty if identifier is None else self.live_sessions.setdefault(identifier, empty)
        self.last_points['live'] = self.live_last_points.get(identifier)
        self.real_scan_started = time.monotonic()
        self.estimate_cache.clear()

    def _active(self, mode=None):
        session = self.sessions[mode or self.mode]
        return next((p for p in session['points'] if p['id'] == session['active_id']), None)

    def run_elapsed(self, mode):
        timer = self.timers[mode]
        return max(0., time.monotonic()-timer['monotonic_start']) if timer else 0.

    def point_snapshot(self, mode, point, summary=False):
        result = copy.deepcopy({k: v for k, v in point.items()
                                if not summary or k not in ('readings', 'recording_runs')})
        timer = self.timers[mode]
        elapsed = self.run_elapsed(mode) if timer and timer['point_id'] == point['id'] else 0.
        result['total_recording_seconds'] = round(point.get('total_recording_seconds', 0)+elapsed, 3)
        result['run_count'] = len(point.get('recording_runs', []))
        if elapsed and not summary:
            result['recording_runs'][-1]['duration_seconds'] = round(elapsed, 3)
        return result

    def stop_timer(self, mode, timestamp=None):
        timer = self.timers[mode]
        if not timer:
            return
        point = self._active(mode)
        if point:
            run = point['recording_runs'][-1]
            elapsed = self.run_elapsed(mode) if timestamp is None else max(0., timestamp-run['started_at'])
            run.update(stopped_at=time.time() if timestamp is None else timestamp,
                       duration_seconds=round(elapsed, 3))
            point['total_recording_seconds'] = point.get('total_recording_seconds', 0)+elapsed
            point['last_run_seconds'] = elapsed
            point['stopped_at'] = run['stopped_at']
        self.timers[mode] = None

    def _record(self, mode, rssi, timestamp=None):
        now = time.time() if timestamp is None else timestamp
        session = self.sessions[mode]
        observation_time = time.monotonic()
        if mode == 'live' and timestamp is not None:
            observation_time -= time.time() - timestamp
        session['observations'].append((observation_time, rssi))
        cutoff = time.monotonic()-90
        session['observations'] = [(t, v) for t, v in session['observations'] if t >= cutoff]
        point = self._active(mode)
        elapsed = self.run_elapsed(mode)
        if point and timestamp is not None:
            elapsed = timestamp - self.timers[mode]['started_at']
        if point and elapsed < 0:
            point = None
        reading = {'timestamp': now, 'rssi': rssi, 'source': mode,
                   'point_id': point['id'] if point else None,
                   'elapsed_seconds': round(elapsed, 3) if point else None,
                   'recording_run': len(point.get('recording_runs', [])) if point else None}
        path = self.signal_path(mode)
        self.ensure_signal_log(mode)
        with path.open('a', encoding='utf-8') as log:
            log.write(json.dumps(reading)+'\n')
        self.signal_counts[str(path)] += 1
        if point is None:
            return
        point.setdefault('readings', []).append(reading)
        values = [r['rssi'] for r in point['readings']]
        point.update(rssi=round(statistics.median(values), 1), count=len(values),
                     minimum=min(values), maximum=max(values), timestamp=now)
        self.estimate_cache.clear()
        self.save()

    def signal_path(self, mode):
        if mode == 'live' and self.target is None:
            raise ValueError('Choose a device first.')
        name = 'simulation' if mode == 'simulation' else 'live-'+self.target.replace(':', '')
        return self.data_path.parent / 'signals' / (name+'.jsonl')

    def ensure_signal_log(self, mode):
        path = self.signal_path(mode)
        key = str(path)
        if key in self.signal_counts:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            previous = [dict(r, point_id=p['id']) for p in self.sessions[mode]['points'] for r in p.get('readings', [])]
            previous.sort(key=lambda r: r['timestamp'])
            with path.open('w', encoding='utf-8') as log:
                for reading in previous:
                    log.write(json.dumps(reading)+'\n')
        with path.open(encoding='utf-8') as log:
            self.signal_counts[key] = sum(bool(line.strip()) for line in log)

    def signal_readings(self):
        with self.lock:
            if self.mode == 'live' and self.target is None:
                return {'readings': [], 'count': 0, 'unassigned': 0}
            self.ensure_signal_log(self.mode)
            readings = []
            with self.signal_path(self.mode).open(encoding='utf-8') as log:
                for line in log:
                    try:
                        readings.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return {'readings': readings, 'count': len(readings),
                    'unassigned': sum(r.get('point_id') is None for r in readings)}

    def clear_signal_log(self, mode, archive=False):
        self.ensure_signal_log(mode)
        path = self.signal_path(mode)
        if archive and self.signal_counts[str(path)]:
            destination = self.data_path.parent / 'live-resets'
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination / (time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'-signals.jsonl'))
        path.write_text('', encoding='utf-8')
        self.signal_counts[str(path)] = 0

    def _select(self, location, timestamp=None):
        if self.mode == 'live' and self.target is None:
            raise ValueError('Choose a device before recording.')
        session = self.sessions[self.mode]
        point = next((p for p in session['points'] if p['id'] == location.get('id')), None)
        if point is None:
            floor, x, y = str(location.get('floor')), location.get('x'), location.get('y')
            floor = LEGACY_LEVELS.get(floor, floor)
            if floor not in RECORDABLE_FLOORS or any(isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) or not 0 <= v <= 1 for v in (x, y)):
                raise ValueError('Choose a position inside a floor square.')
            now = time.time() if timestamp is None else timestamp
            point = {'id': uuid.uuid4().hex, 'floor': floor, 'x': x, 'y': y,
                     'device': self.target if self.mode == 'live' else 'simulation',
                     'started_at': now, 'timestamp': now, 'rssi': None, 'count': 0,
                     'minimum': None, 'maximum': None, 'readings': []}
            session['points'].append(point)
        self.stop_timer(self.mode, timestamp)
        point.pop('stopped_at', None)
        started = time.time() if timestamp is None else timestamp
        point.setdefault('recording_runs', []).append({'started_at': started, 'stopped_at': None, 'duration_seconds': 0})
        self.timers[self.mode] = {'point_id': point['id'], 'monotonic_start': time.monotonic(), 'started_at': started}
        session['active_id'] = point['id']
        self.last_points[self.mode] = point['id']
        if self.mode == 'simulation':
            self.sim_receiver = {key: point[key] for key in ('floor', 'x', 'y')}
        self.estimate_cache.clear()
        return point

    def measure(self, location):
        with self.lock:
            point = self._select(location)
            self.save()
            return {'saved': True, 'id': point['id'], 'recording': True, 'recording_elapsed_seconds': 0}

    def pause(self):
        with self.lock:
            self.stop_timer(self.mode)
            self.sessions[self.mode]['active_id'] = None
            self.save()

    def level_datasets(self):
        return (*self.live_sessions.values(), self.sessions['simulation'])

    def levels(self):
        return tuple(str(level) for level in range(self.level_count))

    def simulation_truth(self):
        return dict(TRUTH, floor=self.sim_target_floor)

    def set_level_count(self, count):
        if type(count) is not int or not 1 <= count <= MAX_LEVEL_COUNT:
            raise ValueError(f'Choose 1 to {MAX_LEVEL_COUNT} levels.')
        with self.lock:
            if self.level_count != count:
                self.level_count = count
                self.save()

    def level_delete_preview(self, floor):
        if floor not in RECORDABLE_FLOORS:
            raise ValueError('Choose an existing level number.')
        with self.lock:
            if self.mode == 'live' and self.target is None:
                raise ValueError('Choose a device first.')
            points = self.sessions[self.mode]['points']
            return {
                'floor': floor,
                'dataset': self.target if self.mode == 'live' else 'simulation',
                'name': self.devices[self.target]['name'] if self.mode == 'live' else 'Simulated device',
                'deleted_points': sum(point['floor'] == floor for point in points),
                'moved_points': sum(int(point['floor']) > int(floor) for point in points),
            }

    def delete_level(self, floor, dataset, deleted_points, moved_points, confirmed):
        with self.lock:
            preview = self.level_delete_preview(floor)
            if (dataset, deleted_points, moved_points) != (preview['dataset'], preview['deleted_points'], preview['moved_points']):
                raise ValueError('The selected dataset or point counts changed. Review the level again.')
            if not (deleted_points or moved_points):
                return
            if confirmed is not True:
                raise ValueError('Confirm deleting or moving the selected dataset points.')
            session = self.sessions[self.mode]
            removed_ids = {point['id'] for point in session['points'] if point['floor'] == floor}
            if session['active_id'] in removed_ids:
                self.stop_timer(self.mode)
                session['active_id'] = None
            if self.last_points[self.mode] in removed_ids:
                self.last_points[self.mode] = None
            session['points'] = [point for point in session['points'] if point['id'] not in removed_ids]
            removed_index = int(floor)
            for point in session['points']:
                if int(point['floor']) > removed_index:
                    point['floor'] = str(int(point['floor']) - 1)
            if self.mode == 'simulation':
                if int(self.sim_target_floor) > removed_index:
                    self.sim_target_floor = str(int(self.sim_target_floor) - 1)
                if self.sim_receiver['floor'] == floor:
                    self.sim_receiver = {'floor': '0', 'x': .5, 'y': .5}
                elif int(self.sim_receiver['floor']) > removed_index:
                    self.sim_receiver['floor'] = str(int(self.sim_receiver['floor']) - 1)
                self.test_report = None
            self.estimate_cache.clear()
            self.save()

    def set_mode(self, value):
        if value not in self.sessions:
            raise ValueError('Choose live or simulation.')
        with self.lock:
            self.pause()
            self.mode = value
            self.restart_wifi_scan()
            self.save()

    def resume(self):
        with self.lock:
            point_id = self.last_points[self.mode]
            if not any(p['id'] == point_id for p in self.sessions[self.mode]['points']):
                raise ValueError('Click a position before resuming.')
            return self.measure({'id': point_id})

    def reset_simulation(self):
        with self.lock:
            self.clear_signal_log('simulation')
            self.sim_target_floor = TRUTH['floor']
            self.sessions['simulation'] = {'points': [], 'active_id': None, 'observations': []}
            self.sim_receiver = {'floor': '0', 'x': .5, 'y': .5}
            self.timers['simulation'] = None
            self.last_points['simulation'] = None
            self.test_report = None
            self.estimate_cache.clear()
            self.save()

    def simulation_settings(self, value):
        hz, noise = float(value.get('hz', self.sim_hz)), float(value.get('noise', self.sim_noise))
        if not math.isfinite(hz) or not .5 <= hz <= 50 or not math.isfinite(noise) or not 0 <= noise <= 20:
            raise ValueError('Use 0.5 to 50 Hz and 0 to 20 dB noise.')
        with self.lock:
            self.sim_hz, self.sim_noise = hz, noise
            self.estimate_cache.clear()
            self.sim_wake.set()
            self.save()

    def status(self):
        with self.lock:
            session = self.sessions[self.mode]
            now = time.monotonic()
            recent = [v for t, v in session['observations'] if t >= now - 12]
            age = now - session['observations'][-1][0] if session['observations'] else None
            points = [self.point_snapshot(self.mode, point, summary=True) for point in session['points']]
            active = self._active()
            signal_count = 0
            if self.mode == 'simulation' or self.target is not None:
                self.ensure_signal_log(self.mode)
                signal_count = self.signal_counts[str(self.signal_path(self.mode))]
            last_point = next((p for p in session['points'] if p['id'] == self.last_points[self.mode]), None)
            rate_seconds = min(60., max(.1, now-self.real_scan_started))
            real_events = sum(t >= now-rate_seconds for t, r in self.sessions['live']['observations'])
            for address, saved_session in self.live_sessions.items():
                if saved_session['points']:
                    self.devices.setdefault(address, saved_device(address, 'Unnamed'))
            devices = []
            for identifier, device in self.devices.items():
                if source_of(identifier) != self.signal_source:
                    continue
                seen = device['last_seen']
                recent_device = seen is not None and time.time() - seen <= 120
                if recent_device or identifier in self.live_sessions or seen is None:
                    devices.append(dict(device, id=identifier, recently_seen=recent_device,
                                        point_count=len(self.live_sessions.get(identifier, {}).get('points', []))))
            devices.sort(key=lambda device: (device['name'].casefold(), device['address']))
            wifi = self.signal_source == WIFI
            return {'app': 'bluetooth-finder', 'target': self.target, 'name': self.devices.get(self.target, {}).get('name'),
                    'devices': devices, 'mode': self.mode, 'signal_source': self.signal_source,
                    'scanner': 'simulation' if self.mode == 'simulation' else (self.wifi_state if wifi else self.scanner_state),
                    'error': (self.wifi_error if wifi else self.error) if self.mode == 'live' else None,
                    'wifi_last_seen': self.devices[self.target]['last_seen'] if wifi and self.target else self.wifi_last_seen,
                    'wifi_fresh_count': self.wifi_fresh_count,
                    'live_rssi': round(statistics.median(recent), 1) if recent else None,
                    'last_rssi': session['observations'][-1][1] if session['observations'] else None,
                    'last_seen_seconds': round(age, 1) if age is not None else None,
                    'samples': points, 'active_id': session['active_id'],
                    'signal_count': signal_count,
                    'resume_point_id': last_point['id'] if last_point else None,
                    'level_count': self.level_count, 'levels': list(self.levels()),
                    'recording_elapsed_seconds': round(self.run_elapsed(self.mode) if active else
                        (last_point or {}).get('last_run_seconds', 0), 3),
                    'sim_hz': self.sim_hz, 'sim_noise': self.sim_noise,
                    'real_received_hz': round(real_events/rate_seconds, 3), 'rate_window_seconds': round(rate_seconds, 1),
                    'active_count': active.get('count', 0) if active else 0,
                    'recent_count': len(recent), 'required_count': 5,
                    'ready_to_record': bool(active and active.get('count', 0) >= 5),
                    'pending': None, 'pid': os.getpid(), 'simulation_truth': self.simulation_truth(),
                    'sim_receiver': self.sim_receiver,
                    'sim_dimensions': self.sim_dimensions, 'test_report': self.test_report}

    def estimate(self, width, depth, height):
        with self.lock:
            points = self.sessions[self.mode]['points']
            highest_point = max((int(point['floor']) for point in points), default=0)
            highest_target = int(self.sim_target_floor) if self.mode == 'simulation' else 0
            estimate_levels = max(DEFAULT_LEVEL_COUNT, highest_point + 1, highest_target + 1)
            key = (self.mode, tuple((p['id'], p['floor'], p['rssi'], p['count']) for p in points), width, depth, height, self.sim_noise, estimate_levels)
            if key not in self.estimate_cache:
                result = (estimate_simulation(points, width, depth, height, self.sim_noise, estimate_levels) if self.mode == 'simulation'
                          else estimate_location(points, width, depth, height, estimate_levels))
                if self.mode == 'simulation' and result['ready']:
                    result['actual_error_metres'] = round(math.sqrt(((result['x']-self.simulation_truth()['x'])*width)**2
                        + ((result['y']-self.simulation_truth()['y'])*depth)**2
                        + ((int(result['floor'])-int(self.sim_target_floor))*height)**2), 2)
                self.estimate_cache = {key: result}
            return self.estimate_cache[key]

    def point_samples(self, point_id):
        with self.lock:
            points = self.sessions[self.mode]['points']
            point = next((p for p in points if p['id'] == point_id), None)
            if point is None:
                raise ValueError('Point not found in this mode.')
            return self.point_snapshot(self.mode, point)

    def export(self):
        with self.lock:
            return {'mode': self.mode, 'signal_source': self.signal_source,
                    'target': self.devices[self.target]['address'] if self.mode == 'live' and self.target else
                              ('simulation' if self.mode == 'simulation' else None),
                    'signal_readings': self.signal_readings()['readings'],
                    'points': [self.point_snapshot(self.mode, p)
                                                for p in self.sessions[self.mode]['points']]}

    def remove(self, point_id):
        with self.lock:
            session = self.sessions[self.mode]
            if session['active_id'] == point_id:
                self.stop_timer(self.mode)
                session['active_id'] = None
            if self.last_points[self.mode] == point_id:
                self.last_points[self.mode] = None
            session['points'] = [p for p in session['points'] if p['id'] != point_id]
            self.estimate_cache.clear()
            self.save()

    def reset_live(self):
        with self.lock:
            self.stop_timer('live')
            self.clear_signal_log('live', archive=True)
            session = self.sessions['live']
            points = len(session['points'])
            samples = sum(p.get('count', 0) for p in session['points'])
            if points:
                archive_dir = self.data_path.parent / 'live-resets'
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive = archive_dir / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
                archive.write_text(json.dumps({'target': self.target, 'reset_at': time.time(),
                                              'points': session['points']}), encoding='utf-8')
            self.sessions['live'] = {'points': [], 'active_id': None, 'observations': []}
            self.last_points['live'] = None
            self.estimate_cache.clear()
            self.save()
            return {'ok': True, 'cleared_points': points, 'cleared_samples': samples}

    def simulated_signal(self, point, rng):
        width, depth, height = self.sim_dimensions
        distance = max(.8, math.sqrt(((point['x']-self.simulation_truth()['x'])*width)**2
            + ((point['y']-self.simulation_truth()['y'])*depth)**2
            + ((int(point['floor'])-int(self.sim_target_floor))*height)**2))
        return round(max(-120, min(-10, -55 - 25*math.log10(distance) + rng.gauss(0, self.sim_noise))), 1)

    def simulation_worker(self):
        while True:
            self.sim_wake.wait(1/self.sim_hz)
            self.sim_wake.clear()
            with self.lock:
                if self.mode == 'simulation':
                    receiver = self._active('simulation') or self.sim_receiver
                    self._record('simulation', self.simulated_signal(receiver, self.rng))

    def run_simulation_test(self, dimensions):
        with self.lock:
            self.pause()
            self.mode = 'simulation'
            self.restart_wifi_scan()
            self.sim_dimensions = tuple(dimensions)
            self.clear_signal_log('simulation')
            self.sim_target_floor = TRUTH['floor']
            self.sessions['simulation'] = {'points': [], 'active_id': None, 'observations': []}
            rng = random.Random(1137)
            route = [('0',.12,.12),('0',.88,.82),('1',.12,.85),('1',.88,.12),('1',.5,.5),('2',.12,.12),('2',.88,.82),('2',.88,.12)]
            start = time.time() - len(route)*10
            for i, (floor, x, y) in enumerate(route):
                point = self._select({'floor': floor, 'x': x, 'y': y}, start+i*10)
                for j in range(20):
                    self._record('simulation', self.simulated_signal(point, rng), start+i*10+j*.5)
            self.pause()
            result = self.estimate(*dimensions)
            self.test_report = {'points': len(route), 'raw_samples': 160, 'truth': self.simulation_truth(),
                                'estimated': {k: result[k] for k in ('floor', 'x', 'y')},
                                'error_metres': result['actual_error_metres'],
                                'passed': result['floor'] == self.simulation_truth()['floor'] and result['actual_error_metres'] < 1}
            self.save()
            return self.test_report


def dimensions_from(value):
    result = [float(value.get(key, default)) for key, default in [('width', 10), ('depth', 10), ('height', 3)]]
    if not all(math.isfinite(n) and 1 <= n <= 100 for n in result):
        raise ValueError('Dimensions must be between 1 and 100 metres.')
    return result


class Handler(BaseHTTPRequestHandler):
    tracker = None

    def log_message(self, *args):
        pass

    def send(self, value, status=200):
        payload = json.dumps(value, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == '/api/status':
                return self.send(self.tracker.status())
            if path == '/api/export':
                return self.send(self.tracker.export())
            if path == '/api/signal-readings':
                return self.send(self.tracker.signal_readings())
            if path == '/api/point-samples':
                return self.send(self.tracker.point_samples(parse_qs(parsed.query).get('id', [''])[0]))
            if path == '/api/level-delete-preview':
                floor = parse_qs(parsed.query).get('floor', [''])[0]
                return self.send(self.tracker.level_delete_preview(floor))
            if path == '/api/estimate':
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                return self.send(self.tracker.estimate(*dimensions_from(query)))
        except ValueError as exc:
            return self.send({'error': str(exc)}, 400)
        if path not in ('/', '/wireless-device-locator.js', '/wireless-device-locator.css'):
            return self.send({'error': 'Not found'}, 404)
        name = 'wireless-device-locator.html' if path == '/' else path[1:]
        payload = (ROOT / 'static' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', {'wireless-device-locator.html': 'text/html; charset=utf-8',
                         'wireless-device-locator.js': 'application/javascript', 'wireless-device-locator.css': 'text/css'}[name])
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')):
            return self.send({'error': 'Origin not allowed'}, 403)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 4096:
                raise ValueError('Invalid request size')
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError('Invalid request')
            if self.path in ('/api/measure', '/api/point'):
                return self.send(self.tracker.measure(value), 201)
            if self.path == '/api/visible-levels':
                self.tracker.set_level_count(value['count'])
                return self.send({'ok': True})
            if self.path == '/api/delete-level':
                self.tracker.delete_level(value['floor'], value['dataset'], value['deleted_points'], value['moved_points'], value['confirmed'])
                return self.send({'ok': True})
            if self.path == '/api/signal-source':
                self.tracker.select_source(value['source'])
                return self.send({'ok': True})
            if self.path == '/api/device':
                self.tracker.select_device(value['address'])
                return self.send({'ok': True})
            if self.path == '/api/reset-live':
                return self.send(self.tracker.reset_live())
            if self.path == '/api/remove':
                self.tracker.remove(value['id'])
            elif self.path == '/api/pause':
                self.tracker.pause()
            elif self.path == '/api/resume':
                return self.send(self.tracker.resume())
            elif self.path == '/api/simulation-reset':
                self.tracker.reset_simulation()
            elif self.path == '/api/simulation-settings':
                self.tracker.simulation_settings(value)
            elif self.path == '/api/mode':
                self.tracker.set_mode(value['mode'])
            elif self.path == '/api/simulation-test':
                return self.send(self.tracker.run_simulation_test(dimensions_from(value)))
            else:
                return self.send({'error': 'Not found'}, 404)
            return self.send({'ok': True})
        except (ValueError, KeyError, TypeError) as exc:
            return self.send({'error': str(exc)}, 400)
        except RuntimeError as exc:
            return self.send({'error': str(exc)}, 409)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('ETHERMAGIC_PORT', '8731'))), Handler)
    Handler.tracker = Tracker()
    print('Wireless Device Locator: http://127.0.0.1:8731', flush=True)
    server.serve_forever()
