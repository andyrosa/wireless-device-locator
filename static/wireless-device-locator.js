const DEFAULT_LEVEL_COUNT = 3;
const SIGNAL_LOG = '__signal_log__';
const REFRESH_MILLISECONDS = 1000;
const CLOCK_MILLISECONDS = 250;
const VIEW_PREFERENCES_KEY = 'bluetooth-finder-view';
const DEFAULT_SCENE_POSE = {yaw: -.65, pitch: .85, zoom: 1};
const MIN_PITCH = .2, MAX_PITCH = 1.35, MIN_ZOOM = .5, MAX_ZOOM = 2.5;
const METRES_PER_FOOT = 0.3048;
const DEFAULT_LIVE_GEOMETRY = {width: 10, depth: 10, height: 10 * METRES_PER_FOOT};
const GEOMETRY_FIELDS = [
 {key: 'width', input: 'house-width', output: 'sim-width'},
 {key: 'depth', input: 'house-depth', output: 'sim-depth'},
 {key: 'height', input: 'floor-height', output: 'sim-height'},
];
const levelName = identifier => floors[identifier] || `Level ${identifier}`;
const el = identifier => document.getElementById(identifier);
let floors = {};
let liveGeometry = {...DEFAULT_LIVE_GEOMETRY};
let changingLevels = false;
let changingSelection = false, selectionRevision = 0;
let state = null, submitting = false, signature = '', estimateToken = 0, polling = false;
let selectedPointId = SIGNAL_LOG, sampleListKey = '', sampleTableScope = '', sampleLoading = false;
let lastEstimate = null, scenePose = {...DEFAULT_SCENE_POSE}, sceneProjection = null, sceneHits = [], sceneDrag = null;
let preferredRecordingFloor = '0';
const readingsViews = new Map();
let recordingClock = {seconds: 0, at: performance.now(), running: false};
const geometryStyles = document.createElement('style');
document.head.append(geometryStyles);
const geometryRules = new WeakMap();
let geometryRuleCount = 0;
const mapResize = new ResizeObserver(entries => {
 if (state) for (const {target: map} of entries) renderFloorPoints(map);
});

function setText(element, value) {
 const text = String(value);
 if (element.textContent === text) return;
 if (element.childNodes.length === 1 && element.firstChild.nodeType === Node.TEXT_NODE) element.firstChild.nodeValue = text;
 else element.textContent = text;
}
function setHidden(element, hidden) { if (element.hidden !== hidden) element.hidden = hidden; }
function setStyle(element, property, value) {
 let rule = geometryRules.get(element);
 if (!rule) {
  element.dataset.geometryRule = String(++geometryRuleCount);
  const ruleIndex = geometryStyles.sheet.insertRule(`[data-geometry-rule="${element.dataset.geometryRule}"] {}`, geometryStyles.sheet.cssRules.length);
  rule = geometryStyles.sheet.cssRules[ruleIndex]; geometryRules.set(element, rule);
 }
 if (rule.style.getPropertyValue(property) !== value) rule.style.setProperty(property, value);
}
function setInput(identifier, value) {
 const input = el(identifier), text = String(value);
 if (document.activeElement !== input && input.value !== text) input.value = text;
}
function duration(seconds) {
 if (seconds == null) return '--';
 const totalSeconds = Math.floor(seconds), minutes = Math.floor(totalSeconds / 60), remainingSeconds = String(totalSeconds % 60).padStart(2, '0');
 return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${remainingSeconds}` : `${String(minutes).padStart(2, '0')}:${remainingSeconds}`;
}
function drawClock() { setText(el('recording-timer'), duration(recordingClock.seconds + (recordingClock.running ? (performance.now() - recordingClock.at) / 1000 : 0))); }
const format = value => value == null ? 'No samples' : `${value} dBm`;
const pointLabel = (point, number) => `P${number} ${point.rssi == null ? '-- dBm' : format(point.rssi)}`;
function color(signal) { if (signal == null) return '#929b89'; const strength = Math.max(0, Math.min(1, (signal + 105) / 65)); return `hsl(${210 - strength * 185},45%,48%)`; }
async function api(path, body) {
 const response = await fetch(path, body === null ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
 const result = await response.json();
 if (!response.ok) throw new Error(result.error || `Request failed: ${response.status}`);
 return result;
}
function tell(message, error) { setText(el('status-text'), message); setHidden(el('status-text'), !error); }
function saveViewPreferences() {
 try {
  localStorage.setItem(VIEW_PREFERENCES_KEY, JSON.stringify({
   view3D: el('view-3d').checked,
   scenePose,
   recordingFloor: preferredRecordingFloor,
   readingsCollapsed: el('readings-table').hidden,
   readingsViews: [...readingsViews],
  }));
 } catch (error) { tell(`Cannot save view preferences: ${error.message}`, true); }
}
function restoreViewPreferences() {
 try {
  const saved = JSON.parse(localStorage.getItem(VIEW_PREFERENCES_KEY));
  if (!saved) return;
  const pose = saved.scenePose;
  if (pose && Number.isFinite(pose.yaw) && pose.pitch >= MIN_PITCH && pose.pitch <= MAX_PITCH && pose.zoom >= MIN_ZOOM && pose.zoom <= MAX_ZOOM) {
   scenePose = {yaw: pose.yaw, pitch: Number(pose.pitch), zoom: Number(pose.zoom)};
  }
  if (typeof saved.recordingFloor === 'string' && /^[0-9]+$/.test(saved.recordingFloor)) preferredRecordingFloor = saved.recordingFloor;
  if (Array.isArray(saved.readingsViews)) {
   for (const entry of saved.readingsViews) {
    if (Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1] === 'string') readingsViews.set(entry[0], entry[1]);
   }
  }
  setReadingsExpanded(saved.readingsCollapsed !== true);
  el('view-3d').checked = saved.view3D === true;
 } catch (error) { tell(`Cannot restore view preferences: ${error.message}`, true); }
}
function readingsDataset() { return state.mode === 'simulation' ? 'simulation' : state.target; }
function selectReadingsView(identifier) {
 selectedPointId = identifier;
 sampleListKey = '';
 if (state) {
  const dataset = readingsDataset();
  if (identifier === SIGNAL_LOG) readingsViews.delete(dataset);
  else readingsViews.set(dataset, identifier);
  saveViewPreferences();
 }
}
function applyPlanTop() { setHidden(el('plan-orientation-arrow'), !el('plan-top').value.trim()); draw3D(); updateOrientationArrow(); }
function updateOrientationArrow() {
 let angle = 0;
 if (el('view-3d').checked && sceneProjection) {
  const {width, depth, height} = geometry(), topHeight = (Object.keys(floors).length - 1) * height;
  const center = sceneProjection.project(width * .5, depth * .5, topHeight), top = sceneProjection.project(width * .5, depth * .1, topHeight);
  angle = Math.atan2(top.y - center.y, top.x - center.x) * 180 / Math.PI + 90;
 }
 setStyle(el('plan-orientation-arrow'), 'transform', `rotate(${angle}deg)`);
}
function geometry() {
 if (state?.mode !== 'simulation') return liveGeometry;
 const [width, depth, height] = state.sim_dimensions;
 return {width, depth, height};
}
function renderGeometry() {
 const simulated = state?.mode === 'simulation', metric = el('metric-units').checked;
 const dimensions = geometry(), metresPerUnit = metric ? 1 : METRES_PER_FOOT;
 for (const {key, input, output} of GEOMETRY_FIELDS) {
  const displayValue = Number((dimensions[key] / metresPerUnit).toFixed(2));
  setInput(input, displayValue);
  setText(el(output), displayValue);
  setHidden(el(input), simulated);
  setHidden(el(output), !simulated);
 }
 for (const unit of document.querySelectorAll('.plan-unit')) setText(unit, metric ? 'm' : 'ft');
}
function applyMapAspect() {
 const {width, depth} = geometry();
 if (Number.isFinite(width) && Number.isFinite(depth) && width > 0 && depth > 0) setStyle(document.documentElement, '--floor-aspect', String(width / depth));
 draw3D();
}
function restoreLiveGeometry() {
 try {
  const saved = localStorage.getItem('f1-geometry');
  if (saved !== null) {
   const dimensions = JSON.parse(saved);
   for (const {key} of GEOMETRY_FIELDS) {
    if (Number.isFinite(dimensions[key]) && dimensions[key] >= 1 && dimensions[key] <= 100) liveGeometry[key] = dimensions[key];
   }
  }
  el('metric-units').checked = localStorage.getItem('bluetooth-finder-metric') === 'true';
 } catch (error) { tell(`Cannot read saved floor dimensions: ${error.message}`, true); }
}
function reconcileOptions(picker, entries, selectedValue) {
 const options = new Map([...picker.options].map(option => [option.value, option]));
 const values = new Set(entries.map(entry => entry.value));
 for (const option of [...picker.options]) if (!values.has(option.value)) option.remove();
 entries.forEach((entry, index) => {
  const option = options.get(entry.value) || document.createElement('option');
  if (option.value !== entry.value) option.value = entry.value;
  setText(option, entry.text);
  if (picker.options[index] !== option) picker.insertBefore(option, picker.options[index] || null);
 });
 if (document.activeElement !== picker && picker.value !== selectedValue) picker.value = selectedValue;
}
function updateDevicePicker(data) {
 const simulated = data.mode === 'simulation';
 const wifi = data.signal_source === 'wifi';
 setHidden(el('signal-source-field'), simulated);
 const source = data.signal_source ?? 'bluetooth';
 if (el('signal-source').value !== source) el('signal-source').value = source;
 el('signal-source').disabled = changingSelection || !data.signal_source;
 setHidden(el('live-device-field'), simulated); setHidden(el('simulated-source'), !simulated);
 if (simulated) { setText(el('simulated-source'), `Simulated device - ${data.samples.length} ${data.samples.length === 1 ? 'point' : 'points'}`); return; }
 const picker = el('device-picker');
 setText(el('device-label'), wifi ? 'Access point' : 'Device');
 picker.classList.toggle('wifi', wifi);
 picker.disabled = changingSelection || data.devices.length === 0;
 const deviceOptions = data.devices.map(device => {
  const signal = device.recently_seen ? `${device.rssi} dBm` : 'not seen';
  const points = `${device.point_count} ${device.point_count === 1 ? 'point' : 'points'}`;
  const addressLabel = wifi || device.name === 'Unnamed' ? device.address : device.address.slice(-8);
  return {value: device.id ?? device.address, text: [device.name, signal, addressLabel, points].join(' \u00b7 ')};
 });
 if (!data.target) {
  const placeholder = wifi ? (data.devices.length ? 'Choose an access point' : 'No access points yet')
                          : (data.devices.length ? 'Choose a device' : 'Scanning for devices...');
  deviceOptions.unshift({value: '', text: placeholder});
 }
 reconcileOptions(picker, deviceOptions, data.target ?? '');
}
function updateWifiStatus(data) {
 const visible = data.mode === 'live' && data.signal_source === 'wifi';
 const needsRestart = data.mode === 'live' && !data.signal_source;
 el('measurement-status').classList.toggle('wifi', visible);
 setHidden(el('wifi-scan-status'), !visible && !needsRestart);
 setHidden(el('wifi-freshness'), !visible);
 setHidden(el('signal-age'), visible);
 if (needsRestart) setText(el('wifi-scan-status'), 'Restart the local reader to enable Wi-Fi.');
 if (!visible) return;
 const count = data.wifi_fresh_count;
 const phases = {starting: 'Starting', scanning: 'Scanning',
  complete: `${count} ${count === 1 ? 'AP' : 'APs'}`, error: 'Failed'};
 setText(el('wifi-scan-status'), phases[data.scanner]);
 const seen = data.wifi_last_seen == null ? null : new Date(data.wifi_last_seen * 1000);
 const freshness = el('wifi-freshness');
 setText(freshness, `Last ${seen ? seen.toTimeString().slice(0, 8) : '--:--:--'}`);
 const subject = data.target ? ' for this access point' : '';
 const detail = seen ? `Last fresh reading${subject} at ${seen.toLocaleString()}` : `No fresh reading${subject} yet`;
 if (freshness.title !== detail) freshness.title = detail;
}
function updatePointPicker() {
 if (selectedPointId !== SIGNAL_LOG && !state.samples.some(point => point.id === selectedPointId)) selectReadingsView(SIGNAL_LOG);
 const entries = [{value: SIGNAL_LOG, text: 'All readings'}, ...state.samples.map((point, index) => ({value: point.id, text: `${pointLabel(point, index + 1)} - ${levelName(point.floor)}`}))];
 reconcileOptions(el('point-picker'), entries, selectedPointId);
 setHidden(el('remove-point'), selectedPointId === SIGNAL_LOG);
}
function updateDeleteControls() {
 const highestPointFloor = Math.max(-1, ...state.samples.map(point => Number(point.floor)));
 setHidden(el('delete-recording-level'), highestPointFloor < Number(el('scene-floor').value));
 for (const button of document.querySelectorAll('.delete-floor')) setHidden(button, highestPointFloor < Number(button.dataset.floor));
}
function updateVisibleLevels() {
 const shown = state.level_count;
 ensureLevels(shown);
 updateDeleteControls();
 const hiddenCounts = new Map();
 for (const point of state.samples) {
  const floor = Number(point.floor);
  if (floor >= shown) hiddenCounts.set(floor, (hiddenCounts.get(floor) ?? 0) + 1);
 }
 const counts = [...hiddenCounts].sort(([leftFloor], [rightFloor]) => leftFloor - rightFloor);
 const info = el('hidden-levels-info');
 setText(info, counts.length ? 'Hidden points \u00b7 ' + counts.map(([floor, count]) => `Level ${floor}: ${count}`).join(' \u00b7 ') : '');
 setHidden(info, !counts.length);
}
function render(data) {
 const changedMode = state?.mode !== data.mode, changedDevice = state?.target !== data.target;
 state = data;
 if (changedMode || changedDevice) { lastEstimate = null; selectedPointId = readingsViews.get(readingsDataset()) ?? SIGNAL_LOG; sampleListKey = ''; }
 updateVisibleLevels(); el('house-levels').disabled = false; setInput('house-levels', data.level_count); updateDevicePicker(data);
 updateWifiStatus(data);
 const simulated = data.mode === 'simulation';
 el('mode').checked = simulated;
 setHidden(el('simulation-controls'), !simulated); setHidden(el('simulation-legend'), !simulated); setHidden(el('real-rate'), simulated);
 setText(el('real-rate'), `${data.real_received_hz.toFixed(2)} Hz`);
 setInput('sim-hz', data.sim_hz); setInput('sim-noise', data.sim_noise);
 renderGeometry();
 applyMapAspect();
 const active = data.samples.find(point => point.id === data.active_id), resumePoint = data.samples.find(point => point.id === data.resume_point_id);
 const activeNumber = active ? data.samples.indexOf(active) + 1 : 0;
 setText(el('live'), data.live_rssi ?? data.last_rssi ?? '--');
 setText(el('signal-age'), data.live_rssi != null ? '' : data.last_seen_seconds == null ? (simulated ? 'Starting simulation' : 'No signal yet') : `${Math.round(data.last_seen_seconds)}s ago`);
 recordingClock = {seconds: data.recording_elapsed_seconds, at: performance.now(), running: !!active}; drawClock();
 setHidden(el('run-clock'), !active && !resumePoint); setHidden(el('pause-recording'), !active && !resumePoint);
 setText(el('pause-recording'), active ? `Pause P${activeNumber}` : `Resume P${resumePoint ? data.samples.indexOf(resumePoint) + 1 : ''}`);
 setHidden(el('reset-data'), !data.samples.length && !data.signal_count);
 setHidden(el('export'), !data.signal_count);
 tell(data.error ? `${data.signal_source === 'wifi' ? 'Wi-Fi' : 'Bluetooth'}: ${data.error}` : '', !!data.error);
 const nextSignature = JSON.stringify([data.mode, data.target, data.level_count, data.samples.map(point => [point.id, point.floor, point.x, point.y, point.rssi, point.count]), data.active_id]);
 if (nextSignature !== signature) { signature = nextSignature; renderMaps(); updateEstimate(); draw3D(); }
 updatePointPicker(); refreshSampleList();
}
async function selectPoint(location) {
 if (submitting || !state) return;
 if (state.mode === 'live' && !state.target) { tell(state.signal_source === 'wifi' ? 'Choose a Wi-Fi access point before recording.' : 'Choose a Bluetooth device before recording.', true); return; }
 submitting = true;
 try {
  const result = await api('/api/point', location); selectReadingsView(result.id);
  recordingClock = {seconds: 0, at: performance.now(), running: true}; drawClock(); await refresh();
 } catch (error) { tell(error.message, true); } finally { submitting = false; }
}
function ensureLevels(count) {
 if (Object.keys(floors).length === count) return;
 floors = Object.fromEntries(Array.from({length: count}, (_, index) => [String(index), `Level ${index}`]));
 const grid = document.querySelector('.floor-grid'); mapResize.disconnect(); grid.replaceChildren(); setStyle(document.documentElement, '--level-count', String(count));
 for (const [floor, label] of Object.entries(floors)) {
  const card = document.createElement('article'); card.className = 'floor-card';
  card.innerHTML = `<div class="floor-title"><h3>${label}</h3><span id="count-${floor}"></span><button class="delete-floor" data-floor="${floor}" type="button" aria-label="Delete ${label}" hidden>Delete level</button></div><div class="map-wrap"><div class="floor-map" id="map-${floor}" data-floor="${floor}" role="button" tabindex="0" aria-label="${label}: click your position to record"></div></div>`;
  grid.append(card); bindFloorMap(card.querySelector('.floor-map'));
  card.querySelector('.delete-floor').addEventListener('click', () => deleteLevel(floor));
 }
 const picker = el('scene-floor'), selectedFloor = Object.hasOwn(floors, preferredRecordingFloor) ? preferredRecordingFloor : '0';
 reconcileOptions(picker, Object.entries(floors).map(([value, text]) => ({value, text})), selectedFloor);
 if (picker.value !== selectedFloor) picker.value = selectedFloor;
 signature = ''; lastEstimate = null; applyPlanTop();
}
function bindFloorMap(map) {
 mapResize.observe(map);
 map.addEventListener('click', event => {
  const bounds = map.getBoundingClientRect();
  selectPoint({floor: map.dataset.floor, x: (event.clientX - bounds.left) / bounds.width, y: (event.clientY - bounds.top) / bounds.height});
 });
 map.addEventListener('keydown', event => {
  if (event.target === map && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); selectPoint({floor: map.dataset.floor, x: .5, y: .5}); }
 });
}
function renderFloorPoints(map) {
 const floor = map.dataset.floor, points = state.samples.filter(point => point.floor === floor);
 const markers = new Map([...map.querySelectorAll('.point')].map(marker => [marker.dataset.pointId, marker]));
 const bounds = map.getBoundingClientRect();
 map.classList.toggle('measuring', points.some(point => point.id === state.active_id));
 for (const point of points) {
  let marker = markers.get(point.id);
  if (!marker) {
   marker = document.createElement('button'); marker.type = 'button'; marker.className = 'point'; marker.dataset.pointId = point.id;
   const caption = document.createElement('span'); caption.className = 'point-label'; marker.append(caption);
   marker.addEventListener('click', event => { event.stopPropagation(); selectPoint({id: point.id}); }); map.append(marker);
  }
  markers.delete(point.id);
  const label = pointLabel(point, state.samples.indexOf(point) + 1);
  setText(marker.firstElementChild, label);
  if (marker.title !== label) marker.title = label;
  marker.classList.toggle('active', point.id === state.active_id);
  setStyle(marker, '--point-color', color(point.rssi));
  const tipLeft = point.x * bounds.width, tipTop = point.y * bounds.height;
  const labelWidth = marker.getBoundingClientRect().width;
  const left = Math.max(0, Math.min(tipLeft - labelWidth / 2, bounds.width - labelWidth));
  setStyle(marker, 'left', `${left}px`); setStyle(marker, 'top', `${point.y * 100}%`);
  setStyle(marker, '--tip-left', `${tipLeft - left}px`);
  marker.classList.toggle('below', tipTop < marker.offsetHeight && point.y < .5);
 }
 for (const unused of markers.values()) unused.remove();
 setText(el(`count-${floor}`), points.length ? `${points.length} ${points.length === 1 ? 'point' : 'points'}` : '');
}
function renderMaps() {
 for (const floor of Object.keys(floors)) renderFloorPoints(el(`map-${floor}`));
 let truthMarker = document.querySelector('.truth-target');
 if (state.mode === 'simulation' && Object.hasOwn(floors, state.simulation_truth.floor)) {
  const truth = state.simulation_truth;
  if (!truthMarker) { truthMarker = document.createElement('div'); truthMarker.className = 'truth-target'; setText(truthMarker, '+'); }
  if (truthMarker.parentElement !== el(`map-${truth.floor}`)) el(`map-${truth.floor}`).append(truthMarker);
  setStyle(truthMarker, 'left', `${truth.x * 100}%`); setStyle(truthMarker, 'top', `${truth.y * 100}%`);
 } else if (truthMarker) truthMarker.remove();
 applyPlanTop();
}
function drawRegions(regions) {
 const namespace = 'http://www.w3.org/2000/svg';
 for (const floor of Object.keys(floors)) {
  const map = el(`map-${floor}`), region = regions.find(candidate => candidate.floor === floor);
  let overlay = map.querySelector('.estimate-regions');
  if (!region) { if (overlay) overlay.remove(); continue; }
  if (!overlay) {
   overlay = document.createElementNS(namespace, 'svg'); overlay.setAttribute('viewBox', '0 0 100 100'); overlay.setAttribute('preserveAspectRatio', 'none'); overlay.setAttribute('class', 'estimate-regions');
   const polygon = document.createElementNS(namespace, 'polygon'); polygon.setAttribute('vector-effect', 'non-scaling-stroke'); overlay.append(polygon); map.append(overlay);
  }
  const coordinates = region.polygon.map(([across, down]) => `${across * 100},${down * 100}`).join(' ');
  if (overlay.firstChild.getAttribute('points') !== coordinates) overlay.firstChild.setAttribute('points', coordinates);
 }
}
async function updateEstimate() {
 if (!state) return;
 const token = ++estimateToken, dimensions = geometry();
 try {
  const result = await api('/api/estimate?' + new URLSearchParams(dimensions), null);
  if (token !== estimateToken) return;
  lastEstimate = result; drawRegions(result.regions || []); draw3D();
  let marker = document.querySelector('.estimate-target');
  if (!result.ready || !Object.hasOwn(floors, result.floor)) { if (marker) marker.remove(); return; }
  if (!marker) { marker = document.createElement('div'); marker.className = 'estimate-target'; setText(marker, 'X'); }
  if (marker.parentElement !== el(`map-${result.floor}`)) el(`map-${result.floor}`).append(marker);
  setStyle(marker, 'left', `${result.x * 100}%`); setStyle(marker, 'top', `${result.y * 100}%`);
 } catch (error) { tell(`Location estimate: ${error.message}`, true); }
}
function readingsScope() { return `${state.mode}:${state.target}:${selectedPointId}`; }
async function refreshSampleList() {
 if (!state || sampleLoading) return;
 const isLog = selectedPointId === SIGNAL_LOG, point = state.samples.find(candidate => candidate.id === selectedPointId);
 if (!isLog && !point) return;
 const sampleVersion = isLog ? `${state.signal_count}:${state.samples.map(sample => sample.id).join(',')}` : `${point.count}:${Math.floor(point.total_recording_seconds)}:${point.run_count}`;
 const scope = readingsScope(), key = `${scope}:${sampleVersion}`;
 if (key === sampleListKey) return;
 sampleLoading = true;
 try {
  const data = await api(isLog ? '/api/signal-readings' : '/api/point-samples?id=' + encodeURIComponent(point.id), null);
  if (readingsScope() !== scope) return;
  const table = el('raw-readings');
  if (sampleTableScope !== scope) { table.replaceChildren(); sampleTableScope = scope; }
  setHidden(el('signal-point-column'), !isLog);
  const pointSignal = point?.count ? `${point.count} samples; median ${format(point.rssi)}` : 'No samples yet';
  setText(el('samples-note'), isLog ? `${data.count} readings; ${data.unassigned} without a location` : `${pointSignal}; total ${duration(data.total_recording_seconds)} in ${data.run_count} ${data.run_count === 1 ? 'run' : 'runs'}${data.legacy_count ? '; older raw values unavailable' : ''}`);
  const existing = new Map([...table.rows].map(row => [row.dataset.sampleIndex, row]));
  for (const row of [...table.rows]) if (!data.readings.length || !row.dataset.sampleIndex || Number(row.dataset.sampleIndex) > data.readings.length) row.remove();
  data.readings.forEach((sample, index) => {
   const sampleNumber = index + 1;
   let row = existing.get(String(sampleNumber));
   if (!row) { row = table.insertRow(0); row.dataset.sampleIndex = String(sampleNumber); }
   const values = [sampleNumber];
   if (isLog) { const pointIndex = state.samples.findIndex(candidate => candidate.id === sample.point_id); values.push(pointIndex >= 0 ? `P${pointIndex + 1}` : sample.point_id ? 'Removed point' : '--'); }
   values.push(sample.recording_run ?? '--', duration(sample.elapsed_seconds), new Date(sample.timestamp * 1000).toLocaleTimeString(), sample.rssi);
   values.forEach((value, column) => { const cell = row.cells[column] || row.insertCell(); setText(cell, value); });
  });
  if (!data.readings.length) { const cell = table.insertRow().insertCell(); cell.colSpan = isLog ? 6 : 5; setText(cell, 'No readings yet.'); }
  sampleListKey = key;
 } catch (error) { setText(el('samples-note'), error.message); } finally { sampleLoading = false; }
}
function setReadingsExpanded(expanded) {
 document.querySelector('.workspace-shell').classList.toggle('readings-collapsed', !expanded);
 el('toggle-readings').setAttribute('aria-expanded', String(expanded));
 setText(el('toggle-readings'), expanded ? 'Collapse' : 'Expand');
 setHidden(el('readings-table'), !expanded);
 draw3D();
}
el('toggle-readings').addEventListener('click', () => {
 setReadingsExpanded(el('readings-table').hidden);
 saveViewPreferences();
});
el('point-picker').addEventListener('change', () => { selectReadingsView(el('point-picker').value); updatePointPicker(); refreshSampleList(); });
el('remove-point').addEventListener('click', async () => { try { await api('/api/remove', {id: selectedPointId}); selectReadingsView(SIGNAL_LOG); await refresh(); } catch (error) { tell(error.message, true); } });
el('pause-recording').addEventListener('click', async () => { try { await api(state.active_id ? '/api/pause' : '/api/resume', {}); await refresh(); } catch (error) { tell(error.message, true); } });
el('reset-data').addEventListener('click', async () => {
 const mode = state.mode, subject = mode === 'simulation' ? 'the simulated device' : state.name;
 if (!window.confirm(`Delete all points and readings for ${subject}? Recording will stop.`)) return;
 el('reset-data').disabled = true;
 try { await api(mode === 'simulation' ? '/api/simulation-reset' : '/api/reset-live', {}); selectReadingsView(SIGNAL_LOG); signature = ''; await refresh(); }
 catch (error) { tell(error.message, true); } finally { el('reset-data').disabled = false; }
});
async function changeSelection(path, value) {
 if (changingSelection) return;
 changingSelection = true;
 selectionRevision++;
 el('signal-source').disabled = true;
 el('device-picker').disabled = true;
 try {
  await api(path, value);
  render(await api('/api/status', null));
 } catch (error) { tell(error.message, true); }
 finally { changingSelection = false; updateDevicePicker(state); }
}
el('signal-source').addEventListener('change', () => changeSelection('/api/signal-source', {source: el('signal-source').value}));
el('device-picker').addEventListener('change', () => {
 if (el('device-picker').value) changeSelection('/api/device', {address: el('device-picker').value});
});
el('mode').addEventListener('change', async () => {
 el('mode').disabled = true;
 try { await api('/api/mode', {mode: el('mode').checked ? 'simulation' : 'live'}); await refresh(); }
 catch (error) { el('mode').checked = state.mode === 'simulation'; tell(error.message, true); } finally { el('mode').disabled = false; }
});
for (const identifier of ['sim-hz', 'sim-noise']) el(identifier).addEventListener('change', async () => {
 try { await api('/api/simulation-settings', {hz: Number(el('sim-hz').value), noise: Number(el('sim-noise').value)}); await refresh(); updateEstimate(); }
 catch (error) { tell(error.message, true); }
});
el('export').addEventListener('click', async () => {
 try {
  const data = await api('/api/export', null), rows = [['device', 'mode', 'point', 'floor', 'x', 'y', 'sample', 'timestamp', 'rssi_dbm', 'run', 'elapsed_seconds', 'location_total_seconds']];
  data.signal_readings.forEach((sample, sampleIndex) => {
   const pointIndex = data.points.findIndex(point => point.id === sample.point_id), point = data.points[pointIndex];
   rows.push([data.target, data.mode, pointIndex >= 0 ? pointIndex + 1 : sample.point_id ?? '', point?.floor ?? '', point?.x ?? '', point?.y ?? '', sampleIndex + 1, new Date(sample.timestamp * 1000).toISOString(), sample.rssi, sample.recording_run ?? '', sample.elapsed_seconds ?? '', point?.total_recording_seconds ?? '']);
  });
  const blob = new Blob([rows.map(row => row.join(',')).join('\r\n')], {type: 'text/csv'}), url = URL.createObjectURL(blob), link = document.createElement('a');
  const source = data.mode === 'live' && data.signal_source === 'wifi' ? 'wifi' : 'bluetooth';
  link.href = url; link.download = `${source}-${data.target.replaceAll(':', '')}-${data.mode}-samples.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url), REFRESH_MILLISECONDS);
 } catch (error) { tell(`CSV export: ${error.message}`, true); }
});
try { el('plan-top').value = localStorage.getItem('bluetooth-finder-plan-top') || ''; } catch (error) { tell(`Cannot read saved plan direction: ${error.message}`, true); }
el('plan-top').addEventListener('input', () => { try { localStorage.setItem('bluetooth-finder-plan-top', el('plan-top').value); } catch (error) { tell(`Cannot save plan direction: ${error.message}`, true); } applyPlanTop(); });
restoreViewPreferences();
ensureLevels(DEFAULT_LEVEL_COUNT); restoreLiveGeometry(); renderGeometry(); applyViewMode(); applyMapAspect();
el('metric-units').addEventListener('change', () => {
 try { localStorage.setItem('bluetooth-finder-metric', String(el('metric-units').checked)); }
 catch (error) { tell(`Cannot save unit preference: ${error.message}`, true); }
 renderGeometry();
});
for (const {key, input} of GEOMETRY_FIELDS) el(input).addEventListener('change', () => {
 const field = el(input), metric = el('metric-units').checked;
 const metres = field.valueAsNumber * (metric ? 1 : METRES_PER_FOOT);
 if (!Number.isFinite(metres) || metres < 1 || metres > 100) {
  const metresPerUnit = metric ? 1 : METRES_PER_FOOT;
  field.value = Number((liveGeometry[key] / metresPerUnit).toFixed(2));
  tell(metric ? 'Enter a dimension from 1 to 100 m.' : 'Enter a dimension from 3.29 to 328.08 ft.', true);
  return;
 }
 liveGeometry[key] = metres;
 try { localStorage.setItem('f1-geometry', JSON.stringify(liveGeometry)); }
 catch (error) { tell(`Cannot save floor dimensions: ${error.message}`, true); }
 applyMapAspect();
 updateEstimate();
});
async function deleteLevel(floor) {
 if (!state || changingLevels) return;
 changingLevels = true;
 const selectedDataset = state.mode === 'live' ? state.target : 'simulation';
 try {
  const preview = await api('/api/level-delete-preview?' + new URLSearchParams({floor}), null);
  if (preview.dataset !== selectedDataset) throw new Error('The selected dataset changed. Choose the level again.');
  if (!preview.deleted_points && !preview.moved_points) return;
  const lost = `${preview.deleted_points} ${preview.deleted_points === 1 ? 'point' : 'points'} will be deleted.`;
  const moved = preview.moved_points ? ` ${preview.moved_points} ${preview.moved_points === 1 ? 'point on a higher level will move' : 'points on higher levels will move'} down one level.` : '';
  if (!window.confirm(`Delete Level ${floor} from ${preview.name}?\n\n${lost}${moved}`)) return;
  await api('/api/delete-level', {floor, dataset: preview.dataset, deleted_points: preview.deleted_points, moved_points: preview.moved_points, confirmed: true});
  await refresh();
 } catch (error) { tell(error.message, true); }
 finally { changingLevels = false; }
}
el('house-levels').addEventListener('change', async () => {
 if (!state) return;
 const input = el('house-levels');
 input.disabled = true;
 try {
  const count = Number(input.value);
  await api('/api/visible-levels', {count});
  await refresh();
 } catch (error) { tell(error.message, true); input.value = state.level_count; }
 finally { input.disabled = false; }
});
el('delete-recording-level').addEventListener('click', () => deleteLevel(el('scene-floor').value));
async function refresh() {
 if (polling || changingSelection) return;
 const revision = selectionRevision;
 polling = true;
 try {
  const data = await api('/api/status', null);
  if (revision === selectionRevision) render(data);
 } catch (error) { tell(`Reader: ${error.message}`, true); } finally { polling = false; }
}
refresh(); setInterval(refresh, REFRESH_MILLISECONDS); setInterval(drawClock, CLOCK_MILLISECONDS);


function buildSceneProjection(width,depth,topFloor,wallHeight,cw,ch,yaw,pitch,zoom){
 const centerZ=wallHeight/2,c=Math.cos(yaw),s=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),distance=4*Math.hypot(width,depth,wallHeight);
 function raw(x,y,z){const dx=x-width/2,dy=y-depth/2,dz=z-centerZ,a=c*dx-s*dy,b=s*dx+c*dy,r=cp*b+sp*dz,f=1-r/distance;return {x:a/f,y:-(cp*dz-sp*b)/f,depth:r};}
 const corners=[raw(0,0,wallHeight),raw(width,0,wallHeight)];
 for(const z of [0,topFloor])for(const x of [0,width])for(const y of [0,depth])corners.push(raw(x,y,z));
 const left=Math.min(...corners.map(p=>p.x)),right=Math.max(...corners.map(p=>p.x)),top=Math.min(...corners.map(p=>p.y)),bottom=Math.max(...corners.map(p=>p.y));
 const cx=(left+right)/2,cy=(top+bottom)/2,scale=Math.min(Math.max(40,cw-100)/(right-left),Math.max(40,ch-100)/(bottom-top))*zoom;
 return {project(x,y,z){const p=raw(x,y,z);return {x:cw/2+(p.x-cx)*scale,y:ch/2+(p.y-cy)*scale,depth:p.depth};},unproject(px,py,z){const rx=(px-cw/2)/scale+cx,ry=(py-ch/2)/scale+cy,dz=z-centerZ,b=(ry*(1-sp*dz/distance)+cp*dz)/(sp+ry*cp/distance),r=cp*b+sp*dz,a=rx*(1-r/distance);return {x:(c*a+s*b+width/2)/width,y:(-s*a+c*b+depth/2)/depth};}};
}
function drawWallLabel(context, projection, width, wallHeight, floorGap) {
 const label = el('plan-top').value.trim();
 if (!label) return;
 const labelElevation = wallHeight - floorGap / 2;
 const center = projection.project(width / 2, 0, labelElevation);
 const across = projection.project(width, 0, labelElevation);
 const below = projection.project(width / 2, 0, labelElevation - floorGap / 2);
 const facing = across.x < center.x ? -1 : 1;
 const fontSize = 16;
 context.save();
 context.font = `600 ${fontSize}px system-ui`;
 context.textAlign = 'center'; context.textBaseline = 'middle';
 const metresPerPixel = Math.min(floorGap / (2 * fontSize), .8 * width / context.measureText(label).width);
 const horizontalScale = facing * metresPerPixel / (width / 2);
 const verticalScale = metresPerPixel / (floorGap / 2);
 context.transform((across.x - center.x) * horizontalScale, (across.y - center.y) * horizontalScale,
  (below.x - center.x) * verticalScale, (below.y - center.y) * verticalScale, center.x, center.y);
 context.fillStyle = '#263122';
 context.fillText(label, 0, 0);
 context.restore();
}
function draw3D(){
 if(!el('view-3d')?.checked)return;
 const container=el('scene-3d'),canvas=el('floor-canvas'),cw=container.clientWidth,ch=container.clientHeight;if(cw<1||ch<1)return;
 const dpr=window.devicePixelRatio||1;if(canvas.width!==Math.round(cw*dpr)||canvas.height!==Math.round(ch*dpr)){canvas.width=Math.round(cw*dpr);canvas.height=Math.round(ch*dpr);}
 const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,cw,ch);
 const {width,depth,height}=geometry(),floorNames=Object.keys(floors);
 const topFloor = (floorNames.length - 1) * height, wallHeight = floorNames.length * height;
 sceneProjection=buildSceneProjection(width,depth,topFloor,wallHeight,cw,ch,scenePose.yaw,scenePose.pitch,scenePose.zoom);sceneHits=[];updateOrientationArrow();
 const project=(x,y,z)=>sceneProjection.project(x*width,y*depth,z*height);
 const layers=floorNames.map((floor,z)=>({floor,z,depth:project(.5,.5,z).depth})).sort((a,b)=>a.depth-b.depth);
 function path(vertices){ctx.beginPath();vertices.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.closePath();}
 const wallCorners = [[0,0], [width,0], [width,wallHeight], [0,wallHeight]];
 path(wallCorners.map(([across, elevation]) => sceneProjection.project(across, 0, elevation)));
 ctx.fillStyle = 'rgba(198,211,190,.35)'; ctx.fill();
 ctx.strokeStyle = '#8c9b82'; ctx.lineWidth = 1.5; ctx.setLineDash([]); ctx.stroke();
 for(const layer of layers){
  const {floor,z}=layer,selected=floor===el('scene-floor').value,corners=[[0,0],[1,0],[1,1],[0,1]].map(([x,y])=>project(x,y,z));
  path(corners);ctx.fillStyle=selected?'rgba(242,249,235,.65)':'rgba(255,255,255,.32)';ctx.fill();ctx.strokeStyle=selected?'#448048':'#9da993';ctx.lineWidth=selected?2:1;ctx.setLineDash([]);ctx.stroke();
  ctx.strokeStyle='rgba(130,145,120,.22)';ctx.lineWidth=1;
  for(let i=1;i<5;i++){ctx.beginPath();for(const [x1,y1,x2,y2] of [[i/5,0,i/5,1],[0,i/5,1,i/5]]){const a=project(x1,y1,z),b=project(x2,y2,z);ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);}ctx.stroke();}
  for(const region of lastEstimate?.regions||[]){if(region.floor!==floor)continue;path(region.polygon.map(([x,y])=>project(x,y,z)));ctx.fillStyle='rgba(40,84,208,.15)';ctx.fill();ctx.strokeStyle='#2854d0';ctx.setLineDash([5,3]);ctx.stroke();ctx.setLineDash([]);}
  ctx.font='bold 12px system-ui';ctx.fillStyle='#3c4935';ctx.textAlign='left';ctx.fillText(floors[floor],corners[0].x+5,corners[0].y-8);
  for(const point of state?.samples||[]){
   if(point.floor!==floor)continue;const p=project(point.x,point.y,z),label=pointLabel(point,state.samples.indexOf(point)+1);
   ctx.beginPath();ctx.arc(p.x,p.y,5,0,Math.PI*2);ctx.fillStyle=color(point.rssi);ctx.fill();if(point.id===state.active_id){ctx.strokeStyle='#168139';ctx.lineWidth=2;ctx.stroke();}
   ctx.font='11px system-ui';const textWidth=ctx.measureText(label).width;ctx.fillStyle='rgba(255,255,255,.86)';ctx.fillRect(p.x+7,p.y-11,textWidth+6,17);ctx.fillStyle='#263122';ctx.fillText(label,p.x+10,p.y+1);
   sceneHits.push({id:point.id,floor,left:p.x-7,right:p.x+textWidth+13,top:p.y-11,bottom:p.y+8});
  }
  if(state?.mode==='simulation'&&state.simulation_truth.floor===floor){const p=project(state.simulation_truth.x,state.simulation_truth.y,z);ctx.font='bold 28px system-ui';ctx.fillStyle='#168139';ctx.textAlign='center';ctx.fillText('+',p.x,p.y+9);ctx.textAlign='left';}
  if(lastEstimate?.ready&&lastEstimate.floor===floor){const p=project(lastEstimate.x,lastEstimate.y,z);ctx.font='bold 22px system-ui';ctx.fillStyle='#1426c6';ctx.textAlign='center';ctx.fillText('X',p.x,p.y+7);ctx.textAlign='left';}
 }
 drawWallLabel(ctx, sceneProjection, width, wallHeight, height);
}
function applyViewMode() {
 const enabled = el('view-3d').checked;
 document.querySelector('.workspace-shell').classList.toggle('view-3d', enabled);
 setHidden(el('scene-level-control'), !enabled);
 setHidden(el('scene-instructions'), !enabled);
 setHidden(document.querySelector('.floor-grid'), enabled);
 setHidden(el('scene-3d'), !enabled);
 if (state) updateDeleteControls();
 draw3D();
 updateOrientationArrow();
}
el('view-3d').addEventListener('change', () => { applyViewMode(); saveViewPreferences(); });
function selectRecordingFloor(floor) {
 preferredRecordingFloor = floor;
 el('scene-floor').value = floor;
 if (state) updateDeleteControls();
 saveViewPreferences();
 draw3D();
}
el('scene-floor').addEventListener('change', () => selectRecordingFloor(el('scene-floor').value));
el('floor-canvas').addEventListener('pointerdown',event=>{if(event.button!==0)return;sceneDrag={x:event.clientX,y:event.clientY,lastX:event.clientX,lastY:event.clientY,moved:false};event.currentTarget.setPointerCapture(event.pointerId);});
el('floor-canvas').addEventListener('pointermove',event=>{if(!sceneDrag)return;const dx=event.clientX-sceneDrag.lastX,dy=event.clientY-sceneDrag.lastY;if(Math.hypot(event.clientX-sceneDrag.x,event.clientY-sceneDrag.y)>4)sceneDrag.moved=true;if(sceneDrag.moved){scenePose.yaw+=dx*.008;scenePose.pitch=Math.max(MIN_PITCH,Math.min(MAX_PITCH,scenePose.pitch+dy*.008));draw3D();}sceneDrag.lastX=event.clientX;sceneDrag.lastY=event.clientY;});
el('floor-canvas').addEventListener('pointerup', event => {
 if (!sceneDrag) return;
 const moved = sceneDrag.moved;
 sceneDrag = null;
 if (moved) { saveViewPreferences(); return; }
 if (!sceneProjection) return;
 const bounds = event.currentTarget.getBoundingClientRect();
 const across = event.clientX - bounds.left, down = event.clientY - bounds.top;
 const hit = [...sceneHits].reverse().find(point => across >= point.left && across <= point.right && down >= point.top && down <= point.bottom);
 if (hit) { selectRecordingFloor(hit.floor); selectPoint({id: hit.id}); return; }
 const floor = el('scene-floor').value;
 const position = sceneProjection.unproject(across, down, Number(floor) * geometry().height);
 if (position.x >= 0 && position.x <= 1 && position.y >= 0 && position.y <= 1) selectPoint({floor, x: position.x, y: position.y});
});
el('floor-canvas').addEventListener('pointercancel', () => {
 if (sceneDrag?.moved) saveViewPreferences();
 sceneDrag = null;
});
el('floor-canvas').addEventListener('wheel', event => {
 event.preventDefault();
 const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, scenePose.zoom * Math.exp(-event.deltaY * .001)));
 if (zoom === scenePose.zoom) return;
 scenePose.zoom = zoom;
 saveViewPreferences();
 draw3D();
}, {passive: false});
new ResizeObserver(draw3D).observe(el('scene-3d'));
