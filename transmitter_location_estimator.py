"""Approximate 3-D RSSI multilateration; no assumed transmit-power calibration."""
import math
import statistics

FLOORS = ('0', '1', '2')


def polygon_hull(points):
    points = sorted(set(points))
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
    if len(points) < 3:
        x, y = points[0] if points else (.5, .5)
        return [[max(0, x-.02), max(0, y-.02)], [min(1, x+.02), max(0, y-.02)],
                [min(1, x+.02), min(1, y+.02)], [max(0, x-.02), min(1, y+.02)]]
    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = [list(p) for p in lower[:-1]+upper[:-1]]
    return hull if len(hull) >= 3 else polygon_hull([points[len(points)//2]])


def regions_for(candidates):
    return [{'floor': floor, 'polygon': polygon_hull([(c['x'], c['y']) for c in candidates if c['floor'] == floor])}
            for floor in sorted({c['floor'] for c in candidates}, key=int) if any(c['floor'] == floor for c in candidates)]


def estimate_simulation(samples, width=10, depth=10, height=3, noise=3, levels=3):
    floors = tuple(str(i) for i in range(levels))
    points = [p for p in samples if p.get('rssi') is not None and p['floor'] in floors]
    if not points:
        return {'ready': False, 'points': 0, 'regions': [{'floor': f, 'polygon': [[0,0],[1,0],[1,1],[0,1]]} for f in floors],
                'message': 'No observations yet; every floor is possible.'}
    observations = []
    for p in points:
        readings = [r['rssi'] for r in p.get('readings', [])]
        observed_noise = statistics.stdev(readings) if len(readings) > 1 else noise
        sigma = max(.9, 1.253*max(noise, observed_noise)/math.sqrt(max(1, len(readings))))
        observations.append((p['x']*width, p['y']*depth, floors.index(p['floor'])*height, p['rssi'], sigma))
    candidates = []
    for floor_index, floor in enumerate(floors):
        for ix in range(26):
            for iy in range(26):
                errors = []
                for px, py, pz, signal, sigma in observations:
                    distance = max(.8, math.sqrt((ix/25*width-px)**2+(iy/25*depth-py)**2+(floor_index*height-pz)**2))
                    errors.append((signal-(-55-25*math.log10(distance)), sigma))
                candidates.append({'floor': floor, 'x': ix/25, 'y': iy/25,
                                   'score': sum((error/sigma)**2 for error, sigma in errors),
                                   'error_db': round(math.sqrt(statistics.mean(e*e for e, sigma in errors)), 2)})
    best = min(candidates, key=lambda c: c['score'])
    plausible = [c for c in candidates if c['score'] <= best['score']+7.8]
    regions = regions_for(plausible)
    distinct = len({(p['floor'], round(p['x']*20), round(p['y']*20)) for p in points})
    return dict(best, ready=distinct >= 3, points=distinct, regions=regions,
                alternative_floors=[r['floor'] for r in regions if r['floor'] != best['floor']],
                quality='Early estimate' if distinct < 3 else 'Simulation estimate',
                message='Click another position to narrow the polygon.')


def estimate_location(samples, width=10, depth=10, height=3, levels=3):
    floors = tuple(str(i) for i in range(levels))
    grouped = {}
    for sample in samples:
        if sample.get('rssi') is None or sample['floor'] not in floors:
            continue
        key = (sample['floor'], round(sample['x'] * 20), round(sample['y'] * 20))
        grouped.setdefault(key, []).append(sample)
    observations = []
    for values in grouped.values():
        sample = values[-1]
        observations.append((sample['x'] * width, sample['y'] * depth,
                             floors.index(sample['floor']) * height,
                             statistics.median(s['rssi'] for s in values)))
    npoints = len(observations)
    if npoints < 4:
        return {'ready': False, 'points': npoints,
                'message': f'Record {4 - npoints} more distinct positions. Spread them around the house and across floors.'}
    floor_scores = []
    candidates = []
    # For each possible position, fit RSSI = A - 10*n*log10(distance).
    # A (the unknown 1-metre signal) and n (attenuation) are nuisance parameters.
    for floor_index, floor in enumerate(floors):
        best_on_floor = None
        for ix in range(26):
            for iy in range(26):
                x, y, z = ix / 25 * width, iy / 25 * depth, floor_index * height
                distances = [max(.8, math.sqrt((x-px)**2 + (y-py)**2 + (z-pz)**2))
                             for px, py, pz, rssi in observations]
                k = [-10 * math.log10(distance) for distance in distances]
                signals = [p[3] for p in observations]
                mk, mr = statistics.mean(k), statistics.mean(signals)
                variance = sum((v-mk)**2 for v in k)
                slope = sum((v-mk)*(r-mr) for v, r in zip(k, signals)) / variance if variance > 1e-8 else 2.5
                slope = min(4.2, max(1.6, slope))
                intercept = mr - slope * mk
                error = math.sqrt(sum((r - (intercept + slope*v))**2 for v, r in zip(k, signals)) / npoints)
                score = error + .15 * abs(slope - 2.5)
                candidate = {'floor': floor, 'x': ix/25, 'y': iy/25, 'error_db': round(error, 2),
                             'score': score, 'attenuation': round(slope, 2)}
                candidates.append(candidate)
                if best_on_floor is None or score < best_on_floor['score']:
                    best_on_floor = candidate
        floor_scores.append(best_on_floor)
    best = min(candidates, key=lambda c: c['score'])
    plausible = [c for c in candidates if c['score'] <= best['score'] + 2]
    alternatives = [c['floor'] for c in floor_scores if c['score'] <= best['score'] + 2 and c['floor'] != best['floor']]
    tested_floors = {p['floor'] for values in grouped.values() for p in values}
    area = [c for c in plausible if c['floor'] == best['floor']]
    bounds = {'left': min(c['x'] for c in area), 'right': max(c['x'] for c in area),
              'top': min(c['y'] for c in area), 'bottom': max(c['y'] for c in area)}
    uncertainty = max(bounds['right'] - bounds['left'], bounds['bottom'] - bounds['top'])
    quality = 'Broad estimate' if alternatives or uncertainty > .5 or len(tested_floors) < 2 else 'Provisional estimate'
    return dict(best, ready=True, points=npoints, quality=quality, area=bounds,
                regions=regions_for(plausible),
                alternative_floors=alternatives, measured_floors=len(tested_floors),
                dimensions={'width': width, 'depth': depth, 'floor_height': height},
                message='Take another reading on an unmeasured floor.' if len(tested_floors) < levels
                else 'Take more readings around the target and on opposite sides of it.')
